"""Visual orchestration canvas — CRUD endpoints for the drag-drop DAG editor.

Distinct from the legacy `/workflows` router (which manages the
state-machine task workflows in app/workflow.py). Canvas workflows are
the user-authored DAGs (nodes + edges + config) saved by the
Orchestration → Canvas tab.

URL prefix: `/api/portal/canvas-workflows`
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse

from ..deps.auth import CurrentUser, get_current_user
from ..deps.hub import get_hub

logger = logging.getLogger("tudouclaw.api.canvas")

router = APIRouter(prefix="/api/portal", tags=["canvas-workflows"])


def _store_or_503():
    from ...canvas_workflows import get_store
    s = get_store()
    if s is None:
        raise HTTPException(503, "canvas workflow store not initialized")
    return s


def _engine_or_503():
    from ...canvas_executor import get_engine
    eng = get_engine()
    if eng is None:
        raise HTTPException(503, "canvas executor not initialized")
    return eng


def _artifact_store_or_503():
    from ...canvas_artifacts import get_store
    s = get_store()
    if s is None:
        raise HTTPException(503, "canvas artifact store not initialized")
    return s


def _verify_run_belongs_to_workflow(engine, wf_id: str, run_id: str) -> dict:
    """Helper: load run state + 404 if it doesn't belong to wf_id."""
    state = engine.store.load_state(run_id)
    if not state:
        raise HTTPException(404, f"run {run_id} not found")
    if state.get("workflow_id") != wf_id:
        raise HTTPException(404, f"run {run_id} doesn't belong to workflow {wf_id}")
    return state


