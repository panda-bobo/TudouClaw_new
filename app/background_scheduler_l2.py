"""Background scheduler — Layer 2 (LLM escalation framework, P4).

Default DISABLED. Set ``TUDOU_BG_LLM_ESCALATION=on`` to opt in.

Layer 1 (app/background_scheduler.py) does deterministic checks
(zero LLM cost). When a check finds something the agent should
*decide* about ("plan stuck — should I continue, abort, or change
approach?"), Layer 2 OPTIONALLY escalates by injecting a system
prompt to the agent and triggering one chat turn.

Strict gates — escalation only fires if ALL of these are true:

  1. ``has_active_user_signal()``  — user is plausibly there to
     react. Last user message < 30min, or explicit browser heartbeat.
  2. ``in_work_hours(agent)``       — per-agent ``work_hours`` config.
     Default: weekday 9:00-22:00 local, weekend silent.
  3. ``budget_remaining(agent)``    — per-agent + global rolling
     escalation count under cap.
  4. ``not_silent_mode(agent)``     — operator hasn't pressed mute.

Any False → silent. Decision logged to ``tudou.bg_sched_l2`` so
audit can see "would have escalated, gated by user_idle".

This module never calls the LLM directly when disabled. The
``Layer2Escalator.run_decision()`` is a no-op stub by default;
operators wire the real LLM call by passing a ``llm_caller``
callable to the constructor (only invoked when all gates pass +
``enabled=True``).
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("tudou.bg_sched_l2")


# ── Tunables (env-overridable) ──────────────────────────────────

DEFAULT_USER_ACTIVE_WINDOW_SEC = 30 * 60        # 30 min
DEFAULT_PER_AGENT_HOURLY_CAP = 3                # max 3 escalations / hour
DEFAULT_GLOBAL_5MIN_CAP = 10                    # max 10 cluster-wide / 5min
DEFAULT_PER_TRIGGER_COOLDOWN_SEC = 30 * 60      # 30 min between same trigger


# ── Data classes ────────────────────────────────────────────────

@dataclass
class WorkHours:
    """Per-agent quiet-hours config. Outside these → never escalate."""
    tz_offset_hours: int = 0    # local offset from UTC; 8 = Asia/Shanghai
    weekday_start: int = 9      # inclusive
    weekday_end: int = 22       # exclusive
    weekend_active: bool = False    # default: weekend silent

    @classmethod
    def default(cls) -> "WorkHours":
        return cls()


@dataclass
class EscalationTrigger:
    """A single reason Layer 1 thinks the agent might want LLM input."""
    kind: str                   # e.g. "plan_stuck"
    agent_id: str
    summary: str                # human-readable trigger description
    meta: dict = field(default_factory=dict)
    detected_at: float = field(default_factory=time.time)


@dataclass
class EscalationDecision:
    """What Layer 2 decided about a trigger."""
    trigger: EscalationTrigger
    will_escalate: bool
    gate_failed: str = ""       # name of failing gate when not escalating
    reason: str = ""            # human-readable

    def as_log_dict(self) -> dict:
        return {
            "trigger_kind": self.trigger.kind,
            "agent_id": self.trigger.agent_id[:8] if self.trigger.agent_id else "",
            "will_escalate": self.will_escalate,
            "gate_failed": self.gate_failed,
            "reason": self.reason,
        }


# ── Gate predicates ─────────────────────────────────────────────

def has_active_user_signal(agent: Any,
                            now: float | None = None,
                            window_sec: float = DEFAULT_USER_ACTIVE_WINDOW_SEC,
                            ) -> bool:
    """User active = last user message in the agent's history was
    sent within ``window_sec``, OR the agent has a recent browser
    heartbeat. If both signals are missing/unknown, treat as
    INACTIVE (conservative).
    """
    now = now or time.time()
    cutoff = now - window_sec
    # Signal A: last user message
    msgs = getattr(agent, "messages", None) or []
    for m in reversed(msgs):
        if m.get("role") == "user":
            # Some user messages have no timestamp; fall back to
            # agent's last_active_at if present.
            ts = m.get("timestamp") or m.get("ts") or 0
            if ts > 0:
                if ts >= cutoff:
                    return True
                break    # found most recent user msg; it's stale
            else:
                # No timestamp on the message — fall back to agent attr
                last_active = float(
                    getattr(agent, "last_user_message_at", 0) or 0)
                if last_active >= cutoff:
                    return True
                break
    # Signal B: browser heartbeat
    hb = float(getattr(agent, "_browser_heartbeat_at", 0) or 0)
    if hb >= now - 5 * 60:
        return True
    return False


def in_work_hours(agent: Any, now: float | None = None) -> bool:
    """Within the agent's work-hours window?"""
    wh = getattr(agent, "work_hours", None) or WorkHours.default()
    if not isinstance(wh, WorkHours):
        # Tolerate raw dict
        wh = WorkHours(
            tz_offset_hours=int(wh.get("tz_offset_hours", 0)) if hasattr(wh, "get") else 0,
            weekday_start=int(wh.get("weekday_start", 9)) if hasattr(wh, "get") else 9,
            weekday_end=int(wh.get("weekday_end", 22)) if hasattr(wh, "get") else 22,
            weekend_active=bool(wh.get("weekend_active", False)) if hasattr(wh, "get") else False,
        )
    # Use timezone-aware UTC datetime (utcfromtimestamp deprecated).
    now_utc = datetime.datetime.fromtimestamp(
        now or time.time(), tz=datetime.timezone.utc)
    local = now_utc + datetime.timedelta(hours=wh.tz_offset_hours)
    is_weekend = local.weekday() >= 5    # 5=Sat, 6=Sun
    if is_weekend and not wh.weekend_active:
        return False
    hour = local.hour
    return wh.weekday_start <= hour < wh.weekday_end


