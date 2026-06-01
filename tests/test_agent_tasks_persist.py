"""Regression test: agent.tasks survives to_persist_dict / from_persist_dict roundtrip.

@user "执行任务存在中断" / "没有生成 TODOs":

Before 2026-05-28, `agent.tasks` was created via `task_update(create)`
and appended to the in-memory list, but ``to_persist_dict`` did NOT
include it. Every server restart reset tasks to []. Symptoms:

  - 小新 created 3 tasks at 18:02 (DB design / API doc / HTML proto)
  - Restart killed all 3
  - On next chat, LLM thought it had nothing in progress → started
    fresh decomposition → looked like "execution interrupted"
  - UI Tasks panel was empty because agent.tasks was empty even
    though the task_update events were in agent.events

This test locks the contract so the regression can't sneak back in.
"""
from __future__ import annotations
import pytest


def test_agent_tasks_survive_persist_roundtrip():
    """Create tasks → to_persist_dict → from_persist_dict → tasks
    must be back with all fields intact."""
    from app.agent import Agent, AgentTask, TaskStatus

    agent = Agent(id="test-agent-tasks-1", name="Tester")
    # Add 3 tasks in various states (mirrors 小新's 18:02 scenario)
    agent.tasks.append(AgentTask(
        id="t-db-design",
        title="数据库设计文档（含DDL）",
        description="设计 ER 模型 + DDL",
        status=TaskStatus.IN_PROGRESS,
        priority=1,
        source="agent_chat",
    ))
    agent.tasks.append(AgentTask(
        id="t-api-doc",
        title="API接口文档",
        description="RESTful 接口规范",
        status=TaskStatus.TODO,
    ))
    agent.tasks.append(AgentTask(
        id="t-html-proto",
        title="HTML交互原型",
        status=TaskStatus.TODO,
        recurrence="weekly",
        recurrence_spec="MON 09:00",
        next_run_at=1234567890.0,
    ))

    # Roundtrip
    d = agent.to_persist_dict()
    assert "tasks" in d, (
        "to_persist_dict MUST include tasks — otherwise restart "
        "wipes every in-progress task and agent loses context")
    assert len(d["tasks"]) == 3

    # Restore
    restored = Agent.from_persist_dict(d)
    assert len(restored.tasks) == 3

    # Verify fields preserved
    db_task = next(t for t in restored.tasks if t.id == "t-db-design")
    assert db_task.title == "数据库设计文档（含DDL）"
    assert db_task.status == TaskStatus.IN_PROGRESS
    assert db_task.priority == 1
    assert db_task.source == "agent_chat"

    html_task = next(t for t in restored.tasks if t.id == "t-html-proto")
    assert html_task.recurrence == "weekly"
    assert html_task.recurrence_spec == "MON 09:00"
    assert html_task.next_run_at == 1234567890.0


def test_agent_tasks_persist_with_no_tasks():
    """Empty tasks list still serializes + restores correctly
    (no defaults breakage)."""
    from app.agent import Agent
    agent = Agent(id="test-agent-tasks-empty", name="Empty")
    assert agent.tasks == []
    d = agent.to_persist_dict()
    assert d.get("tasks") == []
    restored = Agent.from_persist_dict(d)
    assert restored.tasks == []


def test_corrupt_task_skipped_others_survive():
    """If one persisted task has a corrupt enum value, skip just that
    one — don't abort the whole agent load."""
    from app.agent import Agent, AgentTask, TaskStatus
    agent = Agent(id="test-agent-tasks-corrupt", name="Corrupt")
    agent.tasks.append(AgentTask(id="good", title="good task",
                                  status=TaskStatus.TODO))

    d = agent.to_persist_dict()
    # Inject a corrupt task
    d["tasks"].append({"id": "bad", "title": "bad", "status": "INVALID_ENUM_VALUE"})
    d["tasks"].append({"id": "good2", "title": "good 2",
                       "status": "todo"})

    restored = Agent.from_persist_dict(d)
    # Good tasks survive, bad one dropped
    ids = [t.id for t in restored.tasks]
    assert "good" in ids
    assert "good2" in ids
    assert "bad" not in ids


