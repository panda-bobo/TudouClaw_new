"""P3 — BackgroundScheduler Layer 1 (deterministic checks, zero LLM,
2026-05-13).

Tests:
  - lifecycle (start / stop, idempotent, daemon thread)
  - tick: scans agents, runs registered checks, returns summary
  - check 1: plan_stuck — IN_PROGRESS step >30min → marked failed
  - check 2: stale_todos — pending step >1h → emits stale_todo event
  - error isolation: one check failing doesn't kill others
  - tick budget: max_tick exceeded → log + return
  - module-level singleton helpers (start_for / stop / get)

NO LLM calls anywhere. Pure code paths.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from app.background_scheduler import BackgroundScheduler
from app import background_scheduler as _bg
from app.agent_types import (
    ExecutionPlan, ExecutionStep, StepStatus,
)


class _StubAgent:
    def __init__(self, agent_id: str = "test-bg"):
        self.id = agent_id
        self._current_plan: ExecutionPlan | None = None
        self.events: list = []

    def _log(self, kind: str, data: dict):
        self.events.append({"kind": kind, "data": data})


class _StubHub:
    def __init__(self, agents: list):
        self.agents = {a.id: a for a in agents}


# ── Lifecycle ────────────────────────────────────────────────────

def test_start_spawns_daemon_thread():
    hub = _StubHub([])
    sched = BackgroundScheduler(hub, interval_sec=0.1)
    sched.start()
    assert sched._thread is not None
    assert sched._thread.daemon is True
    assert sched._thread.is_alive()
    sched.stop()


def test_start_idempotent():
    hub = _StubHub([])
    sched = BackgroundScheduler(hub, interval_sec=10)
    sched.start()
    t1 = sched._thread
    sched.start()   # again
    assert sched._thread is t1   # same thread, no second spawn
    sched.stop()


def test_stop_signals_loop_to_exit():
    hub = _StubHub([])
    sched = BackgroundScheduler(hub, interval_sec=10)
    sched.start()
    sched.stop()
    # After stop, thread should die quickly (within 2s join)
    if sched._thread:
        assert not sched._thread.is_alive()


# ── Tick ─────────────────────────────────────────────────────────

def test_tick_returns_summary():
    hub = _StubHub([_StubAgent("a1")])
    sched = BackgroundScheduler(hub)
    result = sched.tick()
    assert "tick" in result
    assert "elapsed_ms" in result
    assert "agents_scanned" in result
    assert result["agents_scanned"] == 1
    assert sched.tick_count == 1


def test_tick_with_no_agents():
    hub = _StubHub([])
    sched = BackgroundScheduler(hub)
    result = sched.tick()
    assert result["agents_scanned"] == 0


# ── Check 1: plan_stuck ─────────────────────────────────────────

def test_plan_stuck_marks_long_in_progress_as_failed():
    """A step that's been IN_PROGRESS for > 30min gets auto-marked
    FAILED so the agent doesn't think it's still working on it."""
    agent = _StubAgent("stuck-agent")
    plan = ExecutionPlan(status="active")
    plan.add_step("doing thing")
    plan.steps[0].status = StepStatus.IN_PROGRESS
    # Started 35 min ago
    plan.steps[0].started_at = time.time() - 35 * 60
    agent._current_plan = plan

    hub = _StubHub([agent])
    sched = BackgroundScheduler(hub)
    result = sched.tick()

    assert plan.steps[0].status == StepStatus.FAILED
    assert "auto-marked failed" in plan.steps[0].result_summary
    # Event was logged
    stuck_events = [e for e in agent.events
                    if e["kind"] == "plan_stuck_blocked"]
    assert len(stuck_events) == 1
    assert stuck_events[0]["data"]["step_title"] == "doing thing"
    # Tick summary mentions the change
    assert result["changes"].get("plan_stuck") == 1


def test_plan_stuck_does_not_touch_recent_in_progress():
    """A step started < 30min ago is fine, leave alone."""
    agent = _StubAgent("not-stuck")
    plan = ExecutionPlan(status="active")
    plan.add_step("doing thing")
    plan.steps[0].status = StepStatus.IN_PROGRESS
    plan.steps[0].started_at = time.time() - 5 * 60   # 5 min only
    agent._current_plan = plan

    sched = BackgroundScheduler(_StubHub([agent]))
    sched.tick()

    assert plan.steps[0].status == StepStatus.IN_PROGRESS   # unchanged


def test_plan_stuck_skips_inactive_plan():
    """status != 'active' plans aren't touched."""
    agent = _StubAgent("done-agent")
    plan = ExecutionPlan(status="completed")
    plan.add_step("x")
    plan.steps[0].status = StepStatus.IN_PROGRESS
    plan.steps[0].started_at = time.time() - 99 * 60
    agent._current_plan = plan

    sched = BackgroundScheduler(_StubHub([agent]))
    sched.tick()
    assert plan.steps[0].status == StepStatus.IN_PROGRESS


