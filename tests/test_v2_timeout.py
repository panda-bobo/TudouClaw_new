"""Wall-clock timeout enforcement tests (PRD §6.1 ``timeout_s``).

If a task exceeds ``timeout_s`` since ``started_at``, ``TaskLoop.run``
must finalise it with ``status=FAILED`` / ``finished_reason='timeout'``
on the next phase boundary — no matter what phase handler is running.
"""
from __future__ import annotations

import types

from app.v2.core.task import Task, TaskPhase, TaskStatus
from app.v2.core.task_loop import TaskLoop


class FakeBus:
    def __init__(self):
        self.events = []
    def publish(self, task_id, phase, event_type, payload):
        self.events.append((event_type, dict(payload or {})))
    def flush_and_close(self, task_id=None): pass


class FakeStore:
    def save(self, task): pass


class FakeAgent:
    id = "av2_t"
    capabilities = types.SimpleNamespace(llm_tier="default")


def test_timeout_fires_at_phase_boundary():
    task = Task(
        id="t_timeout", agent_id="av2_t",
        template_id="conversation",
        intent="slow",
        phase=TaskPhase.INTAKE,
        status=TaskStatus.RUNNING,
        timeout_s=1,
    )
    # Simulate a task that was started 10s ago.
    import time
    task.started_at = time.time() - 10.0

    bus = FakeBus()
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(),
                    template={"id": "conversation", "required_slots": [],
                              "verify_rules": []})
    loop.run()

    assert task.status == TaskStatus.FAILED
    assert task.finished_reason == "timeout"
    assert task.phase == TaskPhase.DONE
    types_emitted = [t for t, _ in bus.events]
    assert "task_failed" in types_emitted


def test_no_timeout_when_within_budget():
    """Task that just started should not time out."""
    task = Task(
        id="t_ok", agent_id="av2_t",
        template_id="conversation",
        intent="fast",
        phase=TaskPhase.DONE,   # already done so run() exits immediately
        status=TaskStatus.RUNNING,
        timeout_s=3600,
    )
    import time
    task.started_at = time.time()

    bus = FakeBus()
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(),
                    template={"id": "conversation"})
    loop.run()
    assert task.finished_reason != "timeout"


def test_zero_timeout_is_disabled():
    """timeout_s <= 0 disables the check."""
    task = Task(
        id="t_zero", agent_id="av2_t",
        template_id="conversation",
        intent="no limit",
        phase=TaskPhase.DONE,
        status=TaskStatus.RUNNING,
        timeout_s=0,
    )
    import time
    task.started_at = time.time() - 999.0  # ancient
    bus = FakeBus()
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(), template={})
    loop.run()
    assert task.finished_reason != "timeout"