def test_pending_tasks_summary_injects_continuity_reminder():
    """Continuity fix (a) 2026-06-01: when there's open multi-step
    work, the pending-tasks dynamic-context block must include a
    concise continuity directive (finish one → start next, don't wait
    for '继续') pointing to the task-continuity skill. This is what
    makes the skill's discipline reach the model at the right moment
    without depending on the model proactively loading it (progressive
    disclosure)."""
    from app.agent import Agent, AgentTask, TaskStatus

    ag = Agent(id="cont-reminder-test", name="T")
    # No tasks → no block at all
    assert ag.get_pending_tasks_summary() == ""

    # Open multi-step work → block + continuity reminder
    ag.tasks.append(AgentTask(id="1", title="数据库设计",
                               status=TaskStatus.IN_PROGRESS))
    ag.tasks.append(AgentTask(id="2", title="API文档",
                               status=TaskStatus.TODO))
    ag.tasks.append(AgentTask(id="3", title="已完成项",
                               status=TaskStatus.DONE))
    s = ag.get_pending_tasks_summary()
    assert "PENDING TASKS" in s
    assert "数据库设计" in s
    assert "API文档" in s
    assert "已完成项" not in s            # DONE excluded
    assert "立即开始下一步" in s          # continuity directive present
    assert "get_skill_guide" in s         # points to full skill


def test_solo_isolation_strips_cross_agent_tools():
    """Solo context must NOT expose cross-agent communication tools
    (send_message / dispatch_task / sc_handoff / reply_message).

    @user 2026-06-01 "我没给小明安排工作,一直都是 solo 和小新." But the
    logs showed an echo loop: 小新 → send_message → 小明 → reply →
    cascade. hub auto-triggers receiver.chat on send_message
    (hub/_core.py:3758), so a single cross-agent call in solo silently
    wakes peer agents. Solo's whole point is one-on-one with the user.
    Strip the tools at schema-build time so the model can't fire it.
    """
    from app.agent import Agent
    from app.agent_types import AgentProfile

    # Build a coder-like agent with the cross-agent tools allowed
    # (mirrors 小新's real profile, which inherits coder role preset).
    ag = Agent(id="solo-iso-test", name="T", role="coder")
    ag.profile = AgentProfile(allowed_tools=[
        "bash", "read_file", "write_file",
        "send_message", "dispatch_task", "sc_handoff", "reply_message",
    ])

    # Force solo context — no project/meeting bound.
    # get_context_mode() returns "solo" when neither project_id nor
    # meeting_id is stamped on the agent.
    assert ag.get_context_mode() == "solo"

    tool_names = {t["function"]["name"] for t in ag._get_effective_tools()}
    # Cross-agent comm tools MUST be stripped in solo
    for forbidden in ("send_message", "dispatch_task",
                      "sc_handoff", "reply_message"):
        assert forbidden not in tool_names, (
            f"{forbidden} must be filtered out of solo agents — "
            f"got tool list: {sorted(tool_names)}")
    # Normal tools survive
    assert "bash" in tool_names
    assert "read_file" in tool_names


def test_history_summary_keep_last_default_is_generous():
    """Default for TUDOU_HISTORY_SUMMARY_KEEP_LAST should be generous
    enough that an agent in continuous work doesn't go amnesiac after
    each compression pass.

    @user 2026-06-01: logs showed 50 msgs → 4108 chars (18%) and
    'would have left 0 user/assistant msgs' — recent verbatim was
    being squashed into narrative because KEEP_LAST=6 only retained
    ~1.5 tool rounds (4 msgs per round). Raised to 20 — ~5 tool
    rounds verbatim, ~2-3K extra tokens, far cheaper than the wasted
    work from a forgetful agent.
    """
    from app import agent as _agent_mod
    assert _agent_mod._HISTORY_SUMMARY_KEEP_LAST >= 12, (
        f"KEEP_LAST is {_agent_mod._HISTORY_SUMMARY_KEEP_LAST}; "
        f"anything below ~12 leaves continuously-working agents amnesiac")


def test_task_cap_200_bounds_file_size():
    """Persistence caps at 200 tasks (oldest dropped) to keep
    agents.json from ballooning if an agent leaks task creates."""
    from app.agent import Agent, AgentTask, TaskStatus
    agent = Agent(id="test-agent-tasks-cap", name="Cap")
    for i in range(250):
        agent.tasks.append(AgentTask(
            id=f"t-{i:03d}", title=f"task {i}",
            status=TaskStatus.TODO))
    d = agent.to_persist_dict()
    assert len(d["tasks"]) == 200, (
        f"task cap should keep 200 (got {len(d['tasks'])}); "
        f"unbounded persistence would bloat agents.json over time")
    # Latest tasks kept (oldest dropped)
    ids = [t["id"] for t in d["tasks"]]
    assert "t-249" in ids
    assert "t-050" in ids  # 250 - 200 = 50 is the cutoff
    assert "t-049" not in ids
