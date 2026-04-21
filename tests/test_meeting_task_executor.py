"""Regression: Phase-2 meeting task execution.

After the discussion round ends, any MeetingAssignment that is still
OPEN gets handed to execute_meeting_assignment — which runs the
assignee agent with a different prompt (execution mode, full tools)
and posts the completion result back to the meeting.

Covers:
  - Task-intent detection: @mention + keyword vs @mention alone
  - Auto-create on user post (integration via REST layer mocked out)
  - Executor posts status + final result; assignment flips to done
  - Interrupt gen check drops mid-executor results
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.meeting import (
    AssignmentStatus,
    Meeting,
    MeetingAssignment,
    MeetingRegistry,
    MeetingStatus,
    _detect_task_assignment,
    execute_meeting_assignment,
    meeting_agent_reply,
)


def _fake_agent(aid: str, name: str):
    return SimpleNamespace(id=aid, name=name, role="general", events=[])


def _make_meeting(participant_ids, status=MeetingStatus.ACTIVE) -> Meeting:
    return Meeting(
        id="m-test",
        title="test",
        participants=list(participant_ids),
        status=status,
    )


# ── _detect_task_assignment ──────────────────────────────────────────

def test_detect_finds_task_on_mention_plus_trigger():
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    hits = _detect_task_assignment(
        "@Bob 请完成云交付调研报告", m, agents.get)
    assert len(hits) == 1
    assert hits[0]["assignee_agent_id"] == "a2"
    assert "云交付" in hits[0]["title"]


def test_detect_empty_without_trigger():
    """@ alone is discussion, not a task assignment."""
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    assert _detect_task_assignment(
        "@Bob 你觉得呢？", m, agents.get) == []


def test_detect_empty_without_mention():
    """Trigger alone (no @) doesn't target anyone — no assignment."""
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    assert _detect_task_assignment(
        "谁来完成这个调研", m, agents.get) == []


def test_detect_multiple_mentions_with_one_trigger():
    """@A @B 调研 → both get the assignment (split workload)."""
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    hits = _detect_task_assignment(
        "@Alice @Bob 请一起分析这份数据", m, agents.get)
    assignee_ids = {h["assignee_agent_id"] for h in hits}
    assert assignee_ids == {"a1", "a2"}


def test_detect_english_triggers():
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Bob")}
    hits = _detect_task_assignment(
        "@Bob please prepare a summary report", m, agents.get)
    assert len(hits) == 1


def test_detect_title_truncation():
    """Long messages produce <=80 char titles."""
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Bob")}
    long_msg = "@Bob 请完成 " + "x" * 200
    hits = _detect_task_assignment(long_msg, m, agents.get)
    assert len(hits[0]["title"]) <= 80


def test_detect_excludes_self_mention():
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    # Alice speaking ("a1" is the excluded speaker).
    hits = _detect_task_assignment(
        "@Alice 完成报告", m, agents.get, exclude_agent_id="a1")
    assert hits == []


# ── execute_meeting_assignment ───────────────────────────────────────

def test_executor_runs_agent_and_marks_done():
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Bob")}
    assignment = m.add_assignment(
        title="调研云交付", assignee_agent_id="a1",
    )

    chat_calls = []

    def chat_fn(aid, prompt):
        chat_calls.append((aid, prompt))
        return "已完成调研。已写入 cloud-delivery-report.md 并注册 deliverable。"

    reg = MagicMock(spec=MeetingRegistry)
    execute_meeting_assignment(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn, agent_lookup_fn=agents.get,
        assignment=assignment,
    )

    # Agent was invoked with execution prompt (not meeting discussion).
    assert len(chat_calls) == 1
    assert chat_calls[0][0] == "a1"
    assert "你正在执行" in chat_calls[0][1]
    assert "## 你的任务" in chat_calls[0][1]
    # Should NOT include the discussion-mode "简短为先" rule.
    assert "简短为先" not in chat_calls[0][1]

    # Status flipped to done, result captured.
    assert assignment.status == AssignmentStatus.DONE
    assert "已完成调研" in assignment.result

    # Two messages posted: start tick + final reply.
    assert len(m.messages) >= 2
    start_tick = next((mm for mm in m.messages if "开始执行" in (mm.content or "")), None)
    assert start_tick is not None
    final = m.messages[-1]
    assert final.role == "assistant"
    assert "cloud-delivery-report" in final.content


