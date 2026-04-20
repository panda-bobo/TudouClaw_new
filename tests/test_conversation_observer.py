"""Integration test: feed synthetic AgentEvents through the observer
and assert ChatTask row evolves correctly.
"""
from __future__ import annotations

import pytest

from app.conversation_task import (
    ConversationTask, ConversationTaskStatus,
    ConversationTaskStore,
)
from app.conversation_observer import on_agent_event, mark_done


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated store bound via the singleton override."""
    from app import conversation_task as ct_mod
    ct_mod._reset_singleton_for_tests()
    monkeypatch.setenv("TUDOU_CLAW_DATA_DIR", str(tmp_path))
    # Force singleton creation in tmp_path
    s = ct_mod.get_store()
    yield s
    ct_mod._reset_singleton_for_tests()


def _seed_task(store, agent_id="A", chat_task_id="ct1"):
    t = ConversationTask(
        agent_id=agent_id, intent="do X", title="do X",
        chat_task_id=chat_task_id,
        status=ConversationTaskStatus.RUNNING,
    )
    store.save(t)
    return t


# ── Plan extraction through observer ──────────────────────────────────


def test_plan_extracted_from_first_assistant_message(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {
        "timestamp": 1.0, "kind": "message",
        "data": {
            "role": "assistant",
            "content": (
                "好的，先规划：\n"
                "📋 计划\n"
                "1. 搜索数据 — 工具: web_search\n"
                "2. 抓详情 — 工具: web_fetch\n"
                "3. 写报告 — 工具: write_file\n"
                "\n"
                "开始执行。"
            ),
        },
    }, chat_task_id="ct1")
    reloaded = store.get(seed.id)
    assert len(reloaded.steps) == 3
    assert reloaded.steps[0].status == "running"
    assert reloaded.steps[0].goal == "搜索数据"
    assert reloaded.steps[0].tool_hint == "web_search"
    assert reloaded.steps[1].status == "pending"
    assert reloaded.current_step_idx == 0


def test_plan_not_re_extracted_on_second_message(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant",
        "content": "📋 计划\n1. 做 A\n2. 做 B\n",
    }}, chat_task_id="ct1")
    # Same message again (or a later one with a different plan) should
    # NOT replace the existing steps.
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant",
        "content": "📋 计划\n1. 做 X\n2. 做 Y\n3. 做 Z\n",
    }}, chat_task_id="ct1")
    reloaded = store.get(seed.id)
    assert len(reloaded.steps) == 2
    assert reloaded.steps[0].goal == "做 A"


# ── Step completion advancement ───────────────────────────────────────


def test_step_completion_marker_advances(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant",
        "content": "📋 计划\n1. 搜 — 工具: web_search\n2. 写 — 工具: write_file\n",
    }}, chat_task_id="ct1")
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant",
        "content": "✓ 第 1 步：已搜到 5 条结果",
    }}, chat_task_id="ct1")
    reloaded = store.get(seed.id)
    assert reloaded.steps[0].status == "done"
    assert reloaded.steps[1].status == "running"
    assert reloaded.current_step_idx == 1


# ── Tool-call routing ─────────────────────────────────────────────────


def test_tool_call_attaches_to_matching_step(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant",
        "content": "📋 计划\n1. 搜 — 工具: web_search\n2. 写 — 工具: write_file\n",
    }}, chat_task_id="ct1")

    # Tool call matches step 1's hint
    on_agent_event(seed.agent_id, {"kind": "tool_call", "data": {
        "name": "web_search", "arguments": {"query": "x"},
    }}, chat_task_id="ct1")

    # Tool call matches step 2's hint — should flip step 2 to running
    on_agent_event(seed.agent_id, {"kind": "tool_call", "data": {
        "name": "write_file", "arguments": {"path": "r.md"},
    }}, chat_task_id="ct1")

    reloaded = store.get(seed.id)
    assert len(reloaded.steps[0].tool_calls) == 1
    assert reloaded.steps[0].tool_calls[0]["name"] == "web_search"
    assert len(reloaded.steps[1].tool_calls) == 1
    assert reloaded.tool_call_total == 2


def test_tool_result_enriches_preview(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant", "content": "📋 计划\n1. A — 工具: web_search\n",
    }}, chat_task_id="ct1")
    on_agent_event(seed.agent_id, {"kind": "tool_call", "data": {
        "name": "web_search", "arguments": {"q": "foo"},
    }}, chat_task_id="ct1")
    on_agent_event(seed.agent_id, {"kind": "tool_result", "data": {
        "name": "web_search", "result": "Found 3 pages about foo.",
    }}, chat_task_id="ct1")
    reloaded = store.get(seed.id)
    assert reloaded.steps[0].tool_calls[0]["result_preview"].startswith(
        "Found 3 pages")


# ── Terminal transitions ──────────────────────────────────────────────


def test_mark_done_flips_status_and_closes_running_step(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant", "content": "📋 计划\n1. A\n2. B\n",
    }}, chat_task_id="ct1")
    mark_done(seed.agent_id, chat_task_id="ct1", failed=False)
    r = store.get(seed.id)
    assert r.status == ConversationTaskStatus.DONE
    # Step 1 was running; should now be "done"
    assert r.steps[0].status == "done"


def test_mark_done_failed_path(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "assistant", "content": "📋 计划\n1. A\n2. B\n",
    }}, chat_task_id="ct1")
    mark_done(seed.agent_id, chat_task_id="ct1", failed=True)
    r = store.get(seed.id)
    assert r.status == ConversationTaskStatus.FAILED
    assert r.steps[0].status == "skipped"


def test_events_for_agent_with_no_task_are_silent(store):
    # No seeded task. Observer must not crash, must not create rows.
    on_agent_event("unknown_agent", {"kind": "message", "data": {
        "role": "assistant", "content": "hello",
    }})
    assert store.list_for_agent("unknown_agent") == []


def test_user_messages_are_ignored(store):
    seed = _seed_task(store)
    on_agent_event(seed.agent_id, {"kind": "message", "data": {
        "role": "user", "content": "📋 计划\n1. 用户写的计划会被忽略\n",
    }}, chat_task_id="ct1")
    r = store.get(seed.id)
    assert r.steps == []
