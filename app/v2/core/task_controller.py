"""
Task controller — lifecycle-transition helpers used by the REST layer.

Responsibilities:
    * Spawn a background ``TaskLoop`` thread for a task (submit / resume).
    * Coordinate pause / resume / cancel / clarify state transitions with
      persistence (the state flag lives on ``Task``; we save after toggling).

This module is the single place the REST layer goes for "make this task
start running". Handlers never import ``TaskLoop`` directly — it keeps
the API layer free of threading details and also lets tests inject a
fake runner.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional, TYPE_CHECKING

from .task import Task, TaskPhase, TaskStatus

if TYPE_CHECKING:
    from ..agent.agent_v2 import AgentV2
    from .task_events import TaskEventBus
    from .task_store import TaskStore


# Thread starter is injectable so unit tests can run loops in-line.
TaskRunner = Callable[["Task", "AgentV2", "TaskEventBus", "TaskStore"], None]


def _default_runner(
    task: Task,
    agent: "AgentV2",
    bus: "TaskEventBus",
    store: "TaskStore",
) -> None:
    from .task_loop import TaskLoop

    loop = TaskLoop(task=task, agent=agent, bus=bus, store=store)
    th = threading.Thread(
        target=loop.run,
        name=f"TaskLoop-{task.id}",
        daemon=True,
    )
    th.start()


def start_task_loop(
    task: Task,
    agent: "AgentV2",
    bus: "TaskEventBus",
    store: "TaskStore",
    *,
    runner: Optional[TaskRunner] = None,
) -> None:
    (runner or _default_runner)(task, agent, bus, store)


# ── state-transition ops ──────────────────────────────────────────────


def pause_task(task: Task, store: "TaskStore", bus: "TaskEventBus") -> bool:
    if not task.pause():
        return False
    store.save(task)
    bus.publish(task.id, task.phase, "task_paused", {"at": time.time()})
    return True


def resume_task(
    task: Task,
    agent: "AgentV2",
    store: "TaskStore",
    bus: "TaskEventBus",
    *,
    runner: Optional[TaskRunner] = None,
) -> bool:
    if not task.resume():
        return False
    store.save(task)
    bus.publish(task.id, task.phase, "task_resumed", {"at": time.time()})
    start_task_loop(task, agent, bus, store, runner=runner)
    return True


def cancel_task(
    task: Task,
    store: "TaskStore",
    bus: "TaskEventBus",
    *,
    agent: "AgentV2 | None" = None,
    runner: Optional[TaskRunner] = None,
) -> bool:
    """Cancel a RUNNING, PAUSED, or QUEUED task.

    When a RUNNING task is cancelled, the next QUEUED task for its agent
    is immediately promoted (if any), so the queue drains without the
    user having to do anything. QUEUED cancellations don't free the
    agent (it was never busy with this task), so no dispatch happens.
    """
    was_active = task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
    if not task.cancel():
        return False
    store.save(task)
    bus.publish(task.id, task.phase, "task_failed", {
        "summary": "task cancelled by user",
        "failed_phase": "cancelled",
        "reason": "user_cancel",
    })
    # If the cancelled task was occupying the agent, drain the queue.
    if was_active:
        dispatch_next_queued(
            task.agent_id, store, bus, agent=agent, runner=runner,
        )
    return True


def dispatch_next_queued(
    agent_id: str,
    store: "TaskStore",
    bus: "TaskEventBus",
    *,
    agent: "AgentV2 | None" = None,
    runner: Optional[TaskRunner] = None,
) -> str | None:
    """Promote the oldest QUEUED task for ``agent_id`` to RUNNING and
    spawn its TaskLoop. No-op if the agent already has an active task
    or the queue is empty.

    Called at three moments:
      1. A TaskLoop finishes (from ``TaskLoop._finalize``)
      2. A RUNNING task is cancelled (from ``cancel_task``)
      3. Crash-recovery completes (from ``recover_orphaned_tasks``)

    Returns the id of the promoted task, or ``None`` on no-op.
    """
    import time as _time

    if store.count_active_tasks(agent_id) > 0:
        return None
    nxt = store.next_queued_for_agent(agent_id)
    if nxt is None:
        return None

    resolved_agent = agent or store.get_agent(agent_id)
    if resolved_agent is None:
        # Orphan queue entry: finalise as failed rather than strand it.
        nxt.status = TaskStatus.FAILED
        nxt.phase = TaskPhase.DONE
        nxt.finished_reason = "agent_missing"
        nxt.updated_at = _time.time()
        if nxt.completed_at is None:
            nxt.completed_at = nxt.updated_at
        store.save(nxt)
        bus.publish(nxt.id, nxt.phase, "task_failed", {
            "summary": "agent disappeared before dequeue",
            "failed_phase": "queue",
            "reason": "agent_missing",
        })
        return None

    nxt.status = TaskStatus.RUNNING
    nxt.updated_at = _time.time()
    store.save(nxt)
    bus.publish(nxt.id, nxt.phase, "task_resumed", {
        "trigger": "dequeue",
        "at": _time.time(),
    })
    start_task_loop(nxt, resolved_agent, bus, store, runner=runner)
    return nxt.id


def accept_clarification(
    task: Task,
    answer: str,
    agent: "AgentV2",
    store: "TaskStore",
    bus: "TaskEventBus",
    *,
    runner: Optional[TaskRunner] = None,
) -> bool:
    if not task.accept_clarification(answer):
        return False
    store.save(task)
    bus.publish(task.id, TaskPhase.INTAKE, "task_resumed", {
        "trigger": "clarification",
        "at": time.time(),
    })
    start_task_loop(task, agent, bus, store, runner=runner)
    return True


def spawn_subtask(
    parent: Task,
    intent: str,
    *,
    agent: "AgentV2",
    store: "TaskStore",
    bus: "TaskEventBus",
    template_id: str = "",
    priority: int | None = None,
    timeout_s: int | None = None,
    runner: Optional[TaskRunner] = None,
) -> Task:
    """Submit a child task under ``parent``.

    The child inherits agent, priority, and timeout from parent unless
    overridden. ``parent_task_id`` links them; the child is NOT subject
    to the single-agent-single-task concurrency lock (that's enforced
    at the REST layer, which explicitly allows children through).

    Emits a ``task_submitted`` event on the child so SSE subscribers see
    the spawn happen in real time.
    """
    import time as _time
    from .task import Task as _Task

    child = _Task(
        id=f"t_{int(_time.time() * 1000):x}",
        agent_id=parent.agent_id,
        parent_task_id=parent.id,
        template_id=template_id or parent.template_id,
        intent=intent,
        phase=TaskPhase.INTAKE,
        status=TaskStatus.RUNNING,
        priority=int(priority if priority is not None else parent.priority),
        timeout_s=int(timeout_s if timeout_s is not None else parent.timeout_s),
        created_at=_time.time(),
        updated_at=_time.time(),
    )
    child.context.capabilities_snapshot = dict(parent.context.capabilities_snapshot)
    store.save(child)

    bus.publish(child.id, TaskPhase.INTAKE, "task_submitted", {
        "intent": intent,
        "template_id": child.template_id,
        "parent_task_id": parent.id,
        "priority": child.priority,
        "timeout_s": child.timeout_s,
    })
    start_task_loop(child, agent, bus, store, runner=runner)
    return child


def recover_orphaned_tasks(
    store: "TaskStore",
    bus: "TaskEventBus",
    *,
    runner: Optional[TaskRunner] = None,
) -> list[str]:
    """Restart every task persisted as RUNNING (startup crash recovery).

    All V2 TaskLoops live in daemon threads — a process restart kills
    them and leaves the tasks' DB rows at ``status='running'`` with no
    worker. This walks those rows and spawns a fresh loop for each.

    A task whose agent has since been deleted is marked ``failed`` with
    ``finished_reason='agent_missing'`` rather than left hanging.

    Returns the list of task ids that were restarted (excludes the
    failed-out ones).
    """
    import time as _time
    restarted: list[str] = []
    for task in store.list_orphaned_running():
        agent = store.get_agent(task.agent_id)
        if agent is None:
            task.status = TaskStatus.FAILED
            task.finished_reason = "agent_missing"
            task.phase = TaskPhase.DONE
            task.updated_at = _time.time()
            if task.completed_at is None:
                task.completed_at = task.updated_at
            store.save(task)
            bus.publish(task.id, task.phase, "task_failed", {
                "summary": "agent missing after restart",
                "failed_phase": "recovery",
                "reason": "agent_missing",
            })
            continue

        bus.publish(task.id, task.phase, "task_resumed", {
            "trigger": "crash_recovery",
            "at": _time.time(),
        })
        start_task_loop(task, agent, bus, store, runner=runner)
        restarted.append(task.id)

    # For every agent whose queue has entries but no active task
    # (e.g. the RUNNING task crashed and hasn't been restarted above
    # because its row was deleted, or all RUNNING tasks completed
    # between shutdown and startup), promote the next QUEUED task.
    seen_agents: set[str] = set()
    for task in store.list_orphaned_running():  # now empty after loop above
        seen_agents.add(task.agent_id)
    try:
        # Scan DB for any agent with a queued task but no active one.
        all_queued = []
        with store._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                "SELECT DISTINCT agent_id FROM tasks_v2 WHERE status='queued'"
            ).fetchall()
            all_queued = [r["agent_id"] for r in rows]
    except Exception:
        all_queued = []
    for aid in all_queued:
        dispatch_next_queued(aid, store, bus, runner=runner)
    return restarted


__all__ = [
    "start_task_loop",
    "pause_task",
    "resume_task",
    "cancel_task",
    "accept_clarification",
    "spawn_subtask",
    "recover_orphaned_tasks",
    "dispatch_next_queued",
]