@router.get("/canvas-workflows")
async def list_canvas_workflows(user: CurrentUser = Depends(get_current_user)):
    """Return summaries of every saved canvas workflow,
    most recently updated first."""
    try:
        store = _store_or_503()
        return {"workflows": [m.to_dict() for m in store.list_meta()]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("list_canvas_workflows failed")
        raise HTTPException(500, str(e))


@router.get("/canvas-workflows/{wf_id}")
async def get_canvas_workflow(wf_id: str,
                                user: CurrentUser = Depends(get_current_user)):
    """Return the full canvas workflow (nodes, edges, config) by id."""
    try:
        store = _store_or_503()
        wf = store.get(wf_id)
        if not wf:
            raise HTTPException(404, f"canvas workflow {wf_id} not found")
        return wf
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_canvas_workflow failed")
        raise HTTPException(500, str(e))


@router.post("/canvas-workflows")
async def save_canvas_workflow(body: dict = Body(...),
                                 user: CurrentUser = Depends(get_current_user)):
    """Create new (no id) or overwrite existing (id given). Server fills
    id (when absent), created_at, updated_at, created_by. Validates
    structural invariants — invalid payload returns 400 with the
    specific error so the canvas UI can surface a useful toast."""
    try:
        store = _store_or_503()
        return store.save(body or {}, created_by=getattr(user, "user_id", "") or "")
    except ValueError as e:
        raise HTTPException(400, f"invalid workflow: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("save_canvas_workflow failed")
        raise HTTPException(500, str(e))


@router.delete("/canvas-workflows/{wf_id}")
async def delete_canvas_workflow(wf_id: str,
                                   user: CurrentUser = Depends(get_current_user)):
    """Remove a canvas workflow file from disk."""
    try:
        store = _store_or_503()
        if not store.delete(wf_id):
            raise HTTPException(404, f"canvas workflow {wf_id} not found")
        return {"ok": True, "id": wf_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete_canvas_workflow failed")
        raise HTTPException(500, str(e))


@router.put("/canvas-workflows/{wf_id}/status")
async def set_canvas_workflow_status(wf_id: str,
                                       body: dict = Body(...),
                                       user: CurrentUser = Depends(get_current_user)):
    """Change executable_status: draft | ready | disabled.

    Marking a workflow `ready` runs full structural validation
    (single start, ≥1 end, all nodes reachable, no cycles, tool nodes
    have tool_name, decision nodes have condition, no dead-ends).
    Validation failures return 400 with the issues joined into the
    detail message so the canvas UI can show a useful toast.

    Body: ``{"status": "ready"|"draft"|"disabled"}``"""
    try:
        store = _store_or_503()
        new_status = str(body.get("status", "")).strip()
        stored = store.set_status(wf_id, new_status)
        return stored
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("set_canvas_workflow_status failed")
        raise HTTPException(500, str(e))


@router.post("/canvas-workflows/{wf_id}/validate")
async def validate_canvas_workflow(wf_id: str,
                                     user: CurrentUser = Depends(get_current_user)):
    """Run validation without changing status. Returns ``{ok: bool,
    issues: [str, ...]}``. Useful for the UI to show a "check before
    marking ready" preview."""
    try:
        store = _store_or_503()
        wf = store.get(wf_id)
        if not wf:
            raise HTTPException(404, f"canvas workflow {wf_id} not found")
        from ...canvas_workflows import WorkflowStore
        issues = WorkflowStore.validate_for_execution(wf)
        return {"ok": not issues, "issues": issues}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("validate_canvas_workflow failed")
        raise HTTPException(500, str(e))


# ── Run lifecycle (HANDOFF [D]) ────────────────────────────────────────


@router.post("/canvas-workflows/{wf_id}/runs")
async def start_canvas_run(wf_id: str,
                            user: CurrentUser = Depends(get_current_user)):
    """Trigger a new execution of a workflow. Workflow must be in
    ``executable_status=ready`` — engine refuses otherwise so an
    in-progress edit can't be silently picked up.

    Returns the created run summary (``{id, state, started_at, ...}``).
    """
    store = _store_or_503()
    engine = _engine_or_503()
    wf = store.get(wf_id)
    if not wf:
        raise HTTPException(404, f"canvas workflow {wf_id} not found")
    if str(wf.get("executable_status", "")) != "ready":
        raise HTTPException(
            400,
            f"workflow status is {wf.get('executable_status', 'draft')!r}; "
            f"must be 'ready' to run. Mark it ready in the editor first."
        )
    try:
        run = engine.trigger(wf, started_by=getattr(user, "user_id", "") or "")
        return run.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("start_canvas_run failed")
        raise HTTPException(500, str(e))


@router.get("/canvas-workflows/{wf_id}/runs")
async def list_canvas_runs(wf_id: str,
                            user: CurrentUser = Depends(get_current_user)):
    """List runs for one workflow, newest first. Cheap — reads
    metadata only from per-run JSON files."""
    engine = _engine_or_503()
    return {"runs": engine.store.list_runs_for_workflow(wf_id)}


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}")
async def get_canvas_run(wf_id: str, run_id: str,
                          user: CurrentUser = Depends(get_current_user)):
    """Get full state of one run (current state, per-node states,
    captured variables, error if any)."""
    engine = _engine_or_503()
    state = engine.store.load_state(run_id)
    if not state:
        raise HTTPException(404, f"run {run_id} not found")
    if state.get("workflow_id") != wf_id:
        raise HTTPException(404, f"run {run_id} doesn't belong to workflow {wf_id}")
    return state


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}/log")
async def get_canvas_run_log(wf_id: str, run_id: str,
                              user: CurrentUser = Depends(get_current_user)):
    """Return the full event log for one run as a JSON array.

    Companion to ``/events`` (which is SSE for live streaming) — this
    one is a plain GET that the UI can use to load a past run's log
    without opening a stream. Same data source: events.jsonl.

    Returns ``{events: [{ts, type, data}, ...], count, byte_offset}``.
    The byte_offset lets a caller resume incremental reads later
    (e.g., poll while a run is still active).
    """
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    events, offset = engine.store.read_events(run_id)
    return {"events": events, "count": len(events), "byte_offset": offset}


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}/events")
async def stream_canvas_run_events(wf_id: str, run_id: str,
                                    user: CurrentUser = Depends(get_current_user)):
    """SSE stream of run events for live UI highlighting (HANDOFF [E]
    consumes this).

    Replays existing events from offset 0 then tails for new events,
    until the run reaches a terminal state — at which point the stream
    sends a ``done`` event and closes.
    """
    engine = _engine_or_503()
    state = engine.store.load_state(run_id)
    if not state:
        raise HTTPException(404, f"run {run_id} not found")
    if state.get("workflow_id") != wf_id:
        raise HTTPException(404, f"run {run_id} doesn't belong to workflow {wf_id}")

    async def _gen():
        offset = 0
        from ...canvas_executor import TERMINAL_RUN_STATES
        terminal_states = {s.value for s in TERMINAL_RUN_STATES}
        # Cap the wait so dead/abandoned runs don't hold a connection
        # open forever. Two minutes is long enough for any node we
        # currently support; the UI can reconnect for longer runs.
        deadline = asyncio.get_event_loop().time() + 120.0
        while True:
            events, offset = engine.store.read_events(run_id, offset)
            for evt in events:
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            cur = engine.store.load_state(run_id) or {}
            if cur.get("state") in terminal_states:
                yield f"data: {json.dumps({'type': 'done', 'state': cur.get('state')})}\n\n"
                return
            if asyncio.get_event_loop().time() > deadline:
                yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ── Run artifacts (HANDOFF artifact closed-loop, 2026-05-02) ──────────


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}/artifacts")
async def list_run_artifacts(wf_id: str, run_id: str,
                              user: CurrentUser = Depends(get_current_user)):
    """List every artifact registered to one run.

    Returns ``{artifacts: [{id, name, size_bytes, sha256, mime,
    producer_node_id, producer_agent_id, marked, description, tags,
    vars_key, created_at}, ...]}``. Marked deliverables come first;
    auto-detected files (likely intermediate) follow.
    """
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    store = _artifact_store_or_503()
    items = store.list_artifacts(run_id)
    items.sort(key=lambda a: (not a.marked, -(a.created_at or 0)))
    return {"artifacts": [a.to_dict() for a in items]}


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}/artifacts/{artifact_id}")
async def download_run_artifact(wf_id: str, run_id: str, artifact_id: str,
                                 user: CurrentUser = Depends(get_current_user)):
    """Download one artifact's bytes. Records a ``read`` audit row.

    Auth check: requires the user to have access to the workflow.
    Future: per-artifact ACL (e.g., agent-only artifacts that admins
    can't see) — not implemented in MVP.
    """
    from fastapi.responses import FileResponse
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    store = _artifact_store_or_503()
    res = store.open_for_read(
        run_id, artifact_id,
        actor_agent_id=getattr(user, "user_id", "") or "",
        actor_node_id="api:download",
    )
    if res is None:
        raise HTTPException(404, f"artifact {artifact_id} not found in run {run_id}")
    art, full = res
    return FileResponse(
        path=str(full),
        media_type=art.mime or "application/octet-stream",
        filename=art.name,
    )