# ── Check 2: stale_todos ─────────────────────────────────────────

def test_stale_todo_emits_event_on_long_pending():
    agent = _StubAgent("stale-agent")
    plan = ExecutionPlan(status="active",
                         created_at=time.time() - 90 * 60)   # 1.5h ago
    plan.add_step("waiting around")
    plan.steps[0].status = StepStatus.PENDING
    agent._current_plan = plan

    sched = BackgroundScheduler(_StubHub([agent]))
    sched.tick()

    stale_events = [e for e in agent.events if e["kind"] == "stale_todo"]
    assert len(stale_events) == 1
    assert stale_events[0]["data"]["step_title"] == "waiting around"


def test_stale_todo_does_not_re_emit():
    """Already-flagged stale todos shouldn't emit on every tick."""
    agent = _StubAgent("once-only")
    plan = ExecutionPlan(status="active",
                         created_at=time.time() - 90 * 60)
    plan.add_step("once")
    plan.steps[0].status = StepStatus.PENDING
    agent._current_plan = plan

    sched = BackgroundScheduler(_StubHub([agent]))
    sched.tick()
    sched.tick()
    sched.tick()

    stale_events = [e for e in agent.events if e["kind"] == "stale_todo"]
    assert len(stale_events) == 1   # only the first tick fired


def test_stale_todo_skips_recent_pending():
    agent = _StubAgent("fresh")
    plan = ExecutionPlan(status="active",
                         created_at=time.time() - 5 * 60)
    plan.add_step("just added")
    plan.steps[0].status = StepStatus.PENDING
    agent._current_plan = plan

    sched = BackgroundScheduler(_StubHub([agent]))
    sched.tick()
    stale_events = [e for e in agent.events if e["kind"] == "stale_todo"]
    assert stale_events == []


# ── Error isolation ─────────────────────────────────────────────

def test_check_failure_isolated():
    """If one check raises, the others still run + tick still returns."""
    agent = _StubAgent("err-agent")
    sched = BackgroundScheduler(_StubHub([agent]))

    def boom(self, agent):
        raise RuntimeError("simulated check failure")

    # Inject a failing check
    BackgroundScheduler._CHECKS["bad_check"] = boom
    try:
        result = sched.tick()
        # tick still completes
        assert "tick" in result
        assert sched.tick_count == 1
    finally:
        del BackgroundScheduler._CHECKS["bad_check"]


# ── Module helpers ──────────────────────────────────────────────

def test_module_level_start_stop_singleton():
    hub = _StubHub([])
    s1 = _bg.start_for(hub, interval_sec=10)
    s2 = _bg.start_for(hub, interval_sec=10)
    assert s1 is s2   # singleton
    assert _bg.get() is s1
    _bg.stop()
    assert _bg.get() is None


def test_zero_llm_property():
    """Marker test: search the scheduler module for any LLM-call hint
    (chat / completions / openai / etc.) — zero hits expected.
    Layer 1 is meant to be PURE deterministic code."""
    import inspect
    src = inspect.getsource(_bg)
    forbidden = [
        "chat_no_stream", "chat_stream_events", "openai", "anthropic",
        "deepseek", "completions", "_dispatch_stream", "/v1/chat",
    ]
    for word in forbidden:
        assert word.lower() not in src.lower(), (
            f"BackgroundScheduler Layer 1 must not reference LLM "
            f"primitives — found {word!r}")
