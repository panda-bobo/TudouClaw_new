"""Tests for the new task_controller surface: concurrency lock,
crash recovery, subtask spawning."""
from __future__ import annotations

import types

import pytest

from app.v2.core.task import Task, TaskPhase, TaskStatus, TaskContext
from app.v2.core import task_controller


class FakeBus:
    def __init__(self):
        self.events = []
    def publish(self, task_id, phase, event_type, payload):
        self.events.append({"task_id": task_id, "type": event_type, "payload": dict(payload)})
    def flush_and_close(self, task_id=None): pass


class FakeStore:
    """Tiny in-memory store mimicking the slice TaskStore methods we need."""

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.agents: dict[str, object] = {}

    def save(self, task): self.tasks[task.id] = task
    def get_task(self, tid): return self.tasks.get(tid)
    def get_agent(self, aid): return self.agents.get(aid)

    def list_orphaned_running(self):
        return [t for t in self.tasks.values()
                if t.status == TaskStatus.RUNNING and t.phase != TaskPhase.DONE]

    def count_active_tasks(self, agent_id):
        return sum(
            1 for t in self.tasks.values()
            if t.agent_id == agent_id
            and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
        )

    def next_queued_for_agent(self, agent_id):
        q = sorted(
            (t for t in self.tasks.values()
             if t.agent_id == agent_id and t.status == TaskStatus.QUEUED),
            key=lambda t: t.created_at,
        )
        return q[0] if q else None

    def list_queued_for_agent(self, agent_id):
        return sorted(
            (t for t in self.tasks.values()
             if t.agent_id == agent_id and t.status == TaskStatus.QUEUED),
            key=lambda t: t.created_at,
        )

    # connect is never used in tests — stubbed to keep interface parity.
    class _DummyConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a, **k): return type("_R", (), {"fetchall": lambda self_: []})()
    def _connect(self): return self._DummyConn()


class FakeAgent:
    id = "av2"
    capabilities = types.SimpleNamespace(llm_tier="default")


# ── spawn_subtask ─────────────────────────────────────────────────────


def test_spawn_subtask_inherits_agent_and_links_parent():
    parent = Task(
        id="parent_1", agent_id="av2", template_id="conversation",
        intent="do big thing", priority=2, timeout_s=600,
    )
    store = FakeStore()
    store.save(parent)
    bus = FakeBus()

    calls = []
    child = task_controller.spawn_subtask(
        parent, "subtask step",
        agent=FakeAgent(), store=store, bus=bus,
        runner=lambda *_a, **_k: calls.append("runner called"),
    )

    assert child.parent_task_id == "parent_1"
    assert child.agent_id == "av2"
    assert child.priority == 2
    assert child.timeout_s == 600
    assert child.template_id == "conversation"
    assert child.status == TaskStatus.RUNNING
    # Runner fired once on the child.
    assert calls == ["runner called"]
    # task_submitted event emitted for the child with parent link.
    subm = [e for e in bus.events if e["type"] == "task_submitted"]
    assert len(subm) == 1
    assert subm[0]["payload"]["parent_task_id"] == "parent_1"


def test_spawn_subtask_overrides_template_and_priority():
    parent = Task(id="p", agent_id="av2", template_id="conversation",
                  intent="x", priority=5, timeout_s=100)
    store = FakeStore()
    store.save(parent)
    bus = FakeBus()
    child = task_controller.spawn_subtask(
        parent, "override me",
        agent=FakeAgent(), store=store, bus=bus,
        template_id="research_report", priority=1, timeout_s=30,
        runner=lambda *_a, **_k: None,
    )
    assert child.template_id == "research_report"
    assert child.priority == 1
    assert child.timeout_s == 30


# ── recover_orphaned_tasks ────────────────────────────────────────────


