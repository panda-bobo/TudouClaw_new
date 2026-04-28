"""Capability-based auto-assignment for sub-tasks.

Called periodically from the hub heartbeat. For each project, finds
ready sub-tasks (status=todo + deps satisfied + ``assigned_to`` empty)
and assigns each to an idle agent matching the task's ``role_hint``.

Triggers ``hub.assign_task`` (or the project chat engine's equivalent)
so the existing dispatch path picks them up — we don't reinvent
scheduling here.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("tudouclaw.long_task.auto_assign")


def _get_role(agent) -> str:
    """Return an agent's effective role (lowercased)."""
    return (getattr(agent, "role", "") or "").lower()


def _agent_busy(agent) -> bool:
    """Best-effort check: True if the agent is mid-turn or has any
    in-flight task. Conservative — when in doubt, treat as busy so we
    don't double-book.
    """
    status = getattr(agent, "status", None)
    status_val = getattr(status, "value", status) or ""
    if str(status_val).lower() in ("busy", "working", "running"):
        return True
    # Also block if the agent already has a sub-task in_progress for
    # any project (one big task at a time per agent — keeps reasoning
    # focused and lets the user follow what's happening).
    tasks = getattr(agent, "tasks", None) or []
    for t in tasks:
        st = getattr(t, "status", None)
        st_val = getattr(st, "value", st) or ""
        if str(st_val).lower() in ("in_progress", "running"):
            return True
    return False


def _ready_sub_tasks(project) -> list:
    """Project sub-tasks that are unassigned, todo, with all deps DONE."""
    from ..project import ProjectTaskStatus  # local import to avoid cycle

    tasks_by_id = {t.id: t for t in (project.tasks or [])}
    ready = []
    for t in project.tasks or []:
        # Only sub-tasks (have parent_task_id) qualify for auto-assign;
        # user-created top-level tasks still need explicit assignment.
        if not getattr(t, "parent_task_id", ""):
            continue
        if (t.assigned_to or "").strip():
            continue
        if t.status != ProjectTaskStatus.TODO:
            continue
        deps_ok = True
        for dep_id in (t.depends_on or []):
            dep = tasks_by_id.get(dep_id)
            if dep is None or dep.status != ProjectTaskStatus.DONE:
                deps_ok = False
                break
        if deps_ok:
            ready.append(t)
    return ready


def _success_rate(agent) -> float:
    """Smoothed success rate: (s + 1) / (s + f + 2). New agents (0/0)
    start at 50% rather than NaN; one win pushes to 67%, one loss to
    33%. Caps the data sparsity problem so a single early failure
    doesn't permanently kill an agent's chances."""
    s = int(getattr(agent, "role_success_count", 0) or 0)
    f = int(getattr(agent, "role_fail_count", 0) or 0)
    return (s + 1) / (s + f + 2)


def _candidates_for(role_hint: str, members: list, hub) -> list:
    """Among the project's members, return idle agents whose role
    matches ``role_hint``, **sorted by success rate descending** (P1
    tiebreaker, 2026-04-27). Falls back to "any idle member" if no
    role match — but still sorts by success rate so the user gets the
    best-available agent regardless of role exactness."""
    role_hint = (role_hint or "").lower().strip()
    members_with_agents = []
    for m in members or []:
        agent_id = getattr(m, "agent_id", "") or (m.get("agent_id", "") if isinstance(m, dict) else "")
        if not agent_id:
            continue
        agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
        if agent is None:
            continue
        members_with_agents.append((agent_id, agent))

    role_match = [
        (aid, ag) for aid, ag in members_with_agents
        if _get_role(ag) == role_hint and not _agent_busy(ag)
    ]
    if role_match:
        # Sort: highest success rate first; ties broken by recent
        # activity (more-recently-active agent preferred).
        role_match.sort(
            key=lambda pair: (
                -_success_rate(pair[1]),
                -float(getattr(pair[1], "role_last_success_at", 0) or 0),
            )
        )
        return role_match
    # Fallback: idle members regardless of role, also sorted.
    fallback = [(aid, ag) for aid, ag in members_with_agents if not _agent_busy(ag)]
    fallback.sort(
        key=lambda pair: (
            -_success_rate(pair[1]),
            -float(getattr(pair[1], "role_last_success_at", 0) or 0),
        )
    )
    return fallback