def test_executor_leaves_open_on_failure():
    """If agent_chat_fn raises or returns error text, assignment stays
    OPEN so a later re-run (or admin intervention) can complete it."""
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Bob")}
    assignment = m.add_assignment(
        title="crashing task", assignee_agent_id="a1",
    )

    def chat_fn(aid, prompt):
        raise RuntimeError("LLM provider unreachable")

    reg = MagicMock(spec=MeetingRegistry)
    execute_meeting_assignment(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn, agent_lookup_fn=agents.get,
        assignment=assignment,
    )
    assert assignment.status == AssignmentStatus.OPEN
    final = m.messages[-1]
    assert "❌" in final.content


def test_executor_drops_result_on_interrupt():
    """User interrupt (gen mismatch) after chat call landed → result
    dropped, assignment still OPEN, no final message posted."""
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Bob")}
    assignment = m.add_assignment(
        title="interrupted task", assignee_agent_id="a1",
    )

    from app.meeting import bump_meeting_reply_gen
    gen_at_spawn = bump_meeting_reply_gen(m.id)

    def chat_fn(aid, prompt):
        # Simulate a NEW user message arriving mid-executor by bumping
        # the generation counter before returning.
        bump_meeting_reply_gen(m.id)
        return "Completed despite interrupt"

    reg = MagicMock(spec=MeetingRegistry)
    execute_meeting_assignment(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn, agent_lookup_fn=agents.get,
        assignment=assignment,
        gen=gen_at_spawn,
    )
    assert assignment.status == AssignmentStatus.OPEN  # stays open
    # Start tick was posted before the chat call; the final assistant
    # message should NOT be — interrupt check fires before add_message.
    assistant_msgs = [mm for mm in m.messages if mm.role == "assistant"]
    assert len(assistant_msgs) == 0


def test_executor_skips_when_agent_missing():
    m = _make_meeting(["a1"])
    assignment = m.add_assignment(
        title="orphan task", assignee_agent_id="zzz-not-a-participant",
    )
    reg = MagicMock(spec=MeetingRegistry)

    chat_calls = []

    def chat_fn(aid, prompt):
        chat_calls.append(aid)
        return "won't get called"

    execute_meeting_assignment(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn,
        agent_lookup_fn=lambda _i: None,  # agent not found
        assignment=assignment,
    )
    assert len(chat_calls) == 0


# ── Integration: reply round → executor chain ───────────────────────

def test_reply_round_runs_executor_after_discussion():
    m = _make_meeting(["a1", "a2"])
    agents = {"a1": _fake_agent("a1", "Alice"),
              "a2": _fake_agent("a2", "Bob")}
    # Seed a task — simulating what the REST route's auto-detect would
    # have added.
    m.add_assignment(title="generate report", assignee_agent_id="a1")

    invocations = []

    def chat_fn(aid, prompt):
        if "你正在执行" in prompt:
            invocations.append(("exec", aid))
            return "报告已生成: report.md"
        invocations.append(("discuss", aid))
        return "好的"

    reg = MagicMock(spec=MeetingRegistry)
    meeting_agent_reply(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn, agent_lookup_fn=agents.get,
        user_msg="kickoff",
        target_agent_ids=["a1"],
    )
    # One discussion call (a1) then one execution call (a1) — in order.
    kinds = [k for k, _ in invocations]
    assert "discuss" in kinds and "exec" in kinds
    assert kinds.index("discuss") < kinds.index("exec")
    # Assignment flipped to done by the executor.
    assignment = m.assignments[0]
    assert assignment.status == AssignmentStatus.DONE


def test_reply_round_skips_executor_for_already_done_assignment():
    m = _make_meeting(["a1"])
    agents = {"a1": _fake_agent("a1", "Alice")}
    a = m.add_assignment(title="already done", assignee_agent_id="a1")
    a.status = AssignmentStatus.DONE

    calls = []

    def chat_fn(aid, prompt):
        calls.append(("exec" if "你正在执行" in prompt else "discuss", aid))
        return "ok"

    reg = MagicMock(spec=MeetingRegistry)
    meeting_agent_reply(
        meeting=m, registry=reg,
        agent_chat_fn=chat_fn, agent_lookup_fn=agents.get,
        user_msg="kickoff",
        target_agent_ids=["a1"],
    )
    kinds = [k for k, _ in calls]
    # Discussion happened, but no execution — the done assignment was skipped.
    assert "discuss" in kinds
    assert "exec" not in kinds
