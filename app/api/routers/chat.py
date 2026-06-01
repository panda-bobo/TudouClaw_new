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
from ..deps.dual_auth import get_user_or_hub_proxy

logger = logging.getLogger("tudouclaw.api.chat")

router = APIRouter(prefix="/api/portal", tags=["chat"])


# ─── Multi-node SSE proxy ────────────────────────────────────────────────
# When master's POST /agent/{id}/chat detects a remote agent, it forwards
# to the worker and wraps the returned task_id with ``n:<node_id>:``.
# The SSE GET below sees that prefix and pipes the worker's stream
# straight to the UI — no master-side state for cross-node tasks.

_NODE_PREFIX = "n:"


def _split_remote_task_id(task_id: str) -> tuple[str, str] | None:
    """If ``task_id`` looks like ``n:<node_id>:<raw>``, return (node_id, raw).
    Otherwise None — caller treats it as local."""
    if not task_id.startswith(_NODE_PREFIX):
        return None
    parts = task_id.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


async def _proxy_sse_stream(hub, node_id: str, raw_task_id: str, cursor: int):
    """Open SSE on the worker and forward chunks to the master client.

    Uses httpx async streaming so we don't block the event loop. The
    worker's stream ends with ``[DONE]`` — we just relay everything.
    """
    import httpx
    node = hub.remote_nodes.get(node_id) if hasattr(hub, "remote_nodes") else None
    if not node or not node.url:
        raise HTTPException(404, f"Unknown node: {node_id}")

    secret = hub._get_cluster_secret() if hasattr(hub, "_get_cluster_secret") else ""
    headers = {"Accept": "text/event-stream"}
    if secret:
        headers["X-Hub-Secret"] = secret

    url = f"{node.url}/api/portal/chat-task/{raw_task_id}/stream"

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)) as client:
                async with client.stream("GET", url, headers=headers,
                                          params={"cursor": cursor}) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        yield (
                            f'data: {{"type":"error","status":{resp.status_code},'
                            f'"detail":{json.dumps(body.decode("utf-8", errors="replace")[:200])}}}\n\n'
                        ).encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        except Exception as e:
            logger.warning("SSE proxy to worker %s failed: %s", node_id, e)
            yield (
                f'data: {{"type":"error","detail":"upstream stream failed: '
                f'{json.dumps(str(e))}"}}\n\n'
            ).encode("utf-8")
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_user_or_hub_proxy),
):
    """Server-Sent Events stream for a chat task.

    The client connects once, receives events in real-time,
    and the stream ends with a 'done' event.

    Multi-node: if ``task_id`` is prefixed ``n:<node_id>:`` (this
    happens when the chat was created on a remote worker), proxy
    the SSE stream from that worker. The UI uses task_id as opaque
    so this is transparent to the client.

    Auth: dual (JWT or X-Hub-Secret) so master can also stream from
    a remote SSE proxy by authenticating with the cluster secret.
    """
    # ── Cross-node proxy branch ────────────────────────────────────
    parts = _split_remote_task_id(task_id)
    if parts is not None:
        node_id, raw_task_id = parts
        return await _proxy_sse_stream(hub, node_id, raw_task_id, cursor)

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
            # 2026-06-01: include the current cursor so the client can
            # advance it. Was missing → client kept reconnecting with
            # cursor=0, server replayed the WHOLE event log every
            # retry (payload grew to 140KB+ for busy tasks). With the
            # cursor in each status heartbeat, the client snapshots it
            # and reconnects from the right position — payload stays
            # tiny (just new events since last seen).
            status_evt = {
                "type": "status",
                "status": task.status.value,
                "progress": task.progress,
                "phase": task.phase,
                "cursor": cursor,
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
