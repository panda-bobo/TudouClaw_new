"""Foreman / Watcher — Phase 2 P2-5 (2026-05-06).

A lightweight (non-LLM) polling daemon that watches in-flight agent
runs within a project and intervenes when one looks stuck.

Design choice — RULE-BASED, not an LLM agent:
  * No model token cost
  * No risk of the watcher itself looping
  * Deterministic, easy to unit-test
  * Triggers can be tuned per-project via thresholds dict

Detection signals:
  1. tool_rate_low      — < 1 tool call per minute  (idle / stuck stream)
  2. read_write_ratio   — many reads, no writes     (research lock)
  3. repeat_signature   — guardrail counters elevated
  4. duration_no_progress — IN_PROGRESS for > N min, no deliverable
                            verified

Interventions (escalating):
  * soft_nudge   — append a system message to that agent's queue
  * notify_pm    — send a message to the project's coordinator(s)
  * escalate     — raise a project-level alert (UI / channel)

The Watcher polls every ``poll_interval`` seconds (default 60). It
runs in a daemon thread spawned by ``start_for_project()``. Stop with
``stop()`` when the project is paused/closed.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("tudouclaw.core.watcher")


@dataclass
class WatcherThresholds:
    """Tunable thresholds. All times in seconds."""
    poll_interval: int = 60
    # Soft-nudge if running > this with no new deliverable verified
    no_progress_soft_after: int = 180   # 3 min
    # Notify PM if running > this with no deliverable verified
    no_progress_notify_pm_after: int = 300   # 5 min
    # Escalate to user if running > this with no deliverable verified
    no_progress_escalate_after: int = 600   # 10 min
    # Read-write ratio: ratio of read-class events to write-class events
    # (any threshold-by-time check kicks in only after this many calls)
    read_write_ratio_threshold: float = 5.0
    read_write_ratio_min_samples: int = 8
    # tool_rate_low triggers when tool calls per minute drops below
    tool_rate_low_per_min: float = 0.5


@dataclass
class AgentRunStat:
    """Snapshot of one agent's recent activity. Updated incrementally
    by ``record_tool_call`` and read by ``poll_once``."""
    agent_id: str
    project_id: str
    task_id: str = ""
    started_at: float = field(default_factory=time.time)
    last_tool_at: float = field(default_factory=time.time)
    last_progress_at: float = field(default_factory=time.time)
    read_count: int = 0
    write_count: int = 0
    other_tool_count: int = 0
    last_intervention: str = ""
    last_intervention_at: float = 0.0


# Tools we classify as "read-class" for the ratio check.
_READ_CLASS = frozenset({
    "read_file", "glob_files", "list_dir", "search_files",
    "knowledge_lookup", "memory_recall", "wiki_lookup",
    "session_search",
})
# Tools we classify as "write-class" — successful invocations reset
# the read_count so it reflects "reads since last write".
_WRITE_CLASS = frozenset({
    "write_file", "edit_file", "patch", "create_file",
    "send_message", "dispatch_task", "submit_deliverable",
})


class ProjectWatcher:
    """One Watcher per active project. Pure rule-based — no LLM.

    Wire-up:
      * Hub creates a Watcher when project transitions to 'active'
      * Agent run paths call ``record_tool_call`` after each tool
      * Hub stops the Watcher when project pauses/completes
    """

    def __init__(self, project_id: str, project_name: str = "",
                 thresholds: WatcherThresholds | None = None,
                 send_to_agent: Callable[[str, str], None] | None = None,
                 notify_pm: Callable[[str, str, str], None] | None = None,
                 escalate: Callable[[str, str, dict], None] | None = None):
        self.project_id = project_id
        self.project_name = project_name
        self.thresholds = thresholds or WatcherThresholds()
        # send_to_agent(agent_id, content) — appends a system message
        self._send = send_to_agent
        # notify_pm(project_id, pm_agent_id, content)
        self._notify_pm = notify_pm
        # escalate(project_id, kind, detail)
        self._escalate = escalate
        self._stats: dict[str, AgentRunStat] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._interventions_emitted = 0
        # Warmup window (seconds) — after the watcher boots, suppress
        # ALL interventions for this many seconds. Gives agents a
        # buffer to organically resume work after a server restart
        # before the watcher starts judging them as "stuck". Without
        # this, every restart of a project with persisted in_progress
        # tasks would dump a fresh batch of "Agent stuck" issues into
        # the Issues/Risks tab the moment the first poll fires.
        self._boot_at = time.time()
        self.warmup_seconds = 120  # 2 min

    # ── lifecycle ──

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"watcher-{self.project_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Watcher started for project=%s", self.project_id[:8])

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Watcher stopped for project=%s emitted=%d",
                    self.project_id[:8], self._interventions_emitted)

    # ── instrumentation API (called from agent.py post-tool) ──

    def begin_agent_run(self, agent_id: str, task_id: str = "") -> None:
        now = time.time()
        with self._lock:
            self._stats[agent_id] = AgentRunStat(
                agent_id=agent_id,
                project_id=self.project_id,
                task_id=task_id,
                started_at=now, last_tool_at=now, last_progress_at=now,
            )

    def end_agent_run(self, agent_id: str) -> None:
        with self._lock:
            self._stats.pop(agent_id, None)

    def record_tool_call(self, agent_id: str, tool_name: str,
                         succeeded: bool = True) -> None:
        now = time.time()
        with self._lock:
            st = self._stats.get(agent_id)
            if st is None:
                st = AgentRunStat(
                    agent_id=agent_id, project_id=self.project_id,
                    started_at=now, last_tool_at=now, last_progress_at=now,
                )
                self._stats[agent_id] = st
            st.last_tool_at = now
            if tool_name in _READ_CLASS:
                st.read_count += 1
            elif tool_name in _WRITE_CLASS:
                st.write_count += 1
                if succeeded:
                    # write reset — research budget restored
                    st.read_count = 0
                    st.last_progress_at = now
            else:
                st.other_tool_count += 1

    def mark_progress(self, agent_id: str) -> None:
        """Called when a deliverable is verified (Day 1 contract pass)."""
        with self._lock:
            st = self._stats.get(agent_id)
            if st is not None:
                st.last_progress_at = time.time()

    # ── polling loop ──

    def _loop(self) -> None:
        while not self._stop.wait(self.thresholds.poll_interval):
            try:
                self.poll_once()
            except Exception as e:
                logger.warning("Watcher poll_once failed: %s", e)

    def poll_once(self) -> list[dict]:
        """Single tick of detection + intervention. Returns a list of
        the interventions emitted this tick (for tests).

        Phase 3 P3-fix-watcher-idle (2026-05-06): only judge agents that
        are CURRENTLY in_progress on a ProjectTask. Idle agents (turn
        ended, waiting for next user message) are NOT stuck — leave
        them alone. Without this gate, the Watcher emits false-positive
        interventions on every idle agent forever.
        """
        now = time.time()
        emitted: list[dict] = []

        # Warmup — silence the watcher for the first warmup_seconds
        # after boot to absorb post-restart noise.
        if (now - self._boot_at) < self.warmup_seconds:
            return emitted

        # Fetch the set of agent_ids currently in_progress on this project
        active_agent_ids: set[str] = set()
        try:
            from ..hub import get_hub
            from ..project import ProjectTaskStatus
            hub = get_hub()
            project = hub.projects.get(self.project_id) if hub else None
            if project is not None:
                for t in (project.tasks or []):
                    if t.status == ProjectTaskStatus.IN_PROGRESS and t.assigned_to:
                        active_agent_ids.add(t.assigned_to)
        except Exception as _e:
            logger.debug("Watcher: failed to query active tasks: %s", _e)

        snap: list[AgentRunStat]
        with self._lock:
            snap = [
                AgentRunStat(**vars(s))  # shallow copy so we can read lock-free
                for s in self._stats.values()
            ]
        th = self.thresholds
        for st in snap:
            # Skip agents that aren't actively running a project task —
            # they're idle (finished turn, waiting), not stuck.
            if active_agent_ids and st.agent_id not in active_agent_ids:
                continue
            since_progress = now - st.last_progress_at
            since_tool = now - st.last_tool_at
            samples = st.read_count + st.write_count + st.other_tool_count
            ratio = (st.read_count / max(1, st.write_count)) if st.write_count > 0 else float(st.read_count)
            high_read_ratio = (samples >= th.read_write_ratio_min_samples
                               and ratio >= th.read_write_ratio_threshold)
            tool_rate = samples / max(1.0, (now - st.started_at) / 60.0)
            tool_rate_low = (since_tool > 60) and (tool_rate < th.tool_rate_low_per_min)

            kind = ""
            detail = {
                "agent_id": st.agent_id,
                "task_id": st.task_id,
                "since_progress_s": int(since_progress),
                "since_tool_s": int(since_tool),
                "read_count": st.read_count,
                "write_count": st.write_count,
                "ratio": round(ratio, 2),
                "tool_rate_per_min": round(tool_rate, 2),
            }
            if since_progress > th.no_progress_escalate_after:
                kind = "escalate"
            elif since_progress > th.no_progress_notify_pm_after:
                kind = "notify_pm"
            elif (since_progress > th.no_progress_soft_after
                  or high_read_ratio
                  or tool_rate_low):
                kind = "soft_nudge"
            if not kind:
                continue
            # Throttle: don't repeat the same intervention more than
            # once per (poll_interval * 2)
            cooldown = th.poll_interval * 2
            if (st.last_intervention == kind
                    and (now - st.last_intervention_at) < cooldown):
                continue
            self._fire_intervention(kind, st.agent_id, detail)
            with self._lock:
                live = self._stats.get(st.agent_id)
                if live is not None:
                    live.last_intervention = kind
                    live.last_intervention_at = now
            emitted.append({"kind": kind, **detail})
        return emitted

    def _fire_intervention(self, kind: str, agent_id: str,
                            detail: dict) -> None:
        self._interventions_emitted += 1
        msg = self._format_intervention(kind, agent_id, detail)
        try:
            if kind in ("soft_nudge",) and self._send:
                self._send(agent_id, msg)
            elif kind == "notify_pm" and self._notify_pm:
                # PM agent_id is project-specific; provide both raw msg
                # and detail to the callback so it can decide who to ping
                self._notify_pm(self.project_id, agent_id, msg)
            elif kind == "escalate" and self._escalate:
                self._escalate(self.project_id, "no_progress", detail)
            logger.info("Watcher intervention %s for agent=%s detail=%s",
                        kind, agent_id[:8], detail)
        except Exception as e:
            logger.warning("Watcher intervention dispatch failed: %s", e)
        # Phase 3 (2026-05-06): notify_pm + escalate ALSO create a
        # tracked issue in the project's Issues tab so it's not just a
        # transient chat message — PM can later see "who got stuck on
        # what task" historically.
        if kind in ("notify_pm", "escalate"):
            try:
                from ..tools_split.project import _auto_report_issue
                from ..hub import get_hub
                hub = get_hub()
                project = hub.projects.get(self.project_id) if hub else None
                if project is not None:
                    sev = "high" if kind == "escalate" else "medium"
                    # Title MUST be stable across consecutive ticks so
                    # _auto_report_issue's "exact-title + 1h" dedup
                    # catches re-fires. Previous form embedded the
                    # since_progress seconds in the title and got 30
                    # duplicate issues per hour (one per poll cycle).
                    # Seconds + per-tick stats now live in description.
                    _auto_report_issue(
                        project,
                        title=f"Agent stuck: {agent_id[:8]} (no progress)",
                        description=(
                            f"Watcher detected: no progress for "
                            f"{detail.get('since_progress_s', '?')}s. "
                            f"reads={detail.get('read_count')}, "
                            f"writes={detail.get('write_count')}, "
                            f"ratio={detail.get('ratio')}, "
                            f"tool_rate={detail.get('tool_rate_per_min')}/min. "
                            f"Action: {kind}."
                        ),
                        severity=sev,
                        related_task_id=detail.get("task_id", "") or "",
                        reporter=agent_id,
                        source="watcher",
                    )
            except Exception as e:
                logger.debug("watcher auto-issue skipped: %s", e)

    def _format_intervention(self, kind: str, agent_id: str,
                              detail: dict) -> str:
        if kind == "soft_nudge":
            return (
                f"[Watcher] You appear to be stuck (read={detail['read_count']}, "
                f"write={detail['write_count']}, ratio={detail['ratio']}, "
                f"no progress for {detail['since_progress_s']}s). "
                f"Stop research mode — write your deliverable now, or "
                f"explicitly say what's blocking you in your next message."
            )
        if kind == "notify_pm":
            return (
                f"[Watcher → PM] Agent {agent_id[:8]} on task "
                f"{detail.get('task_id','?')[:8]} has made no progress for "
                f"{detail['since_progress_s']}s "
                f"(reads={detail['read_count']}, writes={detail['write_count']}). "
                f"Consider: re-dispatching with clearer brief, swapping "
                f"agent, or marking the task blocked."
            )
        if kind == "escalate":
            return (
                f"[Escalation] No progress on agent {agent_id[:8]} for "
                f"{detail['since_progress_s']}s — manual review needed."
            )
        return f"[Watcher] {kind}"

    # ── inspection ──

    def snapshot(self) -> list[dict]:
        """For UI / tests — current per-agent stat rows.

        Phase 3 P3-fix-watcher-idle (2026-05-06): each row carries
        ``is_active`` (= currently has an in_progress ProjectTask).
        UI uses this to show "Idle" instead of "Stuck" for finished
        agents.
        """
        now = time.time()
        # Determine active agents (mirror poll_once)
        active_agent_ids: set[str] = set()
        try:
            from ..hub import get_hub
            from ..project import ProjectTaskStatus
            hub = get_hub()
            project = hub.projects.get(self.project_id) if hub else None
            if project is not None:
                for t in (project.tasks or []):
                    if t.status == ProjectTaskStatus.IN_PROGRESS and t.assigned_to:
                        active_agent_ids.add(t.assigned_to)
        except Exception:
            pass
        with self._lock:
            return [
                {
                    "agent_id": s.agent_id, "task_id": s.task_id,
                    "started_at": s.started_at,
                    "last_tool_at": s.last_tool_at,
                    "last_progress_at": s.last_progress_at,
                    "since_progress_s": int(now - s.last_progress_at),
                    "read_count": s.read_count,
                    "write_count": s.write_count,
                    "other_tool_count": s.other_tool_count,
                    "last_intervention": s.last_intervention,
                    "is_active": s.agent_id in active_agent_ids,
                }
                for s in self._stats.values()
            ]


# ── Hub-level registry ──────────────────────────────────────────────

_WATCHERS: dict[str, ProjectWatcher] = {}
_REG_LOCK = threading.RLock()


def get_watcher(project_id: str) -> Optional[ProjectWatcher]:
    with _REG_LOCK:
        return _WATCHERS.get(project_id)


def start_for_project(project_id: str, *, project_name: str = "",
                      send_to_agent: Callable[[str, str], None] | None = None,
                      notify_pm: Callable[[str, str, str], None] | None = None,
                      escalate: Callable[[str, str, dict], None] | None = None,
                      thresholds: WatcherThresholds | None = None,
                      ) -> ProjectWatcher:
    """Get-or-create + start a Watcher for the given project."""
    with _REG_LOCK:
        w = _WATCHERS.get(project_id)
        if w is None:
            w = ProjectWatcher(
                project_id=project_id, project_name=project_name,
                send_to_agent=send_to_agent, notify_pm=notify_pm,
                escalate=escalate, thresholds=thresholds,
            )
            _WATCHERS[project_id] = w
        w.start()
        return w


def stop_for_project(project_id: str) -> None:
    with _REG_LOCK:
        w = _WATCHERS.pop(project_id, None)
    if w is not None:
        w.stop()


def record_tool_call(project_id: str, agent_id: str,
                     tool_name: str, succeeded: bool = True) -> None:
    """Convenience accessor — agent.py post-tool calls this without
    needing to know whether a watcher exists."""
    w = get_watcher(project_id)
    if w is not None:
        w.record_tool_call(agent_id, tool_name, succeeded=succeeded)


def mark_progress(project_id: str, agent_id: str) -> None:
    w = get_watcher(project_id)
    if w is not None:
        w.mark_progress(agent_id)
