"""Project management router — CRUD, tasks, milestones, goals, deliverables."""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.projects")

router = APIRouter(prefix="/api/portal", tags=["projects"])


def _get_project_or_404(hub, project_id: str):
    """Get project or raise 404."""
    try:
        project = hub.get_project(project_id) if hasattr(hub, "get_project") else None
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
        return project
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Project listing and CRUD
# ---------------------------------------------------------------------------

@router.get("/projects")
async def list_projects(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all projects."""
    try:
        projects = hub.list_projects() if hasattr(hub, "list_projects") else []
        projects_list = [p.to_dict() if hasattr(p, "to_dict") else p for p in projects]
        return {"projects": projects_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects")
async def manage_projects(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create, update, or delete a project."""
    try:
        action = body.get("action", "create")

        if action == "create":
            if not hasattr(hub, "create_project"):
                raise HTTPException(500, "hub.create_project not available")
            # Validation: require a real name. Default "New Project" is
            # a placeholder, not a project. Reject so the DB doesn't
            # fill with placeholder rows.
            name = (body.get("name") or "").strip()
            if not name:
                raise HTTPException(400, "name is required (non-empty)")
            if name.lower() in ("new project", "project", "untitled"):
                raise HTTPException(400,
                    "name is too generic — pick something meaningful")
            project = hub.create_project(
                name=name,
                description=body.get("description", ""),
                member_configs=body.get("members", []),
                working_directory=body.get("working_directory", ""),
                node_id=body.get("node_id", "local"),
                workflow_id=body.get("workflow_id", ""),
                step_assignments=body.get("step_assignments", []),
            )
            return {"ok": True, "project": project.to_dict() if hasattr(project, "to_dict") else project}
        elif action == "update":
            project = hub.update_project(body.get("project_id"), body) if hasattr(hub, "update_project") else None
            return {
                "ok": True,
                "project": project.to_dict() if hasattr(project, "to_dict") else (project or {}),
            }
        elif action == "delete":
            pid = body.get("project_id", "")
            if not pid:
                raise HTTPException(400, "project_id required")
            if hasattr(hub, "remove_project"):
                hub.remove_project(pid)
            elif hasattr(hub, "delete_project"):
                hub.delete_project(pid)
            # Cascade: SQLite row + projects/*{id}*.md docs.
            try:
                from ...infra.database import get_database
                db = get_database()
                if db and hasattr(db, "delete_project"):
                    db.delete_project(pid)
            except Exception as _e:
                logger.warning("projects DB delete failed: %s", _e)
            try:
                import os, glob
                from ... import DEFAULT_DATA_DIR
                data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
                for md in glob.glob(os.path.join(data_dir, "projects", f"*{pid}*.md")):
                    os.remove(md)
            except Exception as _e:
                logger.warning("projects md cleanup failed: %s", _e)
            return {"ok": True}
        else:
            raise HTTPException(400, f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("manage_projects failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """RESTful alias for ``POST /projects {action: delete}``.
    Removes the project + SQLite row + ``projects/*{id}*.md`` docs."""
    project = hub.get_project(project_id) if hasattr(hub, "get_project") else None
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    fn = getattr(hub, "remove_project", None) or getattr(hub, "delete_project", None)
    if fn is None:
        raise HTTPException(501, "hub has no project delete method")
    ok = bool(fn(project_id))
    try:
        from ...infra.database import get_database
        db = get_database()
        if db and hasattr(db, "delete_project"):
            db.delete_project(project_id)
    except Exception as _e:
        logger.warning("projects DB delete failed: %s", _e)
    try:
        import os, glob
        from ... import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
        for md in glob.glob(os.path.join(data_dir, "projects", f"*{project_id}*.md")):
            os.remove(md)
    except Exception as _e:
        logger.warning("projects md cleanup failed: %s", _e)
    return {"ok": ok, "deleted": project_id}


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project detail."""
    try:
        project = _get_project_or_404(hub, project_id)
        data = project.to_dict() if hasattr(project, "to_dict") else project
        return data if isinstance(data, dict) else {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/tasks")
async def get_project_tasks(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project tasks."""
    try:
        project = _get_project_or_404(hub, project_id)
        tasks = project.tasks if hasattr(project, "tasks") else []
        tasks_list = [t.to_dict() if hasattr(t, "to_dict") else t for t in tasks]
        return {"tasks": tasks_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/tasks")
async def assign_task(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Assign or create a task in the project."""
    try:
        project = _get_project_or_404(hub, project_id)
        task = project.create_task(body) if hasattr(project, "create_task") else {}
        return {"ok": True, "task": task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/milestones")
async def get_project_milestones(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project milestones."""
    try:
        project = _get_project_or_404(hub, project_id)
        milestones = project.milestones if hasattr(project, "milestones") else []
        milestones_list = [m.to_dict() if hasattr(m, "to_dict") else m for m in milestones]
        return {"milestones": milestones_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/milestones")
async def create_milestone(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a milestone in the project."""
    try:
        project = _get_project_or_404(hub, project_id)
        milestone = project.create_milestone(body) if hasattr(project, "create_milestone") else {}
        return {"ok": True, "milestone": milestone}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/goals")
async def get_project_goals(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project goals."""
    try:
        project = _get_project_or_404(hub, project_id)
        goals = project.goals if hasattr(project, "goals") else []
        goals_list = [g.to_dict() if hasattr(g, "to_dict") else g for g in goals]
        return {"goals": goals_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Deliverables
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/deliverables")
async def get_project_deliverables(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project deliverables."""
    try:
        project = _get_project_or_404(hub, project_id)
        deliverables = project.deliverables if hasattr(project, "deliverables") else []
        deliverables_list = [d.to_dict() if hasattr(d, "to_dict") else d for d in deliverables]
        return {"deliverables": deliverables_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/issues")
async def get_project_issues(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project issues."""
    try:
        project = _get_project_or_404(hub, project_id)
        issues = project.issues if hasattr(project, "issues") else []
        issues_list = [i.to_dict() if hasattr(i, "to_dict") else i for i in issues]
        return {"issues": issues_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Project overview and management
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/overview")
async def get_project_overview(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project overview (summary stats).

    Project has no dedicated get_overview(); to_dict() already carries every
    field the frontend reads (goal_summary/deliverable_summary/issue_summary/
    task_summary + goals/deliverables/issues lists). We just reshape it to
    match the legacy stdlib handler's response.
    """
    try:
        project = _get_project_or_404(hub, project_id)
        d = project.to_dict()
        return {
            "project": {
                "id": d["id"], "name": d["name"],
                "description": d["description"], "status": d["status"],
                "members": d["members"],
                "working_directory": d["working_directory"],
                "node_id": d["node_id"],
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
            },
            "goals": d.get("goals", []),
            "goal_summary": d.get("goal_summary", {}),
            "milestones": d.get("milestones", []),
            "deliverables": d.get("deliverables", []),
            "deliverable_summary": d.get("deliverable_summary", {}),
            "issues": d.get("issues", []),
            "issue_summary": d.get("issue_summary", {}),
            "task_summary": d.get("task_summary", {}),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/status")
async def update_project_status(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update project status."""
    try:
        project = _get_project_or_404(hub, project_id)
        status = body.get("status", "")
        if hasattr(project, "set_status"):
            project.set_status(status)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/members")
async def manage_project_members(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Add or remove project members."""
    try:
        project = _get_project_or_404(hub, project_id)
        action = body.get("action", "add")
        member_id = body.get("member_id", "")

        if action == "add" and hasattr(project, "add_member"):
            project.add_member(member_id)
        elif action == "remove" and hasattr(project, "remove_member"):
            project.remove_member(member_id)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/chat")
async def send_project_message(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Send a message in project chat.

    Frontend sends ``content`` (primary); legacy clients may send ``message``.
    Persistence is done via ``hub.project_chat(proj_id, content, target_agents)``
    which also fans out to agent respondents. Attachments (list of
    ``{name, mime, size, data_base64}``) are saved under the project working
    directory and their filenames are appended to the content as 📎 refs.
    """
    try:
        project = _get_project_or_404(hub, project_id)

        # Primary field is `content` (frontend); tolerate legacy `message`.
        content = (body.get("content") or body.get("message") or "").strip()
        target_agents = body.get("target_agents")
        attachments = body.get("attachments") or []

        # Persist attachments to project working dir, append 📎refs to content.
        saved_refs: list[str] = []
        if isinstance(attachments, list) and attachments:
            try:
                base_dir = (
                    getattr(project, "working_directory", "")
                    or os.path.join(
                        os.environ.get("TUDOU_CLAW_DATA_DIR", "."),
                        "projects", project_id,
                    )
                )
                att_dir = os.path.join(base_dir, "attachments")
                os.makedirs(att_dir, exist_ok=True)
                MAX_SIZE = 20 * 1024 * 1024  # 20 MB
                for att in attachments[:10]:  # cap at 10 per message
                    if not isinstance(att, dict):
                        continue
                    raw_name = str(att.get("name") or "attachment.bin")
                    safe_name = "".join(
                        c for c in raw_name if c.isalnum() or c in "._-"
                    ) or "attachment.bin"
                    data_b64 = att.get("data_base64") or ""
                    if not data_b64:
                        continue
                    try:
                        data_bytes = base64.b64decode(data_b64)
                    except Exception:
                        continue
                    if len(data_bytes) > MAX_SIZE:
                        continue
                    ts = int(time.time() * 1000)
                    fname = f"{ts}_{safe_name}"
                    fpath = os.path.join(att_dir, fname)
                    try:
                        with open(fpath, "wb") as _f:
                            _f.write(data_bytes)
                    except Exception:
                        continue
                    saved_refs.append(fname)
            except Exception as _ae:
                logger.warning("attachment save failed: %s", _ae)

        if saved_refs:
            suffix = "\n" + " ".join(f"\U0001f4ce{r}" for r in saved_refs)
            content = (content + suffix) if content else suffix.lstrip()

        if not content:
            raise HTTPException(400, "Empty message")

        respondents: list = []
        if hasattr(hub, "project_chat"):
            respondents = hub.project_chat(
                project_id, content, target_agents,
                user_id=getattr(user, "user_id", "") or "",
            ) or []
        elif hasattr(project, "post_message"):
            project.post_message(
                sender="user",
                sender_name=getattr(user, "user_id", "user") or "user",
                content=content,
            )

        return {
            "ok": True,
            "respondents": respondents,
            "attachments_saved": saved_refs,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("send_project_message failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/abort")
async def project_abort(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Hard abort: stop any in-flight project chat / workflow step AND
    SIGTERM any bash subprocesses spawned by agents working on this
    project.

    Uses the centralized abort_registry:
      - Flips abort flag so agent loops exit between turns
      - Kills tracked OS processes (python scripts, compilers, etc.)
      - Appends a system note to project chat so members see it
    """
    try:
        project = _get_project_or_404(hub, project_id)
        from ... import abort_registry as _ar
        result = _ar.abort(_ar.project_key(project.id))
        try:
            killed_n = len(result.get("killed_pids") or [])
            note = "🛑 项目执行已强制终止"
            if killed_n:
                note += f"（已停止 {killed_n} 个子进程）"
            project.post_message(
                sender="system", sender_name="系统",
                content=note, msg_type="system",
            )
            hub._save_projects()
        except Exception:
            pass
        return {"ok": True, "abort": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("project_abort failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/wake")
async def wake_project_agents(
    project_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Manually wake stuck agents in this project.

    Useful when an agent hits the tool-call hard cap, an LLM call
    times out, or the user aborted mid-turn — the agent goes idle
    with unfinished tasks/milestones still owned, but nothing kicks
    them back into action. This endpoint fires a system trigger msg
    (via the unified dispatch) for either:

      * one specific agent  — body: ``{"agent_id": "..."}``
      * all members with pending tasks/milestones — body: ``{}``

    Optional body field ``reason`` is appended to the trigger so the
    agent knows why it's being woken.
    """
    try:
        project = _get_project_or_404(hub, project_id)
        engine = getattr(hub, "project_chat_engine", None)
        if engine is None:
            raise HTTPException(503, "project chat engine not initialized")
        agent_id = (body.get("agent_id") or "").strip()
        reason = (body.get("reason") or "").strip()
        woke = engine.wake_agents(project, agent_id=agent_id, reason=reason)
        try:
            hub._save_projects()
        except Exception:
            pass
        return {"ok": True, "woken": woke,
                "scope": "agent" if agent_id else "all",
                "target_agent_id": agent_id or None}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("wake_project_agents failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/projects/{project_id}/chat")
async def clear_project_chat(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Clear the project's chat — drops:

      1. ``project.chat_history``  — what the UI displays.
      2. For every member agent: their ``project:{project_id}`` bucket
         in ``_messages_by_context`` — so the next LLM turn won't replay
         this project's old messages either.

    Solo / other-project / meeting buckets on each agent are NOT touched.
    Tasks, milestones, deliverables, members are NOT touched (chat-only).
    """
    try:
        project = _get_project_or_404(hub, project_id)
        # 1. Drop UI-visible chat history
        ui_cleared = len(project.chat_history or [])
        project.chat_history = []
        # 2. Drop per-agent project context bucket for every member.
        #    Each agent's other contexts (solo / other projects / meetings)
        #    are untouched — only this project's bucket is removed.
        ctx_key = f"project:{project.id}"
        agents_cleared = 0
        agent_msgs_cleared = 0
        for m in (project.members or []):
            agent = hub.agents.get(getattr(m, "agent_id", "")) if hasattr(hub, "agents") else None
            if agent is None:
                continue
            mbc = getattr(agent, "_messages_by_context", None)
            if not isinstance(mbc, dict) or ctx_key not in mbc:
                continue
            agent_msgs_cleared += len(mbc.get(ctx_key) or [])
            del mbc[ctx_key]
            # If this context was active, fall back to solo so subsequent
            # reads of agent.messages don't dangle on a removed bucket.
            if getattr(agent, "_active_context_id", "") == ctx_key:
                agent._switch_context("solo")
            agents_cleared += 1
        # 3. Persist
        try:
            hub._save_projects()
            hub._save_agents()
        except Exception as _se:
            logger.debug("clear_project_chat: save failed: %s", _se)
        return {
            "ok": True,
            "ui_messages_cleared": ui_cleared,
            "agents_cleared": agents_cleared,
            "agent_messages_cleared": agent_msgs_cleared,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("clear_project_chat failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/chat")
async def get_project_chat(
    project_id: str,
    limit: int = Query(50, ge=1, le=500),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project chat history.

    Also includes ``streaming`` array — for each member that is
    currently mid-turn, surface their in-progress reply text so the
    frontend can render a "typing" preview (option A from the
    2026-04-28 streaming-UX discussion). Polling-based, no SSE.
    """
    try:
        project = _get_project_or_404(hub, project_id)
        msgs = project.get_chat_history(limit=limit) if hasattr(project, "get_chat_history") else []
        msg_dicts = [m.to_dict() if hasattr(m, "to_dict") else m for m in msgs]
        # Enrich with FileCard refs so the frontend renders clickable
        # download cards for artifacts the agent produced. The
        # existing enricher on the stdlib portal route is generic —
        # it doesn't care whether the messages came from a meeting or
        # a project, just that the message dicts have sender+content.
        try:
            from ...server.portal_routes_get import (
                _enrich_meeting_messages_with_refs,
            )
            _enrich_meeting_messages_with_refs(hub, msg_dicts)
        except Exception as _e:
            logger.debug("project chat ref enrichment failed: %s", _e)
        # Streaming preview: for each project member whose agent.chat
        # is mid-turn, expose the in-progress text buffer + an updated_at
        # so the frontend can render a typing bubble that grows.
        streaming: list[dict] = []
        try:
            from ...agent import AgentStatus as _AS
            for m in (project.members or []):
                a = hub.agents.get(m.agent_id) if hasattr(hub, "agents") else None
                if a is None:
                    continue
                buf = getattr(a, "_streaming_buffer", "") or ""
                if not buf:
                    continue
                # Only surface when the agent is actually busy — a
                # leftover buffer from a finished turn shouldn't show.
                if getattr(a, "status", None) != _AS.BUSY:
                    continue
                streaming.append({
                    "agent_id": a.id,
                    "sender_name": f"{a.role}-{a.name}" if a.role else a.name,
                    "content": buf,
                    "char_count": len(buf),
                })
        except Exception as _se:
            logger.debug("project streaming-preview build failed: %s", _se)
        return {"messages": msg_dicts, "streaming": streaming}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Task updates, steps, checkpoints
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/task-update")
async def update_task(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update an existing task."""
    try:
        proj = _get_project_or_404(hub, project_id)
        task_id = body.get("task_id", "")
        updates = {k: v for k, v in body.items() if k != "task_id"}
        task = proj.update_task(task_id, **updates) if hasattr(proj, "update_task") else None
        if not task:
            raise HTTPException(404, "Task not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "task": task.to_dict() if hasattr(task, "to_dict") else task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/task-steps")
async def define_task_steps(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Define or replace task step list."""
    try:
        proj = _get_project_or_404(hub, project_id)
        task_id = body.get("task_id", "")
        steps = body.get("steps", [])
        task = next((t for t in proj.tasks if t.id == task_id), None) if hasattr(proj, "tasks") else None
        if not task:
            raise HTTPException(404, "Task not found")
        if not isinstance(steps, list):
            raise HTTPException(400, "steps must be a list")
        # Normalize step items
        normalized = []
        for it in steps:
            if isinstance(it, str):
                normalized.append({"name": it, "manual_review": False})
            elif isinstance(it, dict) and "name" in it:
                normalized.append({"name": str(it["name"]), "manual_review": bool(it.get("manual_review", False))})
            else:
                raise HTTPException(400, "each step must be str or {name, manual_review}")
        from app.project import TaskStep
        prev_done = {s.name: s for s in (task.steps or []) if s.status in ("done", "skipped")}
        new_steps = []
        for it in normalized:
            n = it["name"]
            if n in prev_done:
                s = prev_done[n]
                s.manual_review = it["manual_review"]
                new_steps.append(s)
            else:
                new_steps.append(TaskStep(name=n, manual_review=it["manual_review"]))
        task.steps = new_steps
        task.current_step_index = 0
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "task": task.to_dict() if hasattr(task, "to_dict") else task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/task-step-review")
async def review_task_step(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Human review (approve/reject) of a task step."""
    try:
        proj = _get_project_or_404(hub, project_id)
        task_id = body.get("task_id", "")
        step_id = body.get("step_id", "")
        action = body.get("action", "approve")
        task = next((t for t in proj.tasks if t.id == task_id), None) if hasattr(proj, "tasks") else None
        if not task:
            raise HTTPException(404, "Task not found")
        step = next((s for s in (task.steps or []) if s.id == step_id), None)
        if not step:
            raise HTTPException(404, "Step not found")
        if not step.manual_review:
            raise HTTPException(400, "Step is not flagged for manual review")
        if step.status != "awaiting_review":
            raise HTTPException(400, f"Step is not awaiting review (current: {step.status})")
        reviewer = user.user_id if hasattr(user, "user_id") else "user"
        if action == "approve":
            ok = task.approve_step(step, reviewer_id=str(reviewer), override_result=body.get("result", ""))
            if not ok:
                raise HTTPException(400, "Approve failed")
        elif action == "reject":
            ok = task.reject_step(step, reviewer_id=str(reviewer), reason=body.get("reason", ""))
            if not ok:
                raise HTTPException(400, "Reject failed")
        else:
            raise HTTPException(400, "action must be approve|reject")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "task": task.to_dict() if hasattr(task, "to_dict") else task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/task-checkpoint")
async def task_checkpoint(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Manual step checkpoint (done/failed/skipped)."""
    try:
        proj = _get_project_or_404(hub, project_id)
        task_id = body.get("task_id", "")
        step_id = body.get("step_id", "")
        new_status = body.get("status", "done")
        task = next((t for t in proj.tasks if t.id == task_id), None) if hasattr(proj, "tasks") else None
        if not task:
            raise HTTPException(404, "Task not found")
        step = next((s for s in (task.steps or []) if s.id == step_id), None)
        if not step:
            raise HTTPException(404, "Step not found")
        if new_status == "failed":
            task.complete_step(step, error=body.get("error", "manual fail"))
        elif new_status == "skipped":
            step.status = "skipped"
        else:
            task.complete_step(step, result=body.get("result", ""))
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "task": task.to_dict() if hasattr(task, "to_dict") else task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Milestone mutations
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/milestones/{milestone_id}/update")
async def update_milestone(
    project_id: str,
    milestone_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a milestone."""
    try:
        proj = _get_project_or_404(hub, project_id)
        updates = {k: v for k, v in body.items() if k != "milestone_id"}
        ms = proj.update_milestone(milestone_id, **updates) if hasattr(proj, "update_milestone") else None
        if not ms:
            raise HTTPException(404, "Milestone not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "milestone": ms.to_dict() if hasattr(ms, "to_dict") else ms}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/milestones/{milestone_id}/confirm")
async def confirm_milestone(
    project_id: str,
    milestone_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Confirm a milestone."""
    try:
        proj = _get_project_or_404(hub, project_id)
        by = user.user_id if hasattr(user, "user_id") else "admin"
        ms = proj.confirm_milestone(milestone_id, by=by) if hasattr(proj, "confirm_milestone") else None
        if not ms:
            raise HTTPException(404, "Milestone not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "milestone": ms.to_dict() if hasattr(ms, "to_dict") else ms}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/milestones/{milestone_id}/reject")
async def reject_milestone(
    project_id: str,
    milestone_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Reject a milestone."""
    try:
        proj = _get_project_or_404(hub, project_id)
        reason = body.get("reason", "")
        by = user.user_id if hasattr(user, "user_id") else "admin"
        ms = proj.reject_milestone(milestone_id, reason=reason, by=by) if hasattr(proj, "reject_milestone") else None
        if not ms:
            raise HTTPException(404, "Milestone not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "milestone": ms.to_dict() if hasattr(ms, "to_dict") else ms}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Goal mutations
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/goals")
async def add_goal(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Add a goal to the project."""
    try:
        proj = _get_project_or_404(hub, project_id)
        g = proj.add_goal(
            name=body.get("name", ""),
            description=body.get("description", ""),
            owner_agent_id=body.get("owner_agent_id", ""),
            metric=body.get("metric", "count"),
            target_value=float(body.get("target_value", 0) or 0),
            target_text=body.get("target_text", ""),
        ) if hasattr(proj, "add_goal") else None
        if not g:
            raise HTTPException(500, "Failed to add goal")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "goal": g.to_dict() if hasattr(g, "to_dict") else g}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/goals/{goal_id}/update")
async def update_goal(
    project_id: str,
    goal_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a goal."""
    try:
        proj = _get_project_or_404(hub, project_id)
        allowed = {k: v for k, v in body.items()
                   if k in ("name", "description", "owner_agent_id", "metric",
                            "target_value", "current_value", "target_text", "done",
                            "linked_milestone_ids", "linked_deliverable_ids")}
        g = proj.update_goal(goal_id, **allowed) if hasattr(proj, "update_goal") else None
        if not g:
            raise HTTPException(404, "Goal not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "goal": g.to_dict() if hasattr(g, "to_dict") else g}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/goals/{goal_id}/progress")
async def update_goal_progress(
    project_id: str,
    goal_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update goal progress."""
    try:
        proj = _get_project_or_404(hub, project_id)
        g = proj.update_goal_progress(
            goal_id,
            current_value=float(body["current_value"]) if "current_value" in body else None,
            done=bool(body["done"]) if "done" in body else None,
        ) if hasattr(proj, "update_goal_progress") else None
        if not g:
            raise HTTPException(404, "Goal not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "goal": g.to_dict() if hasattr(g, "to_dict") else g}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/goals/{goal_id}/delete")
async def delete_goal(
    project_id: str,
    goal_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a goal."""
    try:
        proj = _get_project_or_404(hub, project_id)
        ok = proj.remove_goal(goal_id) if hasattr(proj, "remove_goal") else False
        if ok and hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Deliverable mutations
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/deliverables")
async def add_deliverable(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Add a deliverable to the project."""
    try:
        proj = _get_project_or_404(hub, project_id)
        dv = proj.add_deliverable(
            title=body.get("title", ""),
            kind=body.get("kind", "document"),
            author_agent_id=body.get("author_agent_id", ""),
            task_id=body.get("task_id", ""),
            milestone_id=body.get("milestone_id", ""),
            content_text=body.get("content_text", ""),
            file_path=body.get("file_path", ""),
            url=body.get("url", ""),
        ) if hasattr(proj, "add_deliverable") else None
        if not dv:
            raise HTTPException(500, "Failed to add deliverable")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "deliverable": dv.to_dict() if hasattr(dv, "to_dict") else dv}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/deliverables/{deliverable_id}/update")
async def update_deliverable(
    project_id: str,
    deliverable_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a deliverable."""
    try:
        proj = _get_project_or_404(hub, project_id)
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "kind", "content_text", "file_path", "url",
                            "task_id", "milestone_id")}
        dv = proj.update_deliverable(deliverable_id, **allowed) if hasattr(proj, "update_deliverable") else None
        if not dv:
            raise HTTPException(404, "Deliverable not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "deliverable": dv.to_dict() if hasattr(dv, "to_dict") else dv}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/deliverables/{deliverable_id}/submit")
async def submit_deliverable(
    project_id: str,
    deliverable_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Submit a deliverable for review."""
    try:
        proj = _get_project_or_404(hub, project_id)
        dv = proj.submit_deliverable(deliverable_id) if hasattr(proj, "submit_deliverable") else None
        if not dv:
            raise HTTPException(404, "Deliverable not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "deliverable": dv.to_dict() if hasattr(dv, "to_dict") else dv}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/deliverables/{deliverable_id}/review")
async def review_deliverable(
    project_id: str,
    deliverable_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Review (approve/reject) a deliverable."""
    try:
        proj = _get_project_or_404(hub, project_id)
        approved = bool(body.get("approved", False))
        comment = body.get("comment", "") or ""
        reviewer = user.user_id if hasattr(user, "user_id") else "admin"
        dv = proj.review_deliverable(
            deliverable_id, approved=approved, reviewer=reviewer, comment=comment,
        ) if hasattr(proj, "review_deliverable") else None
        if not dv:
            raise HTTPException(404, "Deliverable not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "deliverable": dv.to_dict() if hasattr(dv, "to_dict") else dv}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/deliverables/{deliverable_id}/delete")
async def delete_deliverable(
    project_id: str,
    deliverable_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a deliverable."""
    try:
        proj = _get_project_or_404(hub, project_id)
        ok = proj.remove_deliverable(deliverable_id) if hasattr(proj, "remove_deliverable") else False
        if ok and hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Issue mutations
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/issues")
async def add_issue(
    project_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Add an issue to the project."""
    try:
        proj = _get_project_or_404(hub, project_id)
        reporter = body.get("reporter", "") or (user.user_id if hasattr(user, "user_id") else "user")
        iss = proj.add_issue(
            title=body.get("title", ""),
            description=body.get("description", ""),
            severity=body.get("severity", "medium"),
            reporter=reporter,
            assigned_to=body.get("assigned_to", ""),
            related_task_id=body.get("related_task_id", ""),
            related_milestone_id=body.get("related_milestone_id", ""),
        ) if hasattr(proj, "add_issue") else None
        if not iss:
            raise HTTPException(500, "Failed to add issue")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "issue": iss.to_dict() if hasattr(iss, "to_dict") else iss}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/issues/{issue_id}/update")
async def update_issue(
    project_id: str,
    issue_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update an issue."""
    try:
        proj = _get_project_or_404(hub, project_id)
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "description", "severity", "status",
                            "assigned_to", "related_task_id", "related_milestone_id")}
        iss = proj.update_issue(issue_id, **allowed) if hasattr(proj, "update_issue") else None
        if not iss:
            raise HTTPException(404, "Issue not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "issue": iss.to_dict() if hasattr(iss, "to_dict") else iss}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/issues/{issue_id}/resolve")
async def resolve_issue(
    project_id: str,
    issue_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Resolve an issue."""
    try:
        proj = _get_project_or_404(hub, project_id)
        iss = proj.resolve_issue(
            issue_id,
            resolution=body.get("resolution", ""),
            status=body.get("status", "resolved"),
        ) if hasattr(proj, "resolve_issue") else None
        if not iss:
            raise HTTPException(404, "Issue not found")
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "issue": iss.to_dict() if hasattr(iss, "to_dict") else iss}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/issues/{issue_id}/delete")
async def delete_issue(
    project_id: str,
    issue_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete an issue."""
    try:
        proj = _get_project_or_404(hub, project_id)
        ok = proj.remove_issue(issue_id) if hasattr(proj, "remove_issue") else False
        if ok and hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workflow step approval
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/tasks/{task_id}/approve-step")
async def approve_workflow_step(
    project_id: str,
    task_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Approve a workflow step for execution."""
    try:
        proj = _get_project_or_404(hub, project_id)
        task = next((t for t in proj.tasks if t.id == task_id), None) if hasattr(proj, "tasks") else None
        if not task:
            raise HTTPException(404, "Task not found")
        md = getattr(task, "metadata", None) or {}
        if not md.get("pending_approval"):
            raise HTTPException(400, "Task is not awaiting approval")
        approver = user.user_id if hasattr(user, "user_id") else "admin"
        try:
            task.metadata["pending_approval"] = False
            task.metadata["approved_by"] = approver
        except Exception:
            pass
        # Mark as in-progress
        try:
            from app.project import ProjectTaskStatus
            task.status = ProjectTaskStatus.IN_PROGRESS
        except Exception:
            task.status = "in_progress"
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "task": task.to_dict() if hasattr(task, "to_dict") else task}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Project lifecycle: pause / resume
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/pause")
async def pause_project(
    project_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Pause a project."""
    try:
        proj = _get_project_or_404(hub, project_id)
        by = user.user_id if hasattr(user, "user_id") else "admin"
        reason = body.get("reason", "")
        if hasattr(proj, "pause"):
            proj.pause(by=by, reason=reason)
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "paused": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/resume")
async def resume_project(
    project_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Resume a paused project."""
    try:
        proj = _get_project_or_404(hub, project_id)
        by = user.user_id if hasattr(user, "user_id") else "admin"
        if hasattr(proj, "resume"):
            proj.resume(by=by)
        if hasattr(hub, "_save_projects"):
            hub._save_projects()
        return {"ok": True, "paused": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Standalone tasks (cross-project task registry)
# ---------------------------------------------------------------------------

@router.post("/standalone-tasks")
async def create_standalone_task(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a standalone task.

    Gate: title is required (non-empty). Rejecting placeholder titles
    prevents the dashboard from filling with empty "独立任务" rows.
    """
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title is required (non-empty)")
    if title.lower() in ("new task", "task", "untitled", "独立任务"):
        raise HTTPException(400,
            "title is too generic — pick something meaningful")
    try:
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is None:
            raise HTTPException(503, "standalone task registry not initialized")
        actor = user.user_id if hasattr(user, "user_id") else "admin"
        t = reg.create(
            title=title,
            description=body.get("description", ""),
            assigned_to=body.get("assigned_to", ""),
            created_by=body.get("created_by", "") or actor,
            priority=body.get("priority", "normal"),
            due_hint=body.get("due_hint", ""),
            tags=body.get("tags", []) or [],
            source_meeting_id=body.get("source_meeting_id", ""),
        )
        return t.to_dict() if hasattr(t, "to_dict") else {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/standalone-tasks")
async def list_standalone_tasks(
    assignee: str = Query("", description="Filter by assignee agent ID"),
    status: str = Query("", description="Filter by status"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List standalone tasks."""
    try:
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is None:
            return {"tasks": []}
        items = reg.list(
            assignee=assignee or None,
            status=status or None,
        )
        return {"tasks": [t.to_dict() for t in items]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/standalone-tasks/{task_id}")
async def update_standalone_task(
    task_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a standalone task."""
    try:
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is None:
            raise HTTPException(503, "standalone task registry not initialized")
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "description", "assigned_to", "status",
                            "priority", "due_hint", "tags", "result")}
        t = reg.update(task_id, **allowed)
        if t:
            return t.to_dict() if hasattr(t, "to_dict") else {"ok": True}
        raise HTTPException(404, "Standalone task not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/standalone-tasks/{task_id}/delete")
async def delete_standalone_task(
    task_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a standalone task."""
    try:
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is None:
            raise HTTPException(503, "standalone task registry not initialized")
        ok = reg.delete(task_id)
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Template library
# ---------------------------------------------------------------------------

@router.post("/templates")
async def manage_templates(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Template library management (create, update, delete).

    2026-05-03 fix: was calling hub.{create,update,delete}_template
    which the Hub never exposed → every request returned 501. The
    real implementation lives on TemplateLibrary (singleton via
    get_template_library()), with method names add/update/remove.
    Map them here.
    """
    try:
        from ...template_library import get_template_library
        lib = get_template_library()
        action = body.get("action", "create")

        if action == "create":
            tmpl = lib.add_template(
                name=body.get("name", ""),
                content=body.get("content", ""),
                description=body.get("description", ""),
                roles=body.get("roles", []),
                tags=body.get("tags", []),
                category=body.get("category", "general"),
                created_by=getattr(user, "user_id", "") or "admin",
            )
            return {"ok": True, "template": tmpl.to_dict(include_content=True)}

        if action == "update":
            tmpl_id = body.get("id", "")
            if not tmpl_id:
                raise HTTPException(400, "missing template id")
            kwargs = {k: v for k, v in body.items()
                      if k in ("name", "content", "description", "roles", "tags") and v is not None}
            # `category` isn't accepted by update_template (file-on-disk
            # would also need to move) — silently drop to keep the
            # endpoint forgiving.
            tmpl = lib.update_template(tmpl_id, **kwargs)
            if tmpl:
                return {"ok": True, "template": tmpl.to_dict(include_content=True)}
            raise HTTPException(404, "Template not found")

        if action == "delete":
            tmpl_id = body.get("id", "")
            if not tmpl_id:
                raise HTTPException(400, "missing template id")
            ok = lib.remove_template(tmpl_id)
            if not ok:
                raise HTTPException(404, "Template not found")
            return {"ok": True}

        raise HTTPException(400, f"Unknown template action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/templates")
async def list_templates(
    role: str = Query("", description="Filter by role"),
    category: str = Query("", description="Filter by category"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all templates — matches legacy portal_routes_get."""
    try:
        from ...template_library import get_template_library
        lib = get_template_library()
        templates = lib.list_templates(role=role, category=category)
        return {"templates": [t.to_dict() for t in templates]}
    except (ImportError, Exception) as e:
        return {"templates": []}


@router.get("/templates/{template_id}")
async def get_template(
    template_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single template with full content — matches legacy."""
    try:
        from ...template_library import get_template_library
        lib = get_template_library()
        tpl = lib.get_template(template_id)
        if not tpl:
            raise HTTPException(status_code=404, detail="Template not found")
        return tpl.to_dict(include_content=True)
    except HTTPException:
        raise
    except (ImportError, Exception) as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Deliverables by agent
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/deliverables-by-agent")
async def get_deliverables_by_agent(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get project deliverables grouped by member agent.

    Response contract (consumed by ``_renderProjectDeliverables`` in
    ``portal_bundle.js``)::

        {
          "agents": [
            {
              "agent_id": "...",
              "agent_name": "...",
              "role": "...",
              "responsibility": "...",
              "deliverable_dir": "/path/to/workspace",
              "explicit_deliverables": [dv.to_dict(), ...],
              "files": [{id,name,path,rel_path,kind,mime,size,mtime,url,is_remote}, ...],
              "file_count": int,
              "explicit_count": int,
            }, ...
          ],
          "unassigned_deliverables": [dv.to_dict(), ...],
        }
    """
    proj = _get_project_or_404(hub, project_id)
    try:
        import os as _os
        try:
            from ...agent_state.extractors import scan_deliverable_dir
            from ...agent_state.artifact import ArtifactStore
        except Exception:
            scan_deliverable_dir = None  # type: ignore
            ArtifactStore = None         # type: ignore

        # ── Project-level shared workspace: the single canonical deliverables
        # directory. Layout: ~/.tudou_claw/workspaces/shared/<project_id>/
        from ...agent import Agent as _Agent
        shared_dir = ""
        try:
            shared_dir = _Agent.get_shared_workspace_path(project_id)
        except Exception:
            shared_dir = ""
        shared_base_real = ""
        if shared_dir:
            try:
                shared_base_real = _os.path.realpath(shared_dir)
            except Exception:
                shared_base_real = ""

        def _path_under_shared(p: str) -> bool:
            """True when `p` resolves into the project's shared dir."""
            if not p or not shared_base_real:
                return False
            try:
                if _os.path.isabs(p):
                    real = _os.path.realpath(p)
                else:
                    real = _os.path.realpath(_os.path.join(shared_dir, p))
            except Exception:
                return False
            return real == shared_base_real or real.startswith(
                shared_base_real + _os.sep)

        # Index explicit deliverables by author id.
        # Skip legacy auto-registered entries (📎/markdown scan of agent replies)
        # identified by sentinel content_text="(auto-registered from chat reply)".
        # Also skip explicit deliverables whose file_path points OUTSIDE the
        # project's shared dir — only the shared dir is treated as canonical.
        # Content-only deliverables (no file_path, just content_text/url) pass
        # through unconditionally; those are the 11-style stage submissions.
        AUTO_SENTINEL = "(auto-registered from chat reply)"
        explicit_by_author: dict = {}
        unassigned_explicit: list = []
        for dv in proj.deliverables:
            if (getattr(dv, "content_text", "") or "").strip() == AUTO_SENTINEL:
                continue
            fp = (getattr(dv, "file_path", "") or "").strip()
            if fp and not _path_under_shared(fp):
                continue  # file-referencing deliverable outside shared dir → drop
            aid = (getattr(dv, "author_agent_id", "") or "").strip()
            dv_dict = dv.to_dict() if hasattr(dv, "to_dict") else dv
            if aid:
                explicit_by_author.setdefault(aid, []).append(dv_dict)
            else:
                unassigned_explicit.append(dv_dict)

        agents_out: list = []
        seen_ids: set = set()
        for m in (proj.members or []):
            # m is a ProjectMember dataclass, not a string id
            aid = (getattr(m, "agent_id", "") or "").strip()
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            agent = hub.get_agent(aid) if hasattr(hub, "get_agent") else None
            agent_name = getattr(agent, "name", aid) if agent else aid
            role = getattr(agent, "role", "") if agent else ""

            # The canonical "where this agent should write deliverables" is the
            # project's shared dir, not the agent's private workspace. Report it
            # verbatim so the UI caption is consistent across members.
            explicit_list = explicit_by_author.get(aid, [])
            agents_out.append({
                "agent_id": aid,
                "agent_name": agent_name,
                "role": role,
                "responsibility": getattr(m, "responsibility", "") or "",
                "deliverable_dir": shared_dir,
                "explicit_deliverables": explicit_list,
                "files": [],              # per-agent scan removed (shared at project level)
                "file_count": 0,
                "explicit_count": len(explicit_list),
            })

        # Explicit deliverables whose author isn't a current member → unassigned
        for aid, items in explicit_by_author.items():
            if aid not in seen_ids:
                unassigned_explicit.extend(items)

        # ── Project-level: depth-1 listing of the shared dir.
        # Only direct children (files + folders) are surfaced. Nested files
        # inside subdirectories are intentionally NOT recursed into — they
        # are treated as part of the folder, not as individual deliverables.
        shared_files: list = []
        import mimetypes as _mt
        if shared_dir and _os.path.isdir(shared_dir):
            try:
                for name in _os.listdir(shared_dir):
                    if name.startswith("."):
                        continue  # hide dotfiles
                    full = _os.path.join(shared_dir, name)
                    try:
                        st = _os.stat(full)
                    except OSError:
                        continue
                    is_dir = _os.path.isdir(full)
                    mime, _ = _mt.guess_type(full) if not is_dir else ("", None)
                    shared_files.append({
                        "id": name,
                        "name": name,
                        "path": full,
                        "rel_path": name,
                        "kind": "directory" if is_dir else "file",
                        "mime": mime or ("inode/directory" if is_dir else ""),
                        "size": None if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        # Dirs don't have a download URL — show them as a marker
                        # so users see the folder exists without drilling in.
                        "url": "" if is_dir else f"/workspace/shared/{project_id}/{name}",
                        "is_remote": False,
                        "is_dir": is_dir,
                    })
                # Folders first (alphabetical), then files (newest first)
                shared_files.sort(key=lambda f: (
                    0 if f.get("is_dir") else 1,
                    f.get("name", "").lower() if f.get("is_dir")
                        else -(f.get("mtime") or 0),
                ))
            except Exception:
                shared_files = []

        return {
            "agents": agents_out,
            "unassigned_deliverables": unassigned_explicit,
            "shared_dir": shared_dir,
            "shared_files": shared_files,
            "shared_file_count": len(shared_files),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Long-task subsystem — decomposition draft endpoints
# ---------------------------------------------------------------------------
# Backed by app.long_task.{draft_store,confirm}. Drafts are created by
# the agent-callable ``propose_decomposition`` tool; these endpoints let
# the portal list them, confirm (materialize into ProjectTasks), or
# cancel.

@router.get("/projects/{project_id}/decomposition-drafts")
async def list_decomposition_drafts(
    project_id: str,
    status: Optional[str] = Query(None,
        description="Filter by status: pending|confirmed|cancelled|expired"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List decomposition drafts for a project. Default: all statuses."""
    _get_project_or_404(hub, project_id)
    from ...long_task.draft_store import get_draft_store
    from ...long_task.models import DraftStatus
    store = get_draft_store()
    status_enum = None
    if status:
        try:
            status_enum = DraftStatus(status)
        except ValueError:
            raise HTTPException(400, f"invalid status: {status!r}")
    drafts = store.list_for_project(project_id, status=status_enum)
    return {"drafts": [d.to_dict() for d in drafts]}


@router.get("/projects/{project_id}/decomposition-drafts/{draft_id}")
async def get_decomposition_draft(
    project_id: str,
    draft_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Fetch a single decomposition draft (full sub_tasks + PRD)."""
    _get_project_or_404(hub, project_id)
    from ...long_task.draft_store import get_draft_store
    draft = get_draft_store().get(draft_id)
    if draft is None or draft.project_id != project_id:
        raise HTTPException(404, f"draft {draft_id!r} not found in project")
    return draft.to_dict()


@router.post("/projects/{project_id}/decomposition-drafts/{draft_id}/confirm")
async def confirm_decomposition_draft(
    project_id: str,
    draft_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Materialize a pending draft into real ProjectTasks.

    Body (all optional — empty body confirms as-is):

    .. code-block:: json

       {
         "user_overrides": {
           "dropped_sub_task_ids": ["st_abc"],
           "role_overrides":      {"st_xyz": "researcher"},
           "title_overrides":     {"st_xyz": "Renamed"},
           "output_path_overrides": {"st_xyz": "backend/auth_v2"}
         }
       }
    """
    _get_project_or_404(hub, project_id)
    from ...long_task.confirm import confirm_draft, ConfirmError
    user_overrides = body.get("user_overrides") or {}
    try:
        result = confirm_draft(hub, draft_id, user_overrides=user_overrides)
    except ConfirmError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/projects/{project_id}/decomposition-drafts/{draft_id}/cancel")
async def cancel_decomposition_draft(
    project_id: str,
    draft_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Mark a pending draft as cancelled (no tasks created)."""
    _get_project_or_404(hub, project_id)
    from ...long_task.confirm import cancel_draft
    ok = cancel_draft(draft_id)
    if not ok:
        raise HTTPException(404, "draft not found or not in pending state")
    return {"ok": True, "draft_id": draft_id}


@router.delete("/projects/{project_id}/decomposition-drafts/{draft_id}")
async def delete_decomposition_draft(
    project_id: str,
    draft_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Hard-delete a draft (any status). Use cancel for pending if you
    want to keep the audit trail."""
    _get_project_or_404(hub, project_id)
    from ...long_task.draft_store import get_draft_store
    ok = get_draft_store().delete(draft_id)
    if not ok:
        raise HTTPException(404, "draft not found")
    return {"ok": True, "draft_id": draft_id}