def not_silent_mode(agent: Any) -> bool:
    """Operator hasn't toggled the agent into silent mode."""
    profile = getattr(agent, "profile", None)
    if profile is None:
        return True
    silent = bool(getattr(profile, "background_silent", False))
    return not silent


# ── Budget tracking ─────────────────────────────────────────────

class _BudgetTracker:
    """Per-agent + global rolling escalation counters."""

    def __init__(self,
                 per_agent_hourly_cap: int = DEFAULT_PER_AGENT_HOURLY_CAP,
                 global_5min_cap: int = DEFAULT_GLOBAL_5MIN_CAP,
                 per_trigger_cooldown: float = DEFAULT_PER_TRIGGER_COOLDOWN_SEC,
                 ) -> None:
        self.per_agent_cap = per_agent_hourly_cap
        self.global_cap = global_5min_cap
        self.cooldown = per_trigger_cooldown
        # Lock for thread safety (Layer 2 may run from multiple threads
        # if escalator's run_decision spawns work).
        self._lock = threading.Lock()
        # agent_id → list of escalation timestamps
        self._per_agent: dict[str, list[float]] = {}
        # global timestamps
        self._global: list[float] = []
        # (agent_id, trigger_kind) → last_at
        self._cooldown_at: dict[tuple, float] = {}

    def _trim(self, now: float) -> None:
        """Drop expired entries to keep memory bounded."""
        agent_cutoff = now - 3600
        for aid, ts_list in list(self._per_agent.items()):
            kept = [t for t in ts_list if t >= agent_cutoff]
            if kept:
                self._per_agent[aid] = kept
            else:
                del self._per_agent[aid]
        global_cutoff = now - 5 * 60
        self._global = [t for t in self._global if t >= global_cutoff]

    def can_escalate(self, agent_id: str, trigger_kind: str,
                     now: float | None = None) -> tuple[bool, str]:
        """Returns (allowed, reason_if_blocked)."""
        now = now or time.time()
        with self._lock:
            self._trim(now)
            # Per-trigger cooldown
            cd_key = (agent_id, trigger_kind)
            last = self._cooldown_at.get(cd_key, 0.0)
            if now - last < self.cooldown:
                return False, (
                    f"cooldown — same trigger fired "
                    f"{int((now - last) / 60)}min ago, need "
                    f"{int(self.cooldown / 60)}min between")
            # Per-agent hourly cap
            agent_count = len(self._per_agent.get(agent_id, []))
            if agent_count >= self.per_agent_cap:
                return False, (
                    f"per-agent hourly cap reached "
                    f"({agent_count}/{self.per_agent_cap})")
            # Global 5-min cap
            global_count = len(self._global)
            if global_count >= self.global_cap:
                return False, (
                    f"global 5-min cap reached "
                    f"({global_count}/{self.global_cap})")
            return True, ""

    def record_escalation(self, agent_id: str, trigger_kind: str,
                          now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._per_agent.setdefault(agent_id, []).append(now)
            self._global.append(now)
            self._cooldown_at[(agent_id, trigger_kind)] = now


# ── Main escalator ──────────────────────────────────────────────

class Layer2Escalator:
    """Deciding whether & how to escalate a Layer 1 trigger to LLM.

    Default: disabled. Even when enabled, every escalation passes
    through 4 gates before any LLM cost is incurred.

    Operators wire the actual LLM call via ``llm_caller``:
        def my_caller(agent, prompt: str) -> None:
            agent.chat_async(prompt, source="bg_escalation")
        escalator = Layer2Escalator(llm_caller=my_caller, enabled=True)

    When disabled or when gates fail, ``run_decision()`` is a no-op
    that just logs.
    """

    def __init__(self,
                 enabled: bool | None = None,
                 llm_caller: Callable | None = None,
                 budget: _BudgetTracker | None = None,
                 ) -> None:
        if enabled is None:
            # Env-controlled. Default OFF.
            enabled = (os.environ.get("TUDOU_BG_LLM_ESCALATION", "off")
                       .lower() in ("on", "1", "true", "yes"))
        self.enabled = bool(enabled)
        self.llm_caller = llm_caller
        self.budget = budget or _BudgetTracker()
        # Stats for visibility
        self.evaluated_count = 0
        self.escalated_count = 0
        self.gated_count = 0
        self.gate_breakdown: dict[str, int] = {}

    def evaluate(self, agent: Any,
                 trigger: EscalationTrigger) -> EscalationDecision:
        """Decide whether to escalate. Pure — no LLM call yet."""
        self.evaluated_count += 1
        if not self.enabled:
            self.gated_count += 1
            self.gate_breakdown["disabled"] = (
                self.gate_breakdown.get("disabled", 0) + 1)
            return EscalationDecision(
                trigger=trigger, will_escalate=False,
                gate_failed="disabled",
                reason="Layer 2 escalation disabled (set "
                       "TUDOU_BG_LLM_ESCALATION=on to enable)")
        # Gate 1: user active
        if not has_active_user_signal(agent):
            return self._gate_fail(trigger, "user_idle",
                                   "no active user signal in last 30min")
        # Gate 2: work hours
        if not in_work_hours(agent):
            return self._gate_fail(trigger, "off_hours",
                                   "outside agent work hours")
        # Gate 3: silent mode
        if not not_silent_mode(agent):
            return self._gate_fail(trigger, "silent_mode",
                                   "agent.profile.background_silent=True")
        # Gate 4: budget
        ok, reason = self.budget.can_escalate(
            trigger.agent_id, trigger.kind)
        if not ok:
            return self._gate_fail(trigger, "budget", reason)
        # All gates pass → escalate
        return EscalationDecision(
            trigger=trigger, will_escalate=True,
            reason="all gates passed")

    def _gate_fail(self, trigger: EscalationTrigger,
                   gate: str, reason: str) -> EscalationDecision:
        self.gated_count += 1
        self.gate_breakdown[gate] = self.gate_breakdown.get(gate, 0) + 1
        return EscalationDecision(
            trigger=trigger, will_escalate=False,
            gate_failed=gate, reason=reason)

    def run_decision(self, agent: Any,
                     decision: EscalationDecision) -> None:
        """If decision says escalate, invoke llm_caller (if wired) +
        record budget. If not, just log."""
        if decision.will_escalate:
            self.escalated_count += 1
            self.budget.record_escalation(
                decision.trigger.agent_id, decision.trigger.kind)
            logger.info(
                "BG-SCHED L2 ESCALATE: %s",
                decision.as_log_dict())
            if callable(self.llm_caller):
                try:
                    prompt = self._build_prompt(decision.trigger)
                    self.llm_caller(agent, prompt)
                except Exception as e:
                    logger.warning(
                        "Layer 2 llm_caller raised for %s: %s",
                        decision.trigger.kind, e)
        else:
            # Silent — just log the gate
            logger.info(
                "BG-SCHED L2 SILENT (gate=%s): %s",
                decision.gate_failed, decision.as_log_dict())

    def _build_prompt(self, trigger: EscalationTrigger) -> str:
        """Generate the system message that would be injected into the
        agent's chat. Currently template-based; could become
        per-trigger-kind smarter later."""
        return (
            f"[bg_scheduler] {trigger.kind}: {trigger.summary}\n"
            f"This is a low-priority background nudge. If you have an "
            f"answer or next step, share it. If still working, just "
            f"acknowledge briefly. Don't burn tokens repeating context."
        )


# ── Module-level singleton (for use from background_scheduler.py) ─

_active: Layer2Escalator | None = None


def get(enabled: bool | None = None,
        llm_caller: Callable | None = None) -> Layer2Escalator:
    global _active
    if _active is None:
        _active = Layer2Escalator(enabled=enabled, llm_caller=llm_caller)
    return _active


def reset() -> None:
    """For tests."""
    global _active
    _active = None
