"""Meeting management router — list, CRUD, meeting management, file ops."""
from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Body, UploadFile, File
from fastapi.responses import FileResponse

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.meetings")

router = APIRouter(prefix="/api/portal", tags=["meetings"])


# ---------------------------------------------------------------------------
# Meeting listing — matches legacy portal_routes_get
# ---------------------------------------------------------------------------

@router.get("/meetings")
async def list_meetings(
    project_id: str = Query("", description="Filter by project"),
    status: str = Query("", description="Filter by status"),
    participant: str = Query("", description="Filter by participant agent ID"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all meetings."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            return {"meetings": []}
        items = reg.list(
            project_id=project_id or None,
            status=status or None,
            participant=participant or None,
        )
        return {"meetings": [m.to_summary_dict() for m in items]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Single meeting
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting detail."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        return m.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting messages
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}/messages")
async def get_meeting_messages(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting messages."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        msg_dicts = [x.to_dict() for x in m.messages]
        return {"messages": msg_dicts}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting assignments
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}/assignments")
async def get_meeting_assignments(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting assignments."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        return {"assignments": [a.to_dict() for a in m.assignments]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting CRUD
# ---------------------------------------------------------------------------

@router.post("/meetings")
async def manage_meetings(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a meeting."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        title = body.get("title", "")
        if not title:
            raise HTTPException(400, "title is required")
        # Resolve host: use the requesting user's name or first participant
        host = body.get("host", "")
        if not host:
            actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
            host = actor
        meeting = reg.create(
            title=title,
            host=host,
            participants=body.get("participants", []),
            agenda=body.get("agenda", ""),
            project_id=body.get("project_id", ""),
        )
        return {"ok": True, "meeting": meeting.to_dict() if hasattr(meeting, "to_dict") else meeting}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting deletion
# ---------------------------------------------------------------------------

@router.delete("/meetings/{meeting_id}")
async def delete_meeting(
    meeting_id: str,
    purge_workspace: bool = Query(
        True,
        description="Also rm -rf the meeting's workspace directory.",
    ),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a meeting record and (optionally) its workspace directory.

    Meeting messages live inside the meeting document itself, so removing
    the meeting removes its full conversation. The workspace directory
    (``workspaces/meetings/<id>/``) is purged by default — pass
    ``purge_workspace=false`` to keep transcripts on disk.
    """
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")

        meeting = reg.get(meeting_id)
        if meeting is None:
            raise HTTPException(404, f"Meeting {meeting_id!r} not found")

        if not reg.delete(meeting_id):
            raise HTTPException(500, "meeting delete failed")

        purged_ws = False
        if purge_workspace:
            import os, shutil
            from app import DEFAULT_DATA_DIR
            data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
            wsp = os.path.join(data_dir, "workspaces", "meetings", meeting_id)
            if os.path.isdir(wsp):
                try:
                    shutil.rmtree(wsp, ignore_errors=False)
                    purged_ws = True
                except Exception as e:
                    logger.warning("meeting workspace rm failed: %s", e)

        return {
            "ok": True,
            "deleted": meeting_id,
            "purged_workspace": purged_ws,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting management — sub-path routes matching JS client
# ---------------------------------------------------------------------------

def _get_meeting(hub, meeting_id: str):
    """Fetch meeting or raise 404/503."""
    reg = getattr(hub, "meeting_registry", None)
    if reg is None:
        raise HTTPException(503, "meeting registry not initialized")
    m = reg.get(meeting_id)
    if not m:
        raise HTTPException(404, "Meeting not found")
    return reg, m


@router.post("/meetings/{meeting_id}/start")
async def meeting_start(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Start a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.start()
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/close")
async def meeting_close(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Close a meeting with optional summary."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.close(body.get("summary", ""))
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/cancel")
async def meeting_cancel(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Cancel a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.cancel()
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/participants")
async def meeting_add_participant(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Invite a new agent into the meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        agent_id = (body.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(400, detail="agent_id required")
        added = m.add_participant(agent_id)
        if added:
            # Look up agent name for a nicer system message
            name = agent_id
            try:
                pce = getattr(hub, "project_chat_engine", None)
                if pce is not None:
                    ag = pce._lookup(agent_id)
                    if ag is not None:
                        name = getattr(ag, "name", "") or agent_id
            except Exception:
                pass
            try:
                m.add_message(
                    sender="system", sender_name="系统", role="system",
                    content=f"👥 {name} 加入了会议",
                )
            except Exception:
                pass
        reg.save()
        return {"ok": True, "added": added, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.delete("/meetings/{meeting_id}/participants/{agent_id}")
async def meeting_remove_participant(
    meeting_id: str,
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Remove an agent from the meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        removed = m.remove_participant(agent_id)
        if removed:
            # Cancel any in-flight reply sequence so the removed agent
            # doesn't still land a message after leaving.
            try:
                from ...meeting import bump_meeting_reply_gen
                bump_meeting_reply_gen(m.id)
            except Exception:
                pass
            name = agent_id
            try:
                pce = getattr(hub, "project_chat_engine", None)
                if pce is not None:
                    ag = pce._lookup(agent_id)
                    if ag is not None:
                        name = getattr(ag, "name", "") or agent_id
            except Exception:
                pass
            try:
                m.add_message(
                    sender="system", sender_name="系统", role="system",
                    content=f"👋 {name} 已被移出会议",
                )
            except Exception:
                pass
        reg.save()
        return {"ok": True, "removed": removed, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/interrupt")
async def meeting_interrupt(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Stop any in-flight agent reply sequence for this meeting.

    Does not post a message; purely cancels the running round so the
    host regains the floor immediately.
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        from ...meeting import bump_meeting_reply_gen
        new_gen = bump_meeting_reply_gen(m.id)
        # Also append a system note so the transcript shows the interrupt
        try:
            m.add_message(
                sender="system",
                sender_name="系统",
                role="system",
                content="⏸ 主持人已暂停 Agent 发言，等待下一指令",
            )
            reg.save()
        except Exception:
            pass
        return {"ok": True, "gen": new_gen}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/messages")
async def meeting_post_message(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Post a message to a meeting and trigger agent auto-replies."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        sender = body.get("sender", "")
        if not sender:
            sender = getattr(user, "username", "") or getattr(user, "user_id", "user")
        msg = m.add_message(
            sender=sender,
            content=body.get("content", ""),
            role=body.get("role", "user"),
            sender_name=body.get("sender_name", sender),
            attachments=body.get("attachments"),
        )
        reg.save()

        # Phase-2 wiring: auto-create MeetingAssignment when the user's
        # message contains @<agent> + a task-intent keyword ("请完成",
        # "调研", "生成报告", etc.). After the discussion round ends,
        # meeting_agent_reply will find these OPEN assignments and
        # hand each off to an execution-mode worker.
        try:
            if msg.role == "user":
                from ...meeting import _detect_task_assignment
                pce = getattr(hub, "project_chat_engine", None)
                lookup = pce._lookup if pce else (lambda _i: None)
                detected = _detect_task_assignment(
                    msg.content or "", m, lookup,
                )
                for d in detected:
                    try:
                        m.add_assignment(
                            title=d["title"],
                            assignee_agent_id=d["assignee_agent_id"],
                            description="来自会议消息的自动识别任务",
                        )
                        logger.info(
                            "meeting %s: auto-created assignment for %s",
                            m.id, d["assignee_agent_id"][:8],
                        )
                    except Exception as _ae:
                        logger.warning(
                            "meeting %s: add_assignment failed: %s",
                            m.id, _ae,
                        )
                if detected:
                    reg.save()
        except Exception as _det_err:
            logger.warning(
                "meeting %s: task detection failed: %s",
                m.id, _det_err,
            )

        # ── Agent auto-reply: when a user posts to an ACTIVE meeting,
        #    each participant agent replies in sequence (daemon thread).
        #    Stop-commands ("暂停"/"停止"/...) only interrupt in-flight
        #    replies — they do NOT trigger a new round. ──
        interrupted = False
        respondents: list[str] = []  # agent ids expected to reply (for UI typing bubbles)
        try:
            from ...meeting import (
                MeetingStatus, spawn_meeting_reply,
                is_stop_command, bump_meeting_reply_gen,
            )
            _status_val = m.status.value if hasattr(m.status, 'value') else str(m.status)
            if msg.role == "user" and _status_val == "active":
                if is_stop_command(msg.content or ""):
                    # User wants to halt — bump gen so running sequence aborts,
                    # but do not spawn a new reply round.
                    bump_meeting_reply_gen(m.id)
                    interrupted = True
                    logger.info("meeting %s: stop-command detected, aborting in-flight replies", m.id)
                else:
                    pce = getattr(hub, "project_chat_engine", None)
                    if pce is not None and m.participants:
                        logger.info("spawning meeting reply for %d participants", len(m.participants))
                        # target_agents semantics (explicit):
                        #   absent / null  -> None (all participants reply)
                        #   []             -> [] (nobody replies, user @-none)
                        #   [ids]          -> only those reply
                        _ta = body.get("target_agents", None)
                        if _ta is not None and not isinstance(_ta, list):
                            _ta = None
                        # Compute respondents list for frontend typing bubbles
                        _parts = list(m.participants or [])
                        if _ta is None:
                            respondents = _parts
                        elif isinstance(_ta, list):
                            _pset = set(_parts)
                            respondents = [x for x in _ta if x in _pset]
                        spawn_meeting_reply(
                            meeting=m,
                            registry=reg,
                            agent_chat_fn=pce._chat,
                            agent_lookup_fn=pce._lookup,
                            user_msg=msg.content,
                            target_agent_ids=_ta,
                        )
                    else:
                        logger.warning("meeting reply skipped: pce=%s, participants=%s", pce, m.participants)
        except Exception as _e:
            logger.warning("meeting agent reply spawn failed: %s", _e, exc_info=True)

        return {
            "ok": True,
            "message": msg.to_dict(),
            "interrupted": interrupted,
            "respondents": respondents,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments")
async def meeting_create_assignment(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a task assignment within a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        title = body.get("title", "")
        if not title:
            raise HTTPException(400, "title is required")
        a = m.add_assignment(
            title=title,
            assignee_agent_id=body.get("assignee_agent_id", ""),
            description=body.get("description", ""),
            due_hint=body.get("due_hint", ""),
            project_id=body.get("project_id", ""),
        )
        reg.save()
        return {"ok": True, "assignment": a.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments/{assignment_id}/update")
async def meeting_update_assignment(
    meeting_id: str,
    assignment_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a meeting assignment status."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        for a in m.assignments:
            if a.id == assignment_id:
                new_status = body.get("status", "")
                if new_status:
                    a.status = new_status
                reg.save()
                return {"ok": True, "assignment": a.to_dict()}
        raise HTTPException(404, "Assignment not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments/{assignment_id}/dispatch")
async def meeting_dispatch_assignment(
    meeting_id: str,
    assignment_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Materialize a meeting assignment into an AgentTask on the assignee.

    - Creates an AgentTask with source_meeting_id / source_assignment_id
    - Adds the meeting workspace to agent's authorized_workspaces
    - Updates assignment status to in_progress
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        target = None
        for a in m.assignments:
            if a.id == assignment_id:
                target = a
                break
        if not target:
            raise HTTPException(404, "Assignment not found")
        if not target.assignee_agent_id:
            raise HTTPException(400, "Assignment has no assignee_agent_id")

        # Find the agent
        agent = hub.get_agent(target.assignee_agent_id)
        if not agent:
            raise HTTPException(404, f"Agent not found: {target.assignee_agent_id}")

        # Create AgentTask linked to this meeting assignment
        from ...agent import AgentTask, TaskStatus
        task = AgentTask(
            title=target.title,
            description=target.description or target.title,
            source="meeting",
            source_meeting_id=meeting_id,
            source_assignment_id=assignment_id,
            assigned_by=m.host or "meeting",
        )
        agent.tasks.append(task)

        # Grant meeting workspace access
        if m.workspace_dir and m.workspace_dir not in (agent.authorized_workspaces or []):
            agent.authorized_workspaces.append(m.workspace_dir)

        # Update assignment status + link
        target.status = "in_progress"
        target.updated_at = __import__("time").time()

        reg.save()
        hub._save_agent_workspace(agent)
        return {
            "ok": True,
            "agent_task_id": task.id,
            "assignment": target.to_dict(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Progress posting (agents call this to update task status in meeting)
# ---------------------------------------------------------------------------

@router.post("/meetings/{meeting_id}/progress")
async def meeting_post_progress(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Agent posts a progress update for a meeting assignment."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        agent_id = body.get("agent_id", "")
        agent_name = body.get("agent_name", agent_id)
        assignment_id = body.get("assignment_id", "")
        status = body.get("status", "in_progress")
        detail = body.get("detail", "")
        if not assignment_id:
            raise HTTPException(400, "assignment_id is required")
        msg = m.post_progress(
            agent_id=agent_id,
            agent_name=agent_name,
            assignment_id=assignment_id,
            status=status,
            detail=detail,
        )
        reg.save()
        return {"ok": True, "message": msg.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting workspace file management
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}/files")
async def list_meeting_files(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List files in the meeting shared workspace."""
    try:
        _, m = _get_meeting(hub, meeting_id)
        return {"files": m.list_files(), "workspace_dir": m.workspace_dir}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/files/upload")
async def upload_meeting_file(
    meeting_id: str,
    file: UploadFile = File(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Upload a file to the meeting shared workspace."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        os.makedirs(m.workspace_dir, exist_ok=True)
        # Sanitize filename
        safe_name = os.path.basename(file.filename or "upload")
        dest = os.path.join(m.workspace_dir, safe_name)
        # Avoid overwriting: append suffix if exists
        base, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(m.workspace_dir, f"{base}_{counter}{ext}")
            counter += 1
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        final_name = os.path.basename(dest)
        # Auto-post system message about the upload
        actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
        m.add_message(
            sender=actor,
            sender_name=actor,
            role="system",
            content=f"📎 上传文件: {final_name} ({len(content)} bytes)",
        )
        reg.save()
        return {"ok": True, "filename": final_name, "size": len(content)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/meetings/{meeting_id}/files/{filename}")
async def download_meeting_file(
    meeting_id: str,
    filename: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Download a file from the meeting shared workspace."""
    try:
        _, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        safe_name = os.path.basename(filename)
        fpath = os.path.join(m.workspace_dir, safe_name)
        if not os.path.isfile(fpath):
            raise HTTPException(404, f"File not found: {safe_name}")
        return FileResponse(fpath, filename=safe_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.delete("/meetings/{meeting_id}/files/{filename}")
async def delete_meeting_file(
    meeting_id: str,
    filename: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a file from the meeting shared workspace."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        safe_name = os.path.basename(filename)
        fpath = os.path.join(m.workspace_dir, safe_name)
        if not os.path.isfile(fpath):
            raise HTTPException(404, f"File not found: {safe_name}")
        os.remove(fpath)
        actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
        m.add_message(
            sender=actor, sender_name=actor, role="system",
            content=f"🗑️ 删除文件: {safe_name}",
        )
        reg.save()
        return {"ok": True, "deleted": safe_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Fallback: manage meeting via action field in body
# ---------------------------------------------------------------------------

@router.post("/meetings/{meeting_id}")
async def manage_meeting(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Fallback: manage meeting via action field in body."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        action = body.get("action", "")
        if action == "start":
            m.start()
        elif action == "close":
            m.close(body.get("summary", ""))
        elif action == "cancel":
            m.cancel()
        elif action == "add_participant":
            m.add_participant(body.get("agent_id", ""))
        elif action == "remove_participant":
            m.remove_participant(body.get("agent_id", ""))
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
