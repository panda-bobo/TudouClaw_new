"""Meeting management router — list, CRUD, meeting management."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Body

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


@router.post("/meetings/{meeting_id}/messages")
async def meeting_post_message(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Post a message to a meeting."""
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
        return {"ok": True, "message": msg.to_dict()}
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
