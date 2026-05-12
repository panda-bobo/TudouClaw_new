"""Tests for real-time persistence hook in Agent (2026-05-12).

Real-world bug: ~/run_tudou.sh --stop uses kill -9 (SIGKILL) which
bypasses both atexit and the SIGTERM handler in hub/_core.py:506.
Result: any chat history added since the last completed chat() call
was lost on restart, because save only fired in the post-chat
supervisor sync.

Fix: agent calls _maybe_persist() at iteration boundaries inside
chat() and force-saves on user-message-append and chat() exit.
A throttle (default 1s/agent) prevents disk hammering during fast
tool loops.

These tests cover the throttle + force semantics in isolation —
no Hub, no LLM. The Hub-side wiring (_wire_persist_callback) is
exercised by integration when chat() actually runs.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.agent import Agent


# ── Fixtures ──────────────────────────────────────────────────────────

def _make_agent_with_callback(min_interval: float = 1.0):
    """Build a minimal Agent with a mock persist callback wired."""
    agent = Agent(id="test-1", name="t")
    cb = MagicMock(name="persist_callback")
    agent._persist_callback = cb
    agent._persist_min_interval = min_interval
    agent._last_persist_at = 0.0
    return agent, cb


# ── Tests ─────────────────────────────────────────────────────────────

def test_first_call_saves():
    agent, cb = _make_agent_with_callback()
    agent._maybe_persist()
    assert cb.call_count == 1
    cb.assert_called_with(agent)


def test_throttle_skips_rapid_second_call():
    agent, cb = _make_agent_with_callback(min_interval=10.0)
    agent._maybe_persist()
    agent._maybe_persist()   # < 10s elapsed
    assert cb.call_count == 1


def test_throttle_allows_call_after_interval():
    agent, cb = _make_agent_with_callback(min_interval=0.05)
    agent._maybe_persist()
    time.sleep(0.07)
    agent._maybe_persist()
    assert cb.call_count == 2


def test_force_bypasses_throttle():
    agent, cb = _make_agent_with_callback(min_interval=10.0)
    agent._maybe_persist()
    agent._maybe_persist(force=True)   # forced even though throttle window open
    assert cb.call_count == 2


def test_no_callback_is_silent_noop():
    """Agents without a callback (tests, standalone use) just don't save —
    must not raise, must not log anything alarming."""
    agent = Agent(id="t", name="t")
    # No callback set
    agent._maybe_persist()
    agent._maybe_persist(force=True)
    # If we got here without exception, we're good


def test_callback_exception_does_not_break_loop():
    """A failing callback must not propagate — chat() loop must continue
    so the user's request gets answered even if disk is full."""
    agent, _ = _make_agent_with_callback()
    bad_cb = MagicMock(side_effect=IOError("disk full"))
    agent._persist_callback = bad_cb
    # Must not raise
    agent._maybe_persist()
    assert bad_cb.call_count == 1


def test_last_persist_at_updated_on_success():
    agent, cb = _make_agent_with_callback()
    before = time.time()
    agent._maybe_persist()
    after = time.time()
    assert before <= agent._last_persist_at <= after


def test_last_persist_at_not_updated_on_throttle_skip():
    agent, cb = _make_agent_with_callback(min_interval=10.0)
    agent._maybe_persist()
    first_save_at = agent._last_persist_at
    time.sleep(0.01)
    agent._maybe_persist()   # throttled
    # Timestamp should NOT have moved
    assert agent._last_persist_at == first_save_at


def test_last_persist_at_not_updated_on_callback_failure():
    """If callback raises, _last_persist_at stays unchanged so the next
    call retries instead of waiting another full throttle window."""
    agent, _ = _make_agent_with_callback()
    agent._persist_callback = MagicMock(side_effect=RuntimeError("nope"))
    before = time.time()
    agent._maybe_persist()
    # _last_persist_at should remain at original 0.0
    assert agent._last_persist_at == 0.0 or agent._last_persist_at < before


def test_throttle_per_agent_independent():
    """Two agents have independent throttle clocks."""
    a1, cb1 = _make_agent_with_callback(min_interval=10.0)
    a2, cb2 = _make_agent_with_callback(min_interval=10.0)
    a1._maybe_persist()
    a2._maybe_persist()
    # Both saved their first time, throttle is per-agent
    assert cb1.call_count == 1
    assert cb2.call_count == 1


def test_callback_receives_agent_instance():
    """The callback is invoked with the agent itself, so the hub-side
    callback can call agent.to_persist_dict() etc."""
    agent, cb = _make_agent_with_callback()
    agent._maybe_persist()
    assert cb.call_args[0][0] is agent
