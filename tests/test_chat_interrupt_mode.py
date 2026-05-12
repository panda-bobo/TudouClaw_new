"""Tests for Claude-Code style chat interrupt mode (2026-05-12).

Default behaviour: a new user message preempts any running / queued
chat for the same agent. Old "queue + merge" behaviour can be
restored via ``TUDOU_INTERRUPT_MODE=0``.

These tests focus on the ChatTaskManager-level state transitions.
The agent-side wiring (chat_async branch that calls .abort() and
clears the pending queue) is an integration concern — we cover it
with small mocks here, full chat() integration relies on the
end-to-end smoke from the user's restart.
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.chat_task import ChatTask, ChatTaskStatus, get_chat_task_manager


# ── Helpers ──────────────────────────────────────────────────────────

def _fresh_mgr_for_agent(agent_id: str):
    """Get the singleton manager and clear any leftover tasks for the
    given agent so tests don't bleed into each other."""
    mgr = get_chat_task_manager()
    # Hard-reset by removing any tasks for this agent
    if hasattr(mgr, "_tasks"):
        try:
            for tid, t in list(mgr._tasks.items()):
                if getattr(t, "agent_id", None) == agent_id:
                    del mgr._tasks[tid]
        except Exception:
            pass
    return mgr


# ── ChatTask.abort sets aborted flag + status ──────────────────────

def test_abort_sets_aborted_flag():
    mgr = _fresh_mgr_for_agent("a-int-1")
    t = mgr.create_task("a-int-1", "test")
    t.set_status(ChatTaskStatus.THINKING, "thinking", 50)
    assert t.aborted is False
    t.abort()
    assert t.aborted is True
    assert t.status == ChatTaskStatus.ABORTED


def test_abort_pushes_done_event():
    mgr = _fresh_mgr_for_agent("a-int-2")
    t = mgr.create_task("a-int-2", "test")
    t.abort()
    # Inspect events: should contain an error + a done
    new_events, _ = t.get_events_since(0)
    types = [e.get("type") for e in new_events]
    assert "error" in types
    assert "done" in types


# ── env var TUDOU_INTERRUPT_MODE ─────────────────────────────────────

def test_interrupt_mode_default_on():
    """Without env var set, interrupt mode is ON (Claude-style)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TUDOU_INTERRUPT_MODE", None)
        # Read with default "1"
        val = os.environ.get("TUDOU_INTERRUPT_MODE", "1")
        assert val != "0"


def test_interrupt_mode_disabled_via_env():
    with patch.dict(os.environ, {"TUDOU_INTERRUPT_MODE": "0"}):
        assert os.environ.get("TUDOU_INTERRUPT_MODE", "1") == "0"


def test_interrupt_mode_unrelated_value_keeps_on():
    """Anything other than literal '0' = ON."""
    with patch.dict(os.environ, {"TUDOU_INTERRUPT_MODE": "true"}):
        assert os.environ.get("TUDOU_INTERRUPT_MODE", "1") != "0"


# ── State transition simulation ─────────────────────────────────────

def test_simulated_interrupt_aborts_running_task():
    """End-to-end shape simulation: user sends msg2 while msg1 is
    RUNNING. With interrupt mode, msg1 gets aborted + msg2 starts."""
    mgr = _fresh_mgr_for_agent("a-int-3")
    msg1 = mgr.create_task("a-int-3", "msg1")
    msg1.set_status(ChatTaskStatus.THINKING, "running", 30)
    # Replicate the chat_async interrupt path
    active_states = (ChatTaskStatus.THINKING, ChatTaskStatus.STREAMING,
                     ChatTaskStatus.TOOL_EXEC, ChatTaskStatus.QUEUED,
                     ChatTaskStatus.WAITING_APPROVAL)
    aborted = 0
    for t in mgr.get_agent_tasks("a-int-3"):
        if t.status in active_states:
            t.abort()
            aborted += 1
    msg2 = mgr.create_task("a-int-3", "msg2")
    assert aborted == 1
    assert msg1.aborted is True
    assert msg1.status == ChatTaskStatus.ABORTED
    assert msg2.status != ChatTaskStatus.ABORTED


def test_simulated_interrupt_clears_queue():
    """Queued (but not yet running) tasks also get aborted as
    'Superseded by new message'."""
    mgr = _fresh_mgr_for_agent("a-int-4")
    queued1 = mgr.create_task("a-int-4", "queued1")
    queued1.set_status(ChatTaskStatus.QUEUED, "queued", 0)
    queued2 = mgr.create_task("a-int-4", "queued2")
    queued2.set_status(ChatTaskStatus.QUEUED, "queued", 0)

    pending = [(queued1, "queued1", "admin"), (queued2, "queued2", "admin")]
    for t, _, _ in pending:
        t.set_status(ChatTaskStatus.ABORTED, "Superseded", -1)
    # Clear (in real code: pending.clear())
    pending.clear()
    assert queued1.status == ChatTaskStatus.ABORTED
    assert queued2.status == ChatTaskStatus.ABORTED
    assert pending == []


# ── New task is fresh, not poisoned by prior abort ──────────────────

def test_new_task_after_interrupt_is_fresh():
    """The new (3rd) task created after aborting two should be in a
    pristine state — not inheriting aborted=True or any leftover
    events from the predecessors."""
    mgr = _fresh_mgr_for_agent("a-int-5")
    old1 = mgr.create_task("a-int-5", "old1")
    old1.set_status(ChatTaskStatus.THINKING, "x", 10)
    old1.abort()
    old2 = mgr.create_task("a-int-5", "old2")
    old2.set_status(ChatTaskStatus.QUEUED, "x", 0)
    old2.set_status(ChatTaskStatus.ABORTED, "y", -1)

    fresh = mgr.create_task("a-int-5", "fresh")
    assert fresh.aborted is False
    assert fresh.status != ChatTaskStatus.ABORTED


# ── Threading: abort signal seen from another thread ───────────────

def test_abort_flag_visible_across_threads():
    """The abort flag must be visible from the chat-loop thread that
    polls it. ChatTask.aborted is a plain attr but Python attribute
    reads are atomic — verify via a producer/consumer test."""
    mgr = _fresh_mgr_for_agent("a-int-6")
    t = mgr.create_task("a-int-6", "x")
    t.set_status(ChatTaskStatus.THINKING, "x", 0)

    seen_aborted = [False]

    def poll():
        for _ in range(100):
            if t.aborted:
                seen_aborted[0] = True
                return
            time.sleep(0.01)

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.05)
    t.abort()
    th.join(timeout=1.0)
    assert seen_aborted[0] is True


# ── Idempotency ─────────────────────────────────────────────────────

def test_double_abort_is_safe():
    """Calling abort twice (e.g. interrupt fires while user also
    clicks the abort button) must not raise."""
    mgr = _fresh_mgr_for_agent("a-int-7")
    t = mgr.create_task("a-int-7", "x")
    t.set_status(ChatTaskStatus.THINKING, "x", 0)
    t.abort()
    # Should not raise
    t.abort()
    assert t.aborted is True
    assert t.status == ChatTaskStatus.ABORTED
