"""Visual orchestration canvas — CRUD endpoints for the drag-drop DAG editor.

Distinct from the legacy `/workflows` router (which manages the
state-machine task workflows in app/workflow.py). Canvas workflows are
the user-authored DAGs (nodes + edges + config) saved by the
Orchestration → Canvas tab.

URL prefix: `/api/portal/canvas-workflows`
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, Body

from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.canvas")

router = APIRouter(prefix="/api/portal", tags=["canvas-workflows"])


def _store_or_503():
    from ...canvas_workflows import get_store
    s = get_store()
    if s is None:
        raise HTTPException(503, "canvas workflow store not initialized")
    return s


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