@router.post("/canvas-workflows/{wf_id}/runs/{run_id}/artifacts/{artifact_id}/mark")
async def mark_run_artifact(wf_id: str, run_id: str, artifact_id: str,
                             body: dict = Body(...),
                             user: CurrentUser = Depends(get_current_user)):
    """Promote an auto-detected artifact to a marked deliverable +
    optionally add a description / tags. (Plan C, admin-side.)

    Body: ``{"description": "...", "tags": ["..."]}`` (both optional).
    """
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    store = _artifact_store_or_503()
    art = store.mark_artifact(
        run_id,
        name_or_id=artifact_id,
        actor_agent_id=getattr(user, "user_id", "") or "",
        actor_node_id="api:mark",
        description=str(body.get("description", "") or ""),
        tags=list(body.get("tags") or []),
    )
    if art is None:
        raise HTTPException(404, f"artifact {artifact_id} not found in run {run_id}")
    return art.to_dict()


@router.delete("/canvas-workflows/{wf_id}/runs/{run_id}/artifacts/{artifact_id}")
async def delete_run_artifact(wf_id: str, run_id: str, artifact_id: str,
                               user: CurrentUser = Depends(get_current_user)):
    """Remove an artifact from the index AND delete its underlying
    file on disk. Records a ``delete`` audit row before removal.
    """
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    store = _artifact_store_or_503()
    ok = store.delete_artifact(
        run_id, artifact_id,
        actor_agent_id=getattr(user, "user_id", "") or "",
        actor_node_id="api:delete",
    )
    if not ok:
        raise HTTPException(404, f"artifact {artifact_id} not found in run {run_id}")
    return {"ok": True, "artifact_id": artifact_id}


@router.get("/canvas-workflows/{wf_id}/runs/{run_id}/audit")
async def list_run_audit(wf_id: str, run_id: str,
                          user: CurrentUser = Depends(get_current_user)):
    """Return the full audit log for one run. Append-only JSONL, so
    even if an artifact is later deleted its register/read trail
    survives.

    Filter helpers (query params, all optional):
      * ``artifact_id`` — only rows touching this artifact
      * ``action``      — one of register / mark / read / delete /
                           scan_start / scan_end
      * ``actor_agent_id`` — only rows by this agent
    """
    engine = _engine_or_503()
    _verify_run_belongs_to_workflow(engine, wf_id, run_id)
    store = _artifact_store_or_503()
    rows, _offset = store.read_audit(run_id)
    # Newest first feels natural for an audit view.
    rows.sort(key=lambda r: -(r.get("ts") or 0))
    return {"audit": rows, "count": len(rows)}
