"""Unit tests for app.conversation_task.

These verify the store's CRUD + startup-recovery semantics in
isolation. Integration with the V1 chat hook happens in later tests.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from app.conversation_task import (
    ConversationTask,
    ConversationStep,
    ConversationTaskStore,
    ConversationTaskStatus,
)


@pytest.fixture
def store(tmp_path):
    return ConversationTaskStore(str(tmp_path / "ct.db"))


def _make_task(agent_id="ag1", status="running"):
    return ConversationTask(
        agent_id=agent_id, intent="do X then Y", title="do X then Y",
        status=status,
        steps=[ConversationStep(id="s1", goal="search", tool_hint="web_search")],
    )


def test_save_and_get_round_trip(store):
    t = _make_task()
    store.save(t)
    loaded = store.get(t.id)
    assert loaded is not None
    assert loaded.id == t.id
    assert loaded.intent == "do X then Y"
    assert len(loaded.steps) == 1
    assert loaded.steps[0].goal == "search"


def test_get_missing_returns_none(store):
    assert store.get("does-not-exist") is None


def test_save_updates_existing_row(store):
    t = _make_task()
    store.save(t)
    t.status = ConversationTaskStatus.DONE
    t.current_step_idx = 1
    store.save(t)
    reloaded = store.get(t.id)
    assert reloaded.status == ConversationTaskStatus.DONE
    assert reloaded.current_step_idx == 1


def test_list_for_agent_filters_and_order(store):
    t1 = _make_task(agent_id="A")
    time.sleep(0.01)
    t2 = _make_task(agent_id="A")
    t3 = _make_task(agent_id="B")
    for t in (t1, t2, t3):
        store.save(t)
    rows = store.list_for_agent("A")
    assert len(rows) == 2
    assert rows[0].id == t2.id   # newest first
    assert rows[1].id == t1.id


def test_list_for_agent_excludes_terminal_when_requested(store):
    t_run = _make_task(agent_id="A")
    t_done = _make_task(agent_id="A", status=ConversationTaskStatus.DONE)
    t_fail = _make_task(agent_id="A", status=ConversationTaskStatus.FAILED)
    for t in (t_run, t_done, t_fail):
        store.save(t)
    active = store.list_for_agent("A", include_terminal=False)
    assert len(active) == 1
    assert active[0].id == t_run.id


def test_list_resumable_respects_agent_filter(store):
    ta = _make_task(agent_id="A", status=ConversationTaskStatus.RUNNING)
    tb = _make_task(agent_id="B", status=ConversationTaskStatus.PAUSED)
    tc = _make_task(agent_id="A", status=ConversationTaskStatus.DONE)
    for t in (ta, tb, tc):
        store.save(t)
    assert {r.id for r in store.list_resumable()} == {ta.id, tb.id}
    assert {r.id for r in store.list_resumable("A")} == {ta.id}


def test_delete_removes_row(store):
    t = _make_task()
    store.save(t)
    assert store.delete(t.id) is True
    assert store.get(t.id) is None
    assert store.delete(t.id) is False  # second delete no-ops


def test_mark_paused_if_running_flips_only_running(store):
    t_run = _make_task(status=ConversationTaskStatus.RUNNING)
    t_done = _make_task(status=ConversationTaskStatus.DONE)
    t_pause = _make_task(status=ConversationTaskStatus.PAUSED)
    for t in (t_run, t_done, t_pause):
        store.save(t)
    flipped = store.mark_paused_if_running()
    assert flipped == 1
    assert store.get(t_run.id).status == ConversationTaskStatus.PAUSED
    assert store.get(t_done.id).status == ConversationTaskStatus.DONE
    assert store.get(t_pause.id).status == ConversationTaskStatus.PAUSED


def test_step_round_trip_preserves_tool_calls(store):
    t = _make_task()
    t.steps[0].tool_calls = [
        {"name": "web_search", "arguments_preview": '{"q":"x"}',
         "result_preview": "3 hits", "ts": 1234.5},
    ]
    t.steps[0].status = "done"
    t.steps[0].completed_at = 1235.0
    store.save(t)
    reloaded = store.get(t.id)
    assert reloaded.steps[0].status == "done"
    assert len(reloaded.steps[0].tool_calls) == 1
    assert reloaded.steps[0].tool_calls[0]["name"] == "web_search"


def test_singleton_returns_same_instance(tmp_path, monkeypatch):
    from app import conversation_task as ct_mod
    ct_mod._reset_singleton_for_tests()
    monkeypatch.setenv("TUDOU_CLAW_DATA_DIR", str(tmp_path))
    a = ct_mod.get_store()
    b = ct_mod.get_store()
    assert a is b
    ct_mod._reset_singleton_for_tests()


def test_mark_paused_preserves_step_state(store):
    """Crash recovery: running task is flipped to paused, but its
    step history (plan + tool_calls) must survive intact. Resume
    depends on this."""
    t = _make_task()
    t.steps = [
        ConversationStep(id="s1", goal="search", status="done",
                          tool_calls=[{"name": "web_search",
                                       "arguments_preview": "...",
                                       "result_preview": "3 hits",
                                       "ts": 1.0}]),
        ConversationStep(id="s2", goal="fetch", status="running"),
        ConversationStep(id="s3", goal="report", status="pending"),
    ]
    t.current_step_idx = 1
    t.tool_call_total = 1
    store.save(t)
    store.mark_paused_if_running()
    reloaded = store.get(t.id)
    assert reloaded.status == ConversationTaskStatus.PAUSED
    assert len(reloaded.steps) == 3
    assert reloaded.steps[0].status == "done"
    assert reloaded.steps[0].tool_calls[0]["name"] == "web_search"
    assert reloaded.steps[1].status == "running"
    assert reloaded.current_step_idx == 1
    assert reloaded.tool_call_total == 1