def tick(hub) -> int:
    """One pass over all projects. Returns the number of sub-tasks
    auto-assigned in this tick. Called from the hub heartbeat loop —
    must be cheap and never raise.
    """
    if hub is None or not hasattr(hub, "list_projects"):
        return 0
    assigned_count = 0
    try:
        projects = hub.list_projects() or []
    except Exception as e:  # noqa: BLE001
        logger.debug("auto_assign tick: list_projects failed: %s", e)
        return 0

    for project in projects:
        try:
            ready = _ready_sub_tasks(project)
            if not ready:
                continue
            members = getattr(project, "members", None) or []
            for task in ready:
                role_hint = getattr(task, "role_hint", "") or "general"
                cands = _candidates_for(role_hint, members, hub)
                if not cands:
                    logger.debug(
                        "auto_assign: project=%s task=%s role_hint=%s — "
                        "no idle candidates",
                        project.id, task.id, role_hint,
                    )
                    continue
                # Pick the first candidate; ties don't matter — the
                # heartbeat tick will keep filling as agents free up.
                agent_id, agent = cands[0]
                # Build assignment_reason for the orchestration UI: explains
                # *why* this agent was picked. P0 transparency feature.
                reason = {
                    "ts": __import__("time").time(),
                    "agent_id": agent_id,
                    "agent_name": getattr(agent, "name", "") or agent_id,
                    "role_hint": role_hint,
                    "agent_role": _get_role(agent) or "general",
                    "role_match": _get_role(agent) == (role_hint or "").lower(),
                    "success_rate": round(_success_rate(agent), 3),
                    "candidates_total": len(cands),
                    "runner_ups": [
                        {
                            "name": getattr(a, "name", "") or aid,
                            "success_rate": round(_success_rate(a), 3),
                        }
                        for aid, a in cands[1:3]
                    ],
                }
                _assign(hub, project, task, agent_id, reason)
                assigned_count += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "auto_assign tick failed for project %s: %s",
                getattr(project, "id", "?"), e,
            )
            continue

    if assigned_count > 0:
        logger.info("auto_assign tick: assigned %d sub-task(s)",
                    assigned_count)
    return assigned_count


def _assign(hub, project, task, agent_id: str, reason: dict | None = None) -> None:
    """Set ``task.assigned_to`` and trigger dispatch via the project's
    chat engine. Falls back to direct field write if no dispatch path
    exists (the normal heartbeat will eventually pick it up)."""
    task.assigned_to = agent_id
    if reason is not None:
        # Orchestration UI reads this for the DAG node detail panel.
        try:
            if not isinstance(task.metadata, dict):
                task.metadata = {}
            task.metadata["assignment_reason"] = reason
        except Exception:
            pass

    # Auto-grant shared-context skill on first long-task assignment.
    # Solo conversational agents stay clean; only multi-agent collaborators
    # get the sc_* tool bundle. Once granted, never revoked — the agent
    # has "learned" this toolset for future long-tasks. Best-effort.
    try:
        agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
        if agent is not None:
            granted = list(getattr(agent, "granted_skills", []) or [])
            if "shared-context" not in granted:
                granted.append("shared-context")
                agent.granted_skills = granted
                logger.info(
                    "auto_assign: granted 'shared-context' to agent=%s "
                    "(first long-task assignment)", agent_id,
                )
                if hasattr(hub, "_save_agents"):
                    hub._save_agents()
    except Exception as _ge:
        logger.debug("shared-context auto-grant failed (non-fatal): %s", _ge)

    # Save project state so the assignment survives a restart.
    try:
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
    except Exception:
        pass
    # Try to actively kick the project chat engine so the agent starts
    # immediately rather than waiting for the next user-driven event.
    try:
        engine = getattr(hub, "project_chat_engine", None)
        if engine is not None and hasattr(engine, "handle_task_assignment"):
            engine.handle_task_assignment(project, task)
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "auto_assign: dispatch trigger failed (will rely on next "
            "heartbeat): %s", e,
        )
