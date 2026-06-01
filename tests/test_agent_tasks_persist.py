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