def test_recover_restarts_only_running_tasks():
    store = FakeStore()
    store.agents["av2"] = FakeAgent()

    alive = Task(id="a", agent_id="av2", template_id="x",
                 intent="a", phase=TaskPhase.EXECUTE,
                 status=TaskStatus.RUNNING)
    done = Task(id="b", agent_id="av2", template_id="x",
                intent="b", phase=TaskPhase.DONE,
                status=TaskStatus.SUCCEEDED)
    paused = Task(id="c", agent_id="av2", template_id="x",
                  intent="c", phase=TaskPhase.EXECUTE,
                  status=TaskStatus.PAUSED)
    for t in [alive, done, paused]:
        store.save(t)

    bus = FakeBus()
    runs: list[str] = []
    ids = task_controller.recover_orphaned_tasks(
        store, bus,
        runner=lambda t, *_a, **_k: runs.append(t.id),
    )
    assert ids == ["a"]
    assert runs == ["a"]
    # task_resumed event emitted for the restarted one.
    resumed = [e for e in bus.events if e["type"] == "task_resumed"]
    assert len(resumed) == 1
    assert resumed[0]["payload"]["trigger"] == "crash_recovery"


def test_recover_handles_missing_agent():
    store = FakeStore()
    # agent not registered!
    task = Task(id="orphan", agent_id="gone", template_id="x",
                intent="?", phase=TaskPhase.EXECUTE,
                status=TaskStatus.RUNNING)
    store.save(task)
    bus = FakeBus()
    ids = task_controller.recover_orphaned_tasks(
        store, bus, runner=lambda *_a, **_k: None,
    )
    assert ids == []
    # Task was finalised FAILED with reason=agent_missing.
    assert task.status == TaskStatus.FAILED
    assert task.finished_reason == "agent_missing"
    assert task.phase == TaskPhase.DONE
    failed = [e for e in bus.events if e["type"] == "task_failed"]
    assert failed and failed[0]["payload"]["reason"] == "agent_missing"


# ── state transitions cover 409 paths ─────────────────────────────────


def test_pause_rejects_non_running():
    task = Task(id="t", agent_id="av2", template_id="x",
                intent="x", status=TaskStatus.SUCCEEDED, phase=TaskPhase.DONE)
    store = FakeStore()
    bus = FakeBus()
    assert task_controller.pause_task(task, store, bus) is False


def test_cancel_sets_abandoned_and_done():
    task = Task(id="t", agent_id="av2", template_id="x",
                intent="x", status=TaskStatus.RUNNING, phase=TaskPhase.EXECUTE)
    store = FakeStore()
    bus = FakeBus()
    assert task_controller.cancel_task(task, store, bus) is True
    assert task.status == TaskStatus.ABANDONED
    assert task.finished_reason == "cancelled"
    assert task.phase == TaskPhase.DONE
    assert any(e["type"] == "task_failed" for e in bus.events)


def test_accept_clarification_only_when_pending():
    task = Task(id="t", agent_id="av2", template_id="x",
                intent="orig", status=TaskStatus.PAUSED, phase=TaskPhase.INTAKE,
                context=TaskContext(clarification_pending=True))
    store = FakeStore()
    bus = FakeBus()
    ok = task_controller.accept_clarification(
        task, "yes please", agent=FakeAgent(), store=store, bus=bus,
        runner=lambda *_a, **_k: None,
    )
    assert ok is True
    assert "yes please" in task.intent
    assert task.status == TaskStatus.RUNNING
    assert task.context.clarification_pending is False


def test_accept_clarification_rejects_when_not_pending():
    task = Task(id="t", agent_id="av2", template_id="x",
                intent="orig", status=TaskStatus.RUNNING, phase=TaskPhase.INTAKE,
                context=TaskContext(clarification_pending=False))
    ok = task_controller.accept_clarification(
        task, "hi", agent=FakeAgent(), store=FakeStore(), bus=FakeBus(),
        runner=lambda *_a, **_k: None,
    )
    assert ok is False
