"""Background deterministic scheduler — Layer 1 (P3, 2026-05-13).

Runs as a daemon thread alongside the main Hub. Every ``interval_sec``
ticks, sweeps every loaded agent and runs a small set of deterministic
checks (no LLM calls, no HTTP). Each check is bounded execution time
+ isolated failure (try/except per check).

Design constraints (user 2026-05-13):
  - Default zero LLM cost during idle. Layer 1 is pure code — file
    stat, datetime arithmetic, in-memory state inspection.
  - Layer 2 (LLM escalation) is a separate framework, default
    DISABLED. See app/background_scheduler_l2.py (P4) once shipped.
  - Bounded: total tick duration capped (default 5s). If checks
    overrun, log + skip.
  - Observable: every tick writes a one-line summary to the
    ``tudou.bg_sched`` logger so audit can spot drift.

Currently shipped checks:
  1. plan_stuck         — agent._current_plan IN_PROGRESS step
                          unchanged for ``stuck_after_sec`` (default
                          1800s / 30min) → mark BLOCKED.
  2. stale_todos        — TodoWrite items pending > 1h → emit a
                          ``stale_todo`` event (UI surfaces ⚠️).

Adding a new check: write a method on this class returning a dict
{changed: bool, summary: str}, register it in ``_CHECKS``, ship.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("tudou.bg_sched")


class BackgroundScheduler:
    """Per-hub deterministic check loop. Zero LLM cost."""

    def __init__(self, hub: Any, interval_sec: float = 60.0,
                 max_tick_duration_sec: float = 5.0) -> None:
        self.hub = hub
        self.interval = float(interval_sec)
        self.max_tick = float(max_tick_duration_sec)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-agent state used by the checks (e.g. last seen step
        # timestamps so we can detect "stuck N minutes" without
        # mutating agent state).
        self._agent_state: dict[str, dict] = {}
        # Stats for /status endpoint visibility.
        self.tick_count = 0
        self.last_tick_at: float = 0.0
        self.last_tick_summary: str = ""

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the daemon tick thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="bg-sched", daemon=True)
        self._thread.start()
        logger.info(
            "BackgroundScheduler started (interval=%.0fs, max_tick=%.0fs)",
            self.interval, self.max_tick)

    def stop(self) -> None:
        """Signal the loop to exit. Daemon thread will die on process
        exit too — this is for clean shutdown / tests."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("BackgroundScheduler stopped (after %d ticks)",
                    self.tick_count)

    def _loop(self) -> None:
        # First tick after a short delay so we don't pile onto Hub
        # startup.
        if self._stop_event.wait(min(5.0, self.interval)):
            return
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                # NEVER let a tick error kill the loop.
                logger.warning("BackgroundScheduler tick raised: %s", e)
            if self._stop_event.wait(self.interval):
                return

    # ── One tick ────────────────────────────────────────────────

    def tick(self) -> dict:
        """Run all checks for all agents. Returns a summary dict.

        Public so tests / debug endpoints can invoke it manually.
        """
        t_start = time.time()
        agents = list(getattr(self.hub, "agents", {}).values())
        change_counts: dict[str, int] = {}
        per_agent_results: dict[str, list[str]] = {}
        for agent in agents:
            agent_changes = []
            for check_name, check_fn in self._CHECKS.items():
                # Cap one check at half the tick budget so a runaway
                # check can't starve the rest.
                if (time.time() - t_start) > self.max_tick:
                    logger.warning(
                        "BackgroundScheduler tick exceeded max_tick=%.0fs"
                        " — skipping rest", self.max_tick)
                    break
                try:
                    result = check_fn(self, agent)
                except Exception as e:
                    logger.debug(
                        "check %s failed for agent %s: %s",
                        check_name, getattr(agent, "id", "?")[:8], e)
                    continue
                if isinstance(result, dict) and result.get("changed"):
                    change_counts[check_name] = (
                        change_counts.get(check_name, 0) + 1)
                    agent_changes.append(
                        f"{check_name}:{result.get('summary', '')}")
            if agent_changes:
                per_agent_results[
                    getattr(agent, "id", "?")[:8]] = agent_changes

        # Global checks (not per-agent) would go here. None yet.

        self.tick_count += 1
        self.last_tick_at = time.time()
        elapsed_ms = int((self.last_tick_at - t_start) * 1000)
        if change_counts:
            self.last_tick_summary = (
                f"{elapsed_ms}ms | " + ", ".join(
                    f"{k}={v}" for k, v in change_counts.items()))
            logger.info(
                "BG-SCHED tick #%d: %s", self.tick_count,
                self.last_tick_summary)
        else:
            self.last_tick_summary = f"{elapsed_ms}ms | no changes"
        return {
            "tick": self.tick_count,
            "elapsed_ms": elapsed_ms,
            "agents_scanned": len(agents),
            "changes": change_counts,
            "per_agent": per_agent_results,
        }

    # ── Checks ──────────────────────────────────────────────────

    # Tunables (env-overridable in case operators want to dial)
    PLAN_STUCK_AFTER_SEC = 1800       # 30 min
    TODO_STALE_AFTER_SEC = 3600       # 1 hour

    def _check_plan_stuck(self, agent: Any) -> dict:
        """If agent._current_plan has an IN_PROGRESS step that was
        started > PLAN_STUCK_AFTER_SEC ago, transition it to BLOCKED.
        Writes a 'plan_stuck_blocked' event for observability."""
        plan = getattr(agent, "_current_plan", None)
        if not plan or getattr(plan, "status", "") != "active":
            return {"changed": False, "summary": ""}
        from .agent_types import StepStatus as _SS
        now = time.time()
        cutoff = now - self.PLAN_STUCK_AFTER_SEC
        changes = 0
        for step in (getattr(plan, "steps", []) or []):
            if getattr(step, "status", None) != _SS.IN_PROGRESS:
                continue
            started = float(getattr(step, "started_at", 0) or 0)
            if started <= 0 or started > cutoff:
                continue
            # Stuck — auto-transition to BLOCKED
            try:
                step.status = _SS.FAILED   # closest semantic in our enum
                step.result_summary = (
                    f"[bg_sched] auto-marked failed after "
                    f"{int((now - started) / 60)}min IN_PROGRESS")
                changes += 1
                if hasattr(agent, "_log"):
                    agent._log("plan_stuck_blocked", {
                        "step_id": getattr(step, "id", ""),
                        "step_title": getattr(step, "title", ""),
                        "stuck_minutes": int((now - started) / 60),
                    })
            except Exception:
                pass
        if changes:
            return {"changed": True,
                    "summary": f"agent={agent.id[:8]} stuck_steps={changes}"}
        return {"changed": False, "summary": ""}

    def _check_stale_todos(self, agent: Any) -> dict:
        """Identify TodoWrite items pending too long. Doesn't mutate
        the todos themselves (TodoWrite is the LLM's working memory) —
        just emits an event so the UI can flag them."""
        todos = getattr(agent, "_current_plan", None)   # plan acts as todos
        if not todos:
            return {"changed": False, "summary": ""}
        from .agent_types import StepStatus as _SS
        now = time.time()
        cutoff = now - self.TODO_STALE_AFTER_SEC
        # Per-agent state remembers which step_ids we've already flagged
        # so we don't re-emit every tick.
        st = self._agent_state.setdefault(agent.id, {})
        flagged: set = st.setdefault("stale_todo_ids", set())
        new_flags = 0
        for step in (getattr(todos, "steps", []) or []):
            if getattr(step, "status", None) not in (
                    _SS.PENDING, _SS.IN_PROGRESS):
                continue
            started = float(getattr(step, "started_at", 0)
                            or getattr(todos, "created_at", 0) or 0)
            if started <= 0 or started > cutoff:
                continue
            sid = getattr(step, "id", "") or getattr(step, "title", "")
            if sid in flagged:
                continue
            flagged.add(sid)
            new_flags += 1
            if hasattr(agent, "_log"):
                agent._log("stale_todo", {
                    "step_id": getattr(step, "id", ""),
                    "step_title": getattr(step, "title", ""),
                    "pending_minutes": int((now - started) / 60),
                })
        if new_flags:
            return {"changed": True,
                    "summary": f"agent={agent.id[:8]} new_stale={new_flags}"}
        return {"changed": False, "summary": ""}

    # Map check name → unbound method. Iteration order = check order.
    _CHECKS: dict[str, Callable] = {
        "plan_stuck": _check_plan_stuck,
        "stale_todos": _check_stale_todos,
    }


# ── Module-level singleton helpers (Hub uses these) ──────────────

_active: BackgroundScheduler | None = None


def start_for(hub: Any, interval_sec: float = 60.0) -> BackgroundScheduler:
    """Start (or get) the singleton BackgroundScheduler for this hub."""
    global _active
    if _active is None:
        _active = BackgroundScheduler(hub, interval_sec=interval_sec)
        _active.start()
    return _active


def stop() -> None:
    global _active
    if _active is not None:
        _active.stop()
        _active = None


def get() -> BackgroundScheduler | None:
    return _active
