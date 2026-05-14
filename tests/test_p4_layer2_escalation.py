"""P4 — BackgroundScheduler Layer 2 (LLM escalation framework).

Default DISABLED. Even when enabled, escalation only fires when ALL
4 gates pass:
  1. has_active_user_signal — user plausibly there
  2. in_work_hours          — agent's quiet hours config
  3. not_silent_mode        — operator hasn't muted
  4. budget_remaining       — per-agent + global rolling caps

Tests verify each gate works individually + the composition rejects
on ANY gate fail.

NO real LLM calls in any test — llm_caller is a mock that just
records invocations. Even if a test accidentally enabled escalation,
no actual cost incurred.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from app.background_scheduler_l2 import (
    Layer2Escalator,
    EscalationTrigger,
    EscalationDecision,
    WorkHours,
    _BudgetTracker,
    has_active_user_signal,
    in_work_hours,
    not_silent_mode,
)


# ── Stubs ───────────────────────────────────────────────────────

class _StubAgent:
    def __init__(self, agent_id: str = "test"):
        self.id = agent_id
        self.messages: list = []
        self.profile = type("P", (), {"background_silent": False})()
        self.work_hours = None   # default
        self.last_user_message_at = 0.0
        self._browser_heartbeat_at = 0.0


def _trigger(kind: str = "plan_stuck", agent_id: str = "test") -> EscalationTrigger:
    return EscalationTrigger(
        kind=kind, agent_id=agent_id,
        summary="agent stuck on step X")


# ── Default disabled (most important guard) ─────────────────────

def test_disabled_by_default(monkeypatch):
    """Without TUDOU_BG_LLM_ESCALATION env, escalator is OFF."""
    monkeypatch.delenv("TUDOU_BG_LLM_ESCALATION", raising=False)
    e = Layer2Escalator()
    assert e.enabled is False


def test_explicit_enabled_param_overrides_env(monkeypatch):
    monkeypatch.delenv("TUDOU_BG_LLM_ESCALATION", raising=False)
    e = Layer2Escalator(enabled=True)
    assert e.enabled is True


@pytest.mark.parametrize("val,expected", [
    ("on", True), ("ON", True), ("1", True),
    ("true", True), ("yes", True),
    ("off", False), ("0", False), ("false", False),
    ("", False), ("anything-else", False),
])
def test_env_value_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("TUDOU_BG_LLM_ESCALATION", val)
    e = Layer2Escalator()
    assert e.enabled is expected


def test_disabled_evaluate_always_silent():
    """When disabled, evaluate() always returns will_escalate=False
    regardless of agent state."""
    e = Layer2Escalator(enabled=False)
    agent = _StubAgent()
    # Make agent satisfy ALL gates
    agent.last_user_message_at = time.time()
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)

    decision = e.evaluate(agent, _trigger())
    assert decision.will_escalate is False
    assert decision.gate_failed == "disabled"


def test_disabled_run_decision_does_not_call_llm():
    """run_decision() must NOT invoke llm_caller when disabled."""
    calls = []
    def fake_caller(agent, prompt):
        calls.append((agent.id, prompt))

    e = Layer2Escalator(enabled=False, llm_caller=fake_caller)
    agent = _StubAgent()
    decision = e.evaluate(agent, _trigger())
    e.run_decision(agent, decision)
    assert calls == []


# ── Gate: user active ──────────────────────────────────────────

def test_user_active_recent_message_passes():
    agent = _StubAgent()
    # Recent user message in history
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 60}]
    assert has_active_user_signal(agent) is True


def test_user_idle_old_message_fails():
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 60 * 60}]    # 1h ago
    assert has_active_user_signal(agent) is False


def test_user_active_browser_heartbeat_passes():
    agent = _StubAgent()
    agent._browser_heartbeat_at = time.time() - 30
    assert has_active_user_signal(agent) is True


def test_user_idle_no_signal():
    agent = _StubAgent()
    # No messages, no heartbeat
    assert has_active_user_signal(agent) is False


# ── Gate: work hours ────────────────────────────────────────────

def _utc_ts(*args) -> float:
    """Build a Unix timestamp from UTC components (avoids local-TZ trap
    where datetime(...).timestamp() interprets as local time)."""
    import datetime
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc).timestamp()


def test_in_work_hours_default():
    """Default: weekday 9-22 in UTC."""
    agent = _StubAgent()
    # Tuesday 14:00 UTC
    t = _utc_ts(2026, 5, 12, 14, 0, 0)
    assert in_work_hours(agent, now=t) is True


def test_off_hours_default():
    """Default: weekday 23:30 UTC = outside 9-22."""
    agent = _StubAgent()
    t = _utc_ts(2026, 5, 12, 23, 30, 0)
    assert in_work_hours(agent, now=t) is False


def test_weekend_default_silent():
    """Default: weekend silent. Saturday 2026-05-16 14:00 UTC."""
    agent = _StubAgent()
    t = _utc_ts(2026, 5, 16, 14, 0, 0)
    assert in_work_hours(agent, now=t) is False


def test_weekend_active_when_opted_in():
    agent = _StubAgent()
    agent.work_hours = WorkHours(weekend_active=True)
    t = _utc_ts(2026, 5, 16, 14, 0, 0)
    assert in_work_hours(agent, now=t) is True


def test_tz_offset_respected():
    """tz_offset_hours=8 (Asia/Shanghai) → 14:00 UTC = 22:00 local
    → AT the boundary (exclusive end), so OUTSIDE."""
    agent = _StubAgent()
    agent.work_hours = WorkHours(tz_offset_hours=8)
    t = _utc_ts(2026, 5, 12, 14, 0, 0)
    assert in_work_hours(agent, now=t) is False


# ── Gate: silent mode ──────────────────────────────────────────

def test_not_silent_default():
    agent = _StubAgent()
    assert not_silent_mode(agent) is True


def test_silent_when_profile_flag_set():
    agent = _StubAgent()
    agent.profile.background_silent = True
    assert not_silent_mode(agent) is False


# ── Gate: budget ───────────────────────────────────────────────

def test_budget_first_call_passes():
    bt = _BudgetTracker()
    ok, _ = bt.can_escalate("a", "plan_stuck")
    assert ok is True


def test_budget_per_agent_cap_blocks():
    bt = _BudgetTracker(per_agent_hourly_cap=2,
                        per_trigger_cooldown=0)   # disable cooldown for this test
    bt.record_escalation("a", "plan_stuck")
    bt.record_escalation("a", "different_kind")
    ok, reason = bt.can_escalate("a", "third_kind")
    assert ok is False
    assert "per-agent" in reason


def test_budget_global_cap_blocks():
    bt = _BudgetTracker(global_5min_cap=2,
                        per_agent_hourly_cap=999,
                        per_trigger_cooldown=0)
    # Hammer from many agents
    bt.record_escalation("a", "k1")
    bt.record_escalation("b", "k1")
    ok, reason = bt.can_escalate("c", "k1")
    assert ok is False
    assert "global" in reason


def test_budget_per_trigger_cooldown():
    bt = _BudgetTracker(per_trigger_cooldown=300)
    bt.record_escalation("a", "plan_stuck")
    # Same trigger right away → cooldown blocks
    ok, reason = bt.can_escalate("a", "plan_stuck")
    assert ok is False
    assert "cooldown" in reason
    # Different trigger from same agent → fine
    ok, _ = bt.can_escalate("a", "stale_todos")
    assert ok is True


# ── Composition: any gate fail = silent ────────────────────────

def test_all_gates_pass_then_escalates():
    """Sanity: when EVERYTHING is in order + enabled, escalation
    fires."""
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)
    e = Layer2Escalator(enabled=True)
    decision = e.evaluate(agent, _trigger())
    assert decision.will_escalate is True
    assert decision.gate_failed == ""


def test_user_idle_blocks_escalation():
    agent = _StubAgent()
    # No active signal
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)
    e = Layer2Escalator(enabled=True)
    decision = e.evaluate(agent, _trigger())
    assert decision.will_escalate is False
    assert decision.gate_failed == "user_idle"


def test_off_hours_blocks_escalation():
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    # Force OUT of work hours
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=1,
                                 weekend_active=False)
    e = Layer2Escalator(enabled=True)
    decision = e.evaluate(agent, _trigger())
    assert decision.will_escalate is False
    assert decision.gate_failed in ("off_hours",)


def test_silent_mode_blocks_escalation():
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)
    agent.profile.background_silent = True
    e = Layer2Escalator(enabled=True)
    decision = e.evaluate(agent, _trigger())
    assert decision.will_escalate is False
    assert decision.gate_failed == "silent_mode"


def test_budget_exhaust_blocks_escalation():
    """After per-agent cap reached, more triggers gate with budget."""
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)
    bt = _BudgetTracker(per_agent_hourly_cap=1, per_trigger_cooldown=0)
    e = Layer2Escalator(enabled=True, budget=bt)
    # First passes
    d1 = e.evaluate(agent, _trigger("plan_stuck"))
    assert d1.will_escalate is True
    e.run_decision(agent, d1)
    # Second blocked by cap
    d2 = e.evaluate(agent, _trigger("stale_todos"))
    assert d2.will_escalate is False
    assert d2.gate_failed == "budget"


# ── llm_caller invocation ───────────────────────────────────────

def test_llm_caller_invoked_only_on_pass():
    calls = []
    def fake_caller(agent, prompt):
        calls.append({"agent_id": agent.id, "prompt": prompt})

    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)

    e = Layer2Escalator(enabled=True, llm_caller=fake_caller)
    decision = e.evaluate(agent, _trigger())
    e.run_decision(agent, decision)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == agent.id
    assert "plan_stuck" in calls[0]["prompt"]


def test_llm_caller_NOT_invoked_on_gate_fail():
    calls = []
    def fake_caller(agent, prompt):
        calls.append((agent.id, prompt))

    agent = _StubAgent()
    # User idle → user_idle gate fails
    e = Layer2Escalator(enabled=True, llm_caller=fake_caller)
    decision = e.evaluate(agent, _trigger())
    e.run_decision(agent, decision)
    assert calls == []


def test_llm_caller_exception_isolated():
    """A throwing llm_caller doesn't propagate up."""
    def boom(agent, prompt):
        raise RuntimeError("LLM down")

    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi",
                       "timestamp": time.time() - 30}]
    agent.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                 weekend_active=True)
    e = Layer2Escalator(enabled=True, llm_caller=boom)
    decision = e.evaluate(agent, _trigger())
    # Must not raise
    e.run_decision(agent, decision)


# ── Stats ───────────────────────────────────────────────────────

def test_stats_track_evaluations_and_gates():
    agent_idle = _StubAgent("idle")
    agent_active = _StubAgent("active")
    agent_active.messages = [{"role": "user", "content": "hi",
                              "timestamp": time.time() - 30}]
    agent_active.work_hours = WorkHours(weekday_start=0, weekday_end=24,
                                        weekend_active=True)

    e = Layer2Escalator(enabled=True)
    e.evaluate(agent_idle, _trigger("kind1"))
    e.evaluate(agent_active, _trigger("kind2"))
    e.evaluate(agent_idle, _trigger("kind3"))

    assert e.evaluated_count == 3
    assert e.gate_breakdown.get("user_idle") == 2
    # 1 of the 3 passed
    assert e.gated_count == 2
