"""Chat SSE streaming router — real-time task event streaming."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.chat")

router = APIRouter(prefix="/api/portal", tags=["chat"])


def _get_chat_task(task_id: str):
    """Retrieve a ChatTask by ID."""
    from ...chat_task import get_chat_task_manager
    mgr = get_chat_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found")
    return task


# ---------------------------------------------------------------------------
# SSE stream — replaces the old long-poll endpoint
# ---------------------------------------------------------------------------

@router.get("/chat-task/{task_id}/stream")
async def stream_task_events(
    task_id: str,
    cursor: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_current_user),
):
    """Server-Sent Events stream for a chat task.

    The client connects once, receives events in real-time,
    and the stream ends with a 'done' event.
    """
    task = _get_chat_task(task_id)

    async def event_generator():
        nonlocal cursor
        from ...chat_task import ChatTaskStatus

        terminal = {ChatTaskStatus.COMPLETED, ChatTaskStatus.FAILED, ChatTaskStatus.ABORTED}
        max_idle = 120  # seconds before closing idle connection

        last_event_time = time.time()

        while True:
            events, new_cursor = task.get_events_since(cursor)
            cursor = new_cursor

            for evt in events:
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                last_event_time = time.time()

            # Send status heartbeat
            status_evt = {
                "type": "status",
                "status": task.status.value,
                "progress": task.progress,
                "phase": task.phase,
            }
            yield f"data: {json.dumps(status_evt, ensure_ascii=False)}\n\n"

            if task.status in terminal:
                yield "data: [DONE]\n\n"
                break

            if time.time() - last_event_time > max_idle:
                yield "data: [DONE]\n\n"
                break

            await asyncio.sleep(0.15)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Desktop loopback SSE — same body as stream_task_events, but no JWT.
# Used by the Mac floater app; permission to chat with the agent was
# already granted (or denied) at /agents/desktop/{id}/chat time, so by
# the time we have a task_id, the read side is fine to expose.
# ---------------------------------------------------------------------------

@router.get("/agents/desktop/chat-task/{task_id}/stream")
async def desktop_stream_task_events(
    request: Request,
    task_id: str,
    cursor: int = Query(0, ge=0),
):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="loopback only")
    return await stream_task_events(
        task_id=task_id,
        cursor=cursor,
        user=CurrentUser(user_id="__desktop__", role="superAdmin"),
    )


# ---------------------------------------------------------------------------
# Task status (non-streaming fallback)
# ---------------------------------------------------------------------------

@router.get("/chat-task/{task_id}/status")
async def get_task_status(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Get current task status (non-streaming fallback)."""
    task = _get_chat_task(task_id)
    return {
        "task_id": task.id,
        "agent_id": task.agent_id,
        "status": task.status.value,
        "progress": task.progress,
        "phase": task.phase,
        "result": task.result,
        "error": getattr(task, "error", ""),
    }


@router.get("/agent/{agent_id}/chat-tasks")
async def get_agent_chat_tasks(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """List ChatTasks for an agent (active + recent)."""
    from ...chat_task import get_chat_task_manager
    mgr = get_chat_task_manager()
    tasks = mgr.get_agent_tasks(agent_id)
    return {"tasks": [
        {"id": t.id, "status": t.status.value,
         "progress": t.progress, "phase": t.phase}
        for t in tasks
    ]}


@router.post("/chat-task/{task_id}/abort")
async def abort_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Abort a running chat task."""
    task = _get_chat_task(task_id)
    if hasattr(task, "abort"):
        task.abort()
    return {"ok": True}
