"""
V2 REST + SSE router (PRD §10.1-10.3).

All endpoints are rooted at ``/api/v2``. Conventions:
    - Success:  ``{"ok": true, ...}``
    - Error:    ``{"ok": false, "error": "...", "error_code": "..."}``
              (HTTPException.detail carries this dict)

This module is intentionally flat: one file, 14 endpoints + SSE. The V2
core modules (agent_v2 / task_store / task_controller / task_events /
templates.loader) do the real work; the handlers just parse, dispatch,
and serialise.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Optional

from fastapi import (
    APIRouter, Body, Depends, File, Form, HTTPException, Query,
    Request, UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse

from ..deps.auth import CurrentUser, get_current_user

from app.v2.agent.agent_v2 import AgentV2, Capabilities
from app.v2.core.task import Task, TaskStatus, TaskPhase
from app.v2.core.task_store import get_store
from app.v2.core.task_events import TaskEventBus, TaskEvent
from app.v2.core import task_controller
from app.v2.templates import loader as template_loader


logger = logging.getLogger("tudouclaw.api.v2")

router = APIRouter(prefix="/api/v2", tags=["v2"])


# ── shared singletons ─────────────────────────────────────────────────


_bus_singleton: Optional[TaskEventBus] = None


def _get_bus() -> TaskEventBus:
    """Lazily construct one TaskEventBus per process (mirrors
    v2.agent.agent_v2._get_shared_bus behaviour)."""
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = TaskEventBus(get_store())
    return _bus_singleton


def _err(code: str, msg: str, status: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"ok": False, "error": msg, "error_code": code},
    )


# ── minimal RBAC ──────────────────────────────────────────────────────
#
# Write / destructive operations (delete agent, cancel task, clone) are
# restricted to super-admin in V2. Read + submit / pause / resume /
# clarify are available to any authenticated user so individual users
# can still drive their own tasks. The policy is centralised here so we
# can later swap for a proper Casbin-style rule engine.

_WRITE_OPS_ROLES: frozenset[str] = frozenset({"superAdmin", "admin", "owner"})


def _require_write(user: CurrentUser) -> None:
    if (user.role or "").strip() not in _WRITE_OPS_ROLES:
        raise _err(
            "FORBIDDEN",
            f"role {user.role!r} cannot perform this action",
            403,
        )


# ── serialisation helpers ─────────────────────────────────────────────


def _agent_to_dict(agent: AgentV2) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "v1_agent_id": agent.v1_agent_id,
        "capabilities": asdict(agent.capabilities),
        "task_template_ids": list(agent.task_template_ids),
        "working_directory": agent.working_directory,
        "archived": bool(getattr(agent, "archived", False)),
        "created_at": agent.created_at,
    }


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "agent_id": task.agent_id,
        "parent_task_id": task.parent_task_id,
        "template_id": task.template_id,
        "intent": task.intent,
        "phase": task.phase.value,
        "status": task.status.value,
        "priority": task.priority,
        "timeout_s": task.timeout_s,
        "finished_reason": task.finished_reason,
        "plan": {
            "steps": [asdict(s) for s in task.plan.steps],
            "expected_artifact_count": task.plan.expected_artifact_count,
        },
        "artifacts": [asdict(a) for a in task.artifacts],
        "lessons": [
            {
                "id": le.id,
                "phase": le.phase.value,
                "issue": le.issue,
                "fix": le.fix,
                "occurrence_count": le.occurrence_count,
                "created_at": le.created_at,
            }
            for le in task.lessons
        ],
        "retries": dict(task.retries),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "event_stream_url": f"/api/v2/tasks/{task.id}/events",
    }


def _get_agent_or_404(agent_id: str) -> AgentV2:
    agent = get_store().get_agent(agent_id)
    if agent is None:
        raise _err("AGENT_NOT_FOUND", f"agent {agent_id!r} not found", 404)
    return agent


def _get_task_or_404(task_id: str) -> Task:
    task = get_store().get_task(task_id)
    if task is None:
        raise _err("TASK_NOT_FOUND", f"task {task_id!r} not found", 404)
    return task


# ── agents ────────────────────────────────────────────────────────────


@router.post("/agents", status_code=201)
async def create_agent(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    name = str(body.get("name") or "").strip()
    role = str(body.get("role") or "").strip()
    if not name or not role:
        raise _err("INVALID_BODY", "name and role are required")
    caps_d = body.get("capabilities") or {}
    caps = Capabilities(
        skills=list(caps_d.get("skills") or []),
        mcps=list(caps_d.get("mcps") or []),
        tools=list(caps_d.get("tools") or []),
        llm_tier=str(caps_d.get("llm_tier") or "default"),
        denied_tools=list(caps_d.get("denied_tools") or []),
    )
    # Explicit id allows V2 shells to share ids with V1 agents so both
    # systems address the same logical agent. ``id`` idempotency: if the
    # id already exists as a V2 shell we refuse (409) rather than
    # silently overwrite state.
    want_id = str(body.get("id") or "").strip()
    if want_id:
        existing = get_store().get_agent(want_id)
        if existing is not None:
            raise _err(
                "ID_CONFLICT",
                f"V2 agent with id {want_id!r} already exists",
                409,
            )

    agent = AgentV2.create(
        id=want_id,
        name=name,
        role=role,
        v1_agent_id=str(body.get("v1_agent_id") or ""),
        capabilities=caps,
        task_template_ids=list(body.get("task_template_ids") or []),
        working_directory=str(body.get("working_directory") or ""),
    )
    get_store().save_agent(agent)
    return {"ok": True, "agent": _agent_to_dict(agent)}


@router.get("/agents")
async def list_agents(
    role: str = Query(""),
    include_archived: bool = Query(False),
    user: CurrentUser = Depends(get_current_user),
):
    agents = get_store().list_agents(role=role, include_archived=include_archived)
    return {"ok": True, "agents": [_agent_to_dict(a) for a in agents]}


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(agent_id)
    return {"ok": True, "agent": _agent_to_dict(agent)}


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(agent_id)
    if "name" in body:
        agent.name = str(body["name"]).strip() or agent.name
    if "role" in body:
        agent.role = str(body["role"]).strip() or agent.role
    if "capabilities" in body and isinstance(body["capabilities"], dict):
        cd = body["capabilities"]
        if "skills" in cd:       agent.capabilities.skills = list(cd["skills"])
        if "mcps" in cd:         agent.capabilities.mcps = list(cd["mcps"])
        if "tools" in cd:        agent.capabilities.tools = list(cd["tools"])
        if "llm_tier" in cd:     agent.capabilities.llm_tier = str(cd["llm_tier"])
        if "denied_tools" in cd: agent.capabilities.denied_tools = list(cd["denied_tools"])
    if "task_template_ids" in body:
        agent.task_template_ids = list(body["task_template_ids"])
    get_store().save_agent(agent)
    return {"ok": True, "agent": _agent_to_dict(agent)}


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str,
    hard: bool = Query(
        False,
        description="If true, hard-delete: purge tasks, events, attachments, "
                    "and all dependent rows. If false (default), soft-archive "
                    "only so the agent can be restored.",
    ),
    user: CurrentUser = Depends(get_current_user),
):
    _require_write(user)
    _get_agent_or_404(agent_id)  # presence check
    store = get_store()

    if not hard:
        store.archive_agent(agent_id)
        return {"ok": True, "archived": True, "hard": False}

    # Hard delete: purge dependent data first, then the agent row itself.
    from app.cleanup import purge_agent
    report = purge_agent(agent_id)

    # Remove the V2 agent row last so foreign keys point at something
    # real up to this point.
    try:
        with store._connect() as conn:  # type: ignore[attr-defined]
            conn.execute("DELETE FROM agents_v2 WHERE id = ?", (agent_id,))
    except Exception as e:  # noqa: BLE001
        raise _err("DELETE_FAILED", f"agent row delete: {e}", 500)

    return {"ok": True, "archived": False, "hard": True, "purge": report}


@router.post("/admin/sweep-orphans")
async def sweep_orphaned_data(
    agent_ids: list[str] = Body(
        default=...,
        embed=True,
        description="List of V1/V2 agent IDs whose data must be purged even "
                    "though the agent record is already gone.",
    ),
    user: CurrentUser = Depends(get_current_user),
):
    """Purge dependent rows for agents that were deleted without cascade.

    Use this after upgrading — if you had V1 agents deleted before the
    cascade logic existed, their memory / MCP bindings / skill grants /
    V2 tasks are still in the DB. Pass the stale agent_ids here to clean up.
    """
    _require_write(user)
    from app.cleanup import purge_agent
    results = {aid: purge_agent(aid) for aid in (agent_ids or []) if aid}
    return {"ok": True, "swept": len(results), "results": results}


@router.post("/agents/{agent_id}/clone_from_v1", status_code=201)
async def clone_from_v1(
    agent_id: str,   # unused but present in URL for symmetry with PRD §10.2.2
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    v1_id = str(body.get("v1_agent_id") or "").strip()
    if not v1_id:
        raise _err("INVALID_BODY", "v1_agent_id required")
    try:
        agent = AgentV2.clone_from_v1(v1_id, store=get_store())
    except KeyError as e:
        raise _err("V1_AGENT_NOT_FOUND", str(e), 404)
    except Exception as e:
        raise _err("CLONE_FAILED", f"{type(e).__name__}: {e}", 500)
    return {
        "ok": True,
        "agent": _agent_to_dict(agent),
        "clone_report": {
            "copied_skills": len(agent.capabilities.skills),
            "copied_mcps": len(agent.capabilities.mcps),
            "skipped_messages": True,
        },
    }


# ── tasks ─────────────────────────────────────────────────────────────


@router.post("/agents/{agent_id}/tasks", status_code=202)
async def submit_task(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(agent_id)
    intent = str(body.get("intent") or "").strip()
    if not intent:
        raise _err("INVALID_BODY", "intent is required")
    template_id = str(body.get("template_id") or "").strip()
    if template_id and template_loader.get_template(template_id) is None:
        raise _err("UNKNOWN_TEMPLATE", f"template {template_id!r} not found", 400)

    # One active task per agent: extras are QUEUED, not rejected.
    # ``submit_task`` itself decides whether to start the loop now or
    # park the task in the queue based on
    # ``store.count_active_tasks(agent_id)``. Subtasks always start
    # immediately (they're not subject to the queue).
    parent = str(body.get("parent_task_id") or "")

    # Accept attachment descriptors pre-uploaded via
    # ``POST /agents/{id}/attachments``. We trust only ``handle``,
    # ``kind``, and ``mime`` — arbitrary payload fields are dropped so
    # attachments from a malicious client can't smuggle data through.
    raw_atts = body.get("attachments") or []
    attachments: list[dict] = []
    for a in raw_atts:
        if not isinstance(a, dict):
            continue
        handle = str(a.get("handle") or "").strip()
        if not handle:
            continue
        attachments.append({
            "kind":   str(a.get("kind") or "file"),
            "handle": handle,
            "mime":   str(a.get("mime") or ""),
            "name":   str(a.get("name") or ""),
            "size":   int(a.get("size") or 0),
        })

    task = agent.submit_task(
        intent=intent,
        template_id=template_id,
        parent_task_id=parent,
        attachments=attachments or None,
        priority=int(body.get("priority") or 5),
        timeout_s=int(body.get("timeout_s") or 1800),
        store=get_store(),
        bus=_get_bus(),
    )
    return {"ok": True, "task": _task_to_dict(task)}


@router.get("/tasks")
async def list_tasks(
    agent_id: str = Query(""),
    status: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_current_user),
):
    store = get_store()
    tasks = store.list_tasks(
        agent_id=agent_id, status=status, limit=limit, offset=offset,
    )
    return {
        "ok": True,
        "tasks": [_task_to_dict(t) for t in tasks],
        "has_more": len(tasks) == limit,
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    task = _get_task_or_404(task_id)
    # Surface phase_error events inline so the UI can show WHY a task
    # failed without opening the SSE stream. Limited to recent errors
    # (last 10) to keep payload small.
    errors: list[dict] = []
    try:
        events = get_store().load_events(task_id)
        for evt in events:
            if evt.type == "phase_error":
                payload = evt.payload if isinstance(evt.payload, dict) else {}
                errors.append({
                    "ts": evt.ts,
                    "phase": payload.get("phase") or evt.phase,
                    "error": payload.get("error", ""),
                    "raw_content": payload.get("raw_content", ""),
                    "hint": payload.get("hint", ""),
                    "skipped": payload.get("skipped", []),
                })
        errors = errors[-10:]
    except Exception:
        errors = []
    return {"ok": True, "task": _task_to_dict(task), "errors": errors}


@router.post("/tasks/{task_id}/pause")
async def pause_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    task = _get_task_or_404(task_id)
    if not task_controller.pause_task(task, get_store(), _get_bus()):
        raise _err(
            "INVALID_STATE_TRANSITION",
            f"cannot pause task in status={task.status.value}", 409,
        )
    return {"ok": True, "task": _task_to_dict(task)}


@router.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    task = _get_task_or_404(task_id)
    agent = _get_agent_or_404(task.agent_id)
    if not task_controller.resume_task(task, agent, get_store(), _get_bus()):
        raise _err(
            "INVALID_STATE_TRANSITION",
            f"cannot resume task in status={task.status.value}", 409,
        )
    return {"ok": True, "task": _task_to_dict(task)}


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Hard-delete a V2 task and its event log.

    Only terminal tasks (``completed`` / ``failed`` / ``cancelled``) can
    be deleted. If you want to stop a running/paused task, call
    ``POST /tasks/{id}/cancel`` first — that transitions it to
    ``cancelled`` and unblocks this delete.

    Super-admin only (destructive).
    """
    _require_write(user)
    task = _get_task_or_404(task_id)
    terminal = {"completed", "failed", "cancelled"}
    if task.status.value not in terminal:
        raise _err(
            "INVALID_STATE_TRANSITION",
            f"cannot delete task in status={task.status.value}; "
            f"cancel it first", 409,
        )
    ok = get_store().delete_task(task_id)
    if not ok:
        raise _err("TASK_NOT_FOUND", f"task {task_id!r} vanished", 404)
    return {"ok": True, "task_id": task_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Cancel a RUNNING, PAUSED, or QUEUED task.

    Cancelling a QUEUED task removes it from the queue without ever
    running it. Cancelling the RUNNING task automatically promotes the
    next QUEUED task for the same agent (if any) so the queue drains."""
    _require_write(user)
    task = _get_task_or_404(task_id)
    agent = get_store().get_agent(task.agent_id)
    if not task_controller.cancel_task(
        task, get_store(), _get_bus(), agent=agent,
    ):
        raise _err(
            "INVALID_STATE_TRANSITION",
            f"cannot cancel task in status={task.status.value}", 409,
        )
    return {"ok": True, "task": _task_to_dict(task)}


# ── multimodal attachments ────────────────────────────────────────────


@router.post("/agents/{agent_id}/attachments", status_code=201)
async def upload_attachment(
    agent_id: str,
    file: UploadFile = File(...),
    task_id: str = Form(
        "",
        description="Attach to a specific task. If empty, a 'draft' folder "
                    "is used and callers should move/copy when they finalise "
                    "the task submission.",
    ),
    user: CurrentUser = Depends(get_current_user),
):
    """Upload one attachment for a multimodal task.

    The returned descriptor is what clients pass in
    ``submit_task.attachments[]``. We deliberately separate upload from
    submit so the UI can preview an image before the user clicks Submit.
    """
    agent = _get_agent_or_404(agent_id)
    content = await file.read()
    if not content:
        raise _err("INVALID_BODY", "uploaded file is empty")

    from app.v2.bridges import attachment_bridge

    try:
        descriptor = attachment_bridge.save_attachment(
            agent_working_dir=agent.working_directory,
            task_id=(task_id or "draft"),
            filename=file.filename or "upload.bin",
            content=content,
            mime=file.content_type or "",
        )
    except ValueError as e:
        raise _err("INVALID_BODY", str(e), 400)

    # Serve URL the frontend can use to preview the file.
    import urllib.parse as _up
    serve_url = (
        f"/api/v2/agents/{agent_id}/attachments/serve"
        f"?handle={_up.quote(descriptor['handle'])}"
    )
    return {
        "ok": True,
        "attachment": {**descriptor, "url": serve_url},
    }


@router.get("/agents/{agent_id}/attachments/serve")
async def serve_attachment(
    agent_id: str,
    handle: str = Query(..., description="Absolute path returned at upload."),
    user: CurrentUser = Depends(get_current_user),
):
    """Serve an uploaded attachment for inline rendering.

    Path-traversal protected: only files under the agent's
    ``attachments/`` subtree can be served.
    """
    agent = _get_agent_or_404(agent_id)
    from app.v2.bridges import attachment_bridge
    try:
        resolved = attachment_bridge.resolve_path_for_serve(
            agent_working_dir=agent.working_directory,
            handle=handle,
        )
    except (ValueError, FileNotFoundError) as e:
        raise _err("ATTACHMENT_NOT_FOUND", str(e), 404)
    return FileResponse(resolved)


@router.get("/agents/{agent_id}/queue")
async def get_agent_queue(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Return the (active, queued[]) pair for a given agent.

    ``active`` is the RUNNING/PAUSED task currently holding the agent
    (or ``null`` when idle); ``queued`` is the FIFO-ordered list of
    tasks waiting to run."""
    _get_agent_or_404(agent_id)
    store = get_store()
    # Find the one active (RUNNING or PAUSED) task, if any.
    active_list = store.list_tasks(agent_id=agent_id, status="running", limit=1)
    if not active_list:
        active_list = store.list_tasks(agent_id=agent_id, status="paused", limit=1)
    active = active_list[0] if active_list else None
    queued = store.list_queued_for_agent(agent_id)
    return {
        "ok": True,
        "active": _task_to_dict(active) if active else None,
        "queued": [_task_to_dict(t) for t in queued],
    }


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(
    task_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    task = _get_task_or_404(task_id)
    answer = str(body.get("answer") or "").strip()
    if not answer:
        raise _err("INVALID_BODY", "answer required")
    agent = _get_agent_or_404(task.agent_id)
    ok = task_controller.accept_clarification(
        task, answer, agent, get_store(), _get_bus(),
    )
    if not ok:
        raise _err(
            "INVALID_STATE_TRANSITION",
            "task is not awaiting a clarification answer", 409,
        )
    return {"ok": True, "task": _task_to_dict(task)}


# ── SSE events (PRD §10.3) ────────────────────────────────────────────


_HEARTBEAT_S = 15.0


async def _sse_auth_dep(
    request: Request,
    access_token: Optional[str] = Query(
        None,
        description=(
            "JWT fallback for EventSource clients that can't set the "
            "Authorization header. Ignored when a Bearer token or session "
            "cookie is present."
        ),
    ),
) -> CurrentUser:
    """SSE auth dependency: accepts JWT via query param as a fallback,
    otherwise delegates to the regular ``get_current_user`` dep (which
    ``dependency_overrides`` can still replace in tests)."""
    if access_token:
        try:
            from ..deps.auth import decode_token
            payload = decode_token(access_token)
            return CurrentUser(
                user_id=str(payload.get("sub") or ""),
                role=str(payload.get("role") or "admin"),
                claims=payload,
            )
        except Exception:
            pass
    # Delegate — this is what ``dependency_overrides`` hooks onto.
    return await get_current_user(request)


@router.get("/tasks/{task_id}/events")
async def task_event_stream(
    task_id: str,
    request: Request,
    since: float = Query(0.0, description="only events with ts > since"),
    user: CurrentUser = Depends(_sse_auth_dep),
):

    _get_task_or_404(task_id)  # 404 short-circuit
    bus = _get_bus()

    # Replay persisted events first (since-filtered), then stream live.
    replayed = bus.replay(task_id, since_ts=since)

    # Live stream: bus delivers in-process; we use an asyncio.Queue bridge.
    queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _on_event(evt: TaskEvent) -> None:
        # bus dispatches on worker thread; bridge to the event loop.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, evt)
        except RuntimeError:
            pass

    unsubscribe = bus.subscribe(task_id, _on_event)

    async def _gen():
        try:
            # 1. Drain replay.
            last_ts = since
            for evt in replayed:
                yield _format_sse(evt)
                last_ts = max(last_ts, evt.ts)

            # 2. Live + heartbeat.
            while True:
                if await request.is_disconnected():
                    break

                # Terminal-status probe: if task is DONE we send stream_end.
                task = get_store().get_task(task_id)
                if task is None or task.phase == TaskPhase.DONE:
                    yield "event: stream_end\ndata: {}\n\n"
                    break

                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
                    yield _format_sse(evt)
                    last_ts = max(last_ts, evt.ts)
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {{\"ts\":{time.time()}}}\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format_sse(evt: TaskEvent) -> str:
    data = json.dumps({
        "task_id": evt.task_id,
        "phase": evt.phase,
        "payload": evt.payload,
        "ts": evt.ts,
    }, ensure_ascii=False)
    return f"event: {evt.type}\ndata: {data}\n\n"


# ── templates ─────────────────────────────────────────────────────────


# ── V2 provider / tier bindings ───────────────────────────────────────
#
# These endpoints are V2-owned views over V1's ProviderRegistry. They
# expose ONLY the fields V2 cares about (tier_models, supports_multimodal,
# enabled, base_url for diagnostics). The V1 Providers UI remains
# authoritative for everything else — V2 never creates or deletes
# providers, only edits their tier bindings.


def _provider_summary(p) -> dict:
    return {
        "id":    p.id,
        "name":  p.name,
        "kind":  p.kind,
        "base_url": p.base_url,
        "enabled":  bool(p.enabled),
        "tier_models": dict(p.tier_models or {}),
        "supports_multimodal": bool(p.supports_multimodal),
        "models": list(p.models_cache or []) + list(
            m for m in (p.manual_models or []) if m not in (p.models_cache or [])
        ),
    }


@router.get("/providers")
async def list_v2_providers(
    user: CurrentUser = Depends(get_current_user),
):
    """Return every V1 provider along with its V2-relevant fields.

    Consumed by the Tier Bindings page — users decide which provider
    serves which tier by PATCH-ing ``tier_models``."""
    from app import llm as _llm
    out = [_provider_summary(p)
           for p in _llm.get_registry().list(include_disabled=True)]
    return {"ok": True, "providers": out}


@router.patch("/providers/{provider_id}/tiers")
async def patch_provider_tiers(
    provider_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a provider's ``tier_models`` and/or ``supports_multimodal``.

    Request body fields (all optional)::

        tier_models:          {tier_name: model_name, ...}
        supports_multimodal:  bool
        enabled:              bool     # convenience toggle

    Unknown body keys are ignored. Returns the updated provider summary.
    """
    _require_write(user)
    from app import llm as _llm
    reg = _llm.get_registry()
    prov = reg.get(provider_id)
    if prov is None:
        raise _err("PROVIDER_NOT_FOUND", f"provider {provider_id!r} not found", 404)

    kwargs: dict = {}
    if "tier_models" in body and isinstance(body["tier_models"], dict):
        # Keep only non-empty string-to-string entries.
        tm = {}
        for k, v in body["tier_models"].items():
            k = str(k or "").strip()
            v = str(v or "").strip()
            if k and v:
                tm[k] = v
        kwargs["tier_models"] = tm
    if "supports_multimodal" in body:
        kwargs["supports_multimodal"] = bool(body["supports_multimodal"])
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])

    if not kwargs:
        raise _err("INVALID_BODY", "no mutable fields supplied")

    updated = reg.update(provider_id, **kwargs)
    if updated is None:
        raise _err("PROVIDER_NOT_FOUND", f"update failed for {provider_id!r}", 404)
    return {"ok": True, "provider": _provider_summary(updated)}


@router.post("/providers/{provider_id}/detect-models")
async def detect_provider_models(
    provider_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Ask the provider endpoint for its live model list.

    Delegates to V1's ``ProviderRegistry.detect_models``; returned list
    is also cached on the provider for future reads."""
    _require_write(user)
    from app import llm as _llm
    reg = _llm.get_registry()
    if reg.get(provider_id) is None:
        raise _err("PROVIDER_NOT_FOUND", f"provider {provider_id!r} not found", 404)
    try:
        models = reg.detect_models(provider_id, timeout=10.0)
    except Exception as e:  # noqa: BLE001
        raise _err("DETECT_FAILED", f"{type(e).__name__}: {e}", 502)
    return {"ok": True, "models": models}


@router.get("/tiers")
async def list_tier_catalog(
    user: CurrentUser = Depends(get_current_user),
):
    """Return the known tier names.

    Merges the hard-coded well-known set with any custom tier names
    currently declared in provider ``tier_models``."""
    from app.v2.bridges.llm_tier_routing import known_tiers
    return {"ok": True, "tiers": known_tiers()}


@router.get("/metrics")
async def v2_metrics(
    user: CurrentUser = Depends(get_current_user),
):
    """Return in-process counters (task_submitted, task_completed,
    task_failed, …). Consumed by the dashboard; swap implementation
    for prometheus_client when metrics go multi-process."""
    from app.v2.core.observability import snapshot
    return {"ok": True, "counters": snapshot()}


@router.get("/templates")
async def list_templates_api(
    user: CurrentUser = Depends(get_current_user),
):
    items = template_loader.list_templates()
    summaries = [
        {
            "id": t.get("id"),
            "display_name": t.get("display_name"),
            "version": t.get("version"),
            "required_slots": [
                s.get("name") for s in (t.get("required_slots") or [])
                if isinstance(s, dict) and s.get("name")
            ],
            "allowed_tools": list(t.get("allowed_tools") or []),
        }
        for t in items
    ]
    return {"ok": True, "templates": summaries}


@router.get("/templates/{template_id}")
async def get_template_api(
    template_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    tmpl = template_loader.get_template(template_id)
    if tmpl is None:
        raise _err("TEMPLATE_NOT_FOUND", f"template {template_id!r} not found", 404)
    return {"ok": True, "template": tmpl}


__all__ = ["router"]
