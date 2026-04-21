"""Coordination tools — team_create, send_message, task_update.

All three operate on the hub's agent registry and task queues. Grouped
because they share the ``_get_hub()`` entry point and the "caller-agent"
context-extraction pattern via the ``_caller_agent_id`` kwarg.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from ._common import _get_hub

logger = logging.getLogger(__name__)


# task_update result preview cap — we store completed task results on
# the parent task but truncate so a chatty worker doesn't blow up the
# agent-list JSON payload.
_BG_RESULT_PREVIEW_CHARS = 4000
_BG_EVENT_PREVIEW_CHARS = 200
_SEND_MESSAGE_PREVIEW_CHARS = 200


# ── team_create ──────────────────────────────────────────────────────

def _tool_team_create(name: str, task: str, role: str = "coder",
                      working_dir: str = "", **_: Any) -> str:
    """Spawn a background worker to run a task in parallel.

    The worker is NOT a first-class Agent — it's a transient background
    task owned by the caller. It inherits the caller's model/provider,
    runs the task to completion, pushes a result entry into the caller's
    task list, then disappears. The UI never sees it as a separate agent.
    """
    try:
        hub = _get_hub()
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        parent = hub.get_agent(caller_id) if caller_id else None
        if parent is None:
            return ("Error: team_create requires a calling agent context; "
                    "none was found.")

        worker_id = uuid.uuid4().hex[:8]
        worker_label = f"{role}:{name}" if name else role

        # Record the background job as a task on the PARENT agent so the
        # user can track it from the parent's task list / execution log.
        try:
            t = parent.add_task(
                title=f"[bg:{worker_label}] {task[:80]}",
                description=task,
            )
            task_id = t.id
        except Exception:
            task_id = ""

        def _run_background():
            from ..agent import create_agent as _create_agent_fn
            try:
                # Build an ephemeral worker that inherits parent's config.
                # Resolve working directory: explicit > parent's
                # shared_workspace > parent's working_dir. This ensures
                # child agents in a project share the same directory.
                _wd = working_dir or parent.shared_workspace or parent.working_dir
                worker = _create_agent_fn(
                    name=f"__bg_{worker_label}_{worker_id}",
                    role=role,
                    model=parent.model,
                    provider=parent.provider,
                    working_dir=_wd,
                    node_id=parent.node_id,
                    parent_id=parent.id,
                )
                # Inherit project context so child knows where to write files.
                worker.shared_workspace = parent.shared_workspace
                worker.project_id = parent.project_id
                worker.project_name = parent.project_name
                # Don't register it in the hub — it's transient.
                result_text = ""
                try:
                    result_text = worker.chat(task) or ""
                except Exception as e:
                    result_text = f"Worker error: {e}"
                # Push result back as a completed task entry on the parent.
                if task_id:
                    try:
                        parent.update_task(
                            task_id,
                            status="done",
                            result=(result_text or "")[:_BG_RESULT_PREVIEW_CHARS],
                        )
                    except Exception:
                        pass
                # Log to parent event stream for visibility.
                try:
                    parent._log("bg_task_complete", {
                        "worker": worker_label,
                        "task_id": task_id,
                        "result_preview": (result_text or "")[:_BG_EVENT_PREVIEW_CHARS],
                    })
                except Exception:
                    pass
            except Exception as e:
                if task_id:
                    try:
                        parent.update_task(task_id, status="failed",
                                           result=f"{type(e).__name__}: {e}")
                    except Exception:
                        pass

        th = threading.Thread(target=_run_background, daemon=True,
                              name=f"bg-{worker_label}-{worker_id}")
        th.start()

        return (
            f"Background worker dispatched.\n"
            f"  Role: {role}\n"
            f"  Worker: {worker_label} (id={worker_id})\n"
            f"  Task ID on parent: {task_id or '(none)'}\n"
            f"  Model inherited: {parent.model or '(default)'} @ "
            f"{parent.provider or '(default)'}\n"
            f"The worker runs in background and will post its result back "
            f"to your task list when done. It is NOT a separately managed agent."
        )
    except Exception as e:
        return f"Error dispatching background worker: {e}"


# ── send_message ─────────────────────────────────────────────────────

def _tool_send_message(to_agent: str, content: str,
                       msg_type: str = "task", **_: Any) -> str:
    """Send an inter-agent message."""
    try:
        hub = _get_hub()
        # Resolve agent by name if not an ID.
        target = hub.get_agent(to_agent)
        if target is None:
            # Try finding by name (case-insensitive).
            for a in hub.agents.values():
                if a.name.lower() == to_agent.lower():
                    target = a
                    break
        if target is None:
            available = [f"{a.name} ({a.id})" for a in hub.agents.values()]
            return (
                f"Error: Agent '{to_agent}' not found.\n"
                f"Available agents: {', '.join(available) or 'none'}"
            )
        # Use hub's canonical routing entry point (audited).
        caller_id = _.get("_caller_agent_id", "unknown") if isinstance(_, dict) else "unknown"
        route = getattr(hub, "route_message", None)
        if callable(route):
            route(caller_id, target.id, content, msg_type=msg_type,
                  source="tool_send_message")
        else:
            hub.send_message(caller_id, target.id, content, msg_type=msg_type)
        return (
            f"Message sent to {target.name} ({target.id}).\n"
            f"  Type: {msg_type}\n"
            f"  Content: {content[:_SEND_MESSAGE_PREVIEW_CHARS]}"
        )
    except Exception as e:
        return f"Error sending message: {e}"


# ── task_update ──────────────────────────────────────────────────────

def _parse_run_at(run_at: str) -> float:
    """Parse run_at spec into a unix timestamp.

    Supported formats:
      '+5m'   → 5 minutes from now
      '+2h'   → 2 hours from now
      '18:30' → today at 18:30 (or tomorrow if already past)
    Returns 0.0 on failure.
    """
    run_at = run_at.strip()
    if not run_at:
        return 0.0
    # Relative: +Nm / +Nh / +Ns
    m = re.match(r'^\+(\d+)\s*([mMhHsS])$', run_at)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        delta = {'m': timedelta(minutes=val), 'h': timedelta(hours=val),
                 's': timedelta(seconds=val)}[unit]
        return (datetime.now() + delta).timestamp()
    # Absolute: HH:MM
    m = re.match(r'^(\d{1,2}):(\d{2})$', run_at)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return 0.0


def _tool_task_update(action: str, task_id: str = "", title: str = "",
                      description: str = "", status: str = "",
                      result: str = "",
                      recurrence: str = "once",
                      recurrence_spec: str = "",
                      run_at: str = "", **_: Any) -> str:
    """Manage the shared task list and register with scheduler for execution."""
    try:
        hub = _get_hub()
        # Use the calling agent's task list.
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        agent = hub.get_agent(caller_id) if caller_id else None

        if action == "list":
            all_tasks = []
            for a in hub.agents.values():
                for t in a.tasks:
                    all_tasks.append(
                        f"  [{t.status.value:>11}] {t.id}: {t.title} "
                        f"(agent: {a.name})"
                    )
            if not all_tasks:
                return "No tasks found."
            return (f"Shared task list ({len(all_tasks)} tasks):\n"
                    + "\n".join(all_tasks))

        if action == "create":
            if not title:
                return "Error: 'title' is required for create action."
            rec = (recurrence or "once").lower()
            agent_id = caller_id or (agent.id if agent else "")

            # ── Meeting-scope routing ──
            # If this tool is being called from within a meeting reply loop,
            # route the task to the Standalone Task registry (tagged with
            # meeting_id) instead of creating a scheduled job. Keeps meeting
            # discussions' "action items" out of the scheduler while still
            # making them visible in 任务中心 and traceable to the meeting.
            try:
                from ..meeting_context import get_meeting_context
                _meeting_id = get_meeting_context()
            except Exception:
                _meeting_id = ""
            if _meeting_id and getattr(hub, "standalone_task_registry", None):
                try:
                    st = hub.standalone_task_registry.create(
                        title=title,
                        description=description or "",
                        assigned_to=agent_id,
                        created_by=agent_id,
                        priority="normal",
                        due_hint=run_at or recurrence_spec or "",
                        source_meeting_id=_meeting_id,
                        tags=["from_meeting"],
                    )
                    return (
                        f"Task created (standalone, from meeting): {st.id} — {title}"
                        f" [source_meeting_id={_meeting_id}]"
                    )
                except Exception as _st_err:
                    logger.warning(
                        "Standalone-task routing failed for meeting %s: %s. "
                        "Falling through to default scheduler path.",
                        _meeting_id, _st_err,
                    )

            # Route through agent.add_task so recurrence / next_run_at is computed.
            if agent:
                new_task = agent.add_task(
                    title=title,
                    description=description,
                    assigned_by=caller_id or "system",
                    source="agent_chat",
                    recurrence=rec,
                    recurrence_spec=recurrence_spec or "",
                )
            else:
                # Fallback: no agent context — create plain task.
                from ..agent import AgentTask, TaskStatus
                new_task = AgentTask(
                    title=title,
                    description=description,
                    status=TaskStatus(status) if status else TaskStatus.TODO,
                    assigned_by=caller_id or "system",
                    recurrence=rec,
                    recurrence_spec=recurrence_spec or "",
                )

            # ── Register with TaskScheduler for actual execution ──
            # This is the critical bridge: AgentTask → ScheduledJob.
            #
            # GUARD: When the agent is running INSIDE a scheduled task,
            # block it from creating new scheduled jobs. Otherwise
            # "please generate daily report" prompts cause the agent to
            # create duplicate recurring jobs on every execution.
            _in_scheduled = getattr(agent, '_scheduled_context', False) if agent else False
            if _in_scheduled and (rec != "once" or run_at):
                return (
                    f"Task created: {new_task.id} — {title}"
                    f" [NOTE: scheduler registration skipped — "
                    f"you are already running inside a scheduled job]"
                )

            scheduler_msg = ""
            try:
                from ..scheduler import get_scheduler, recurrence_to_cron
                scheduler = get_scheduler()

                if rec != "once":
                    # Recurring task → register as recurring scheduler job.
                    cron_expr = recurrence_to_cron(rec, recurrence_spec or "")
                    if cron_expr and scheduler and agent_id:
                        job = scheduler.add_job(
                            agent_id=agent_id,
                            name=title,
                            prompt_template=description or title,
                            job_type="recurring",
                            cron_expr=cron_expr,
                        )
                        nxt = datetime.fromtimestamp(
                            job.next_run_at).strftime("%Y-%m-%d %H:%M")
                        scheduler_msg = (
                            f" [SCHEDULED: recurring {rec} @ "
                            f"{recurrence_spec or 'default'}, "
                            f"next run: {nxt}, job_id: {job.id}]")

                elif run_at:
                    # One-time delayed task → register as one_time scheduler job.
                    run_ts = _parse_run_at(run_at)
                    if run_ts > 0 and scheduler and agent_id:
                        job = scheduler.add_job(
                            agent_id=agent_id,
                            name=title,
                            prompt_template=description or title,
                            job_type="one_time",
                            cron_expr="* * * * *",  # placeholder
                            next_run_at=run_ts,
                        )
                        nxt = datetime.fromtimestamp(
                            run_ts).strftime("%Y-%m-%d %H:%M")
                        scheduler_msg = (
                            f" [SCHEDULED: one-time at {nxt}, "
                            f"job_id: {job.id}]")
                    elif run_ts <= 0:
                        scheduler_msg = (
                            f" [WARNING: could not parse run_at='{run_at}', "
                            f"task created but NOT scheduled]")

            except Exception as sched_err:
                logger.warning("Failed to register task with scheduler: %s",
                               sched_err)
                scheduler_msg = f" [scheduler registration failed: {sched_err}]"

            return f"Task created: {new_task.id} — {title}{scheduler_msg}"

        if action in ("update", "complete"):
            if not task_id:
                return "Error: 'task_id' is required for update/complete."
            from ..agent import TaskStatus
            # Find the task across all agents.
            for a in hub.agents.values():
                for t in a.tasks:
                    if t.id == task_id:
                        if action == "complete":
                            t.status = TaskStatus.DONE
                            t.result = result or "Completed"
                        elif status:
                            t.status = TaskStatus(status)
                        if description:
                            t.description = description
                        t.updated_at = time.time()
                        return f"Task {task_id} updated: status={t.status.value}"
            return f"Error: Task '{task_id}' not found."

        return f"Error: Unknown action '{action}'. Use: create | update | complete | list"
    except Exception as e:
        return f"Error managing tasks: {e}"
