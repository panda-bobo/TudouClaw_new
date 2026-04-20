"""Workflow management router — list, templates, catalog, CRUD, execution."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.workflows")

router = APIRouter(prefix="/api/portal", tags=["workflows"])


def _get_workflow_or_404(hub, workflow_id: str):
    """Get workflow or raise 404."""
    try:
        workflow = hub.get_workflow(workflow_id) if hasattr(hub, "get_workflow") else None
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow '{workflow_id}' not found")
        return workflow
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workflow listing
# ---------------------------------------------------------------------------

@router.get("/workflows")
async def list_workflows(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all workflows."""
    try:
        workflows = hub.list_workflows() if hasattr(hub, "list_workflows") else []
        workflows_list = [w.to_dict() if hasattr(w, "to_dict") else w for w in workflows]
        return {"workflows": workflows_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflow-templates")
async def list_workflow_templates(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List available workflow templates — matches legacy portal_routes_get."""
    try:
        from ...workflow import list_workflow_templates as _lwt
        return {"templates": _lwt()}
    except (ImportError, Exception):
        return {"templates": []}


@router.get("/workflow-catalog")
async def get_workflow_catalog(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get workflow catalog — matches legacy portal_routes_get."""
    try:
        from ...server.data.workflow_catalog import list_catalog_templates, get_catalog_categories
        return {
            "catalog": list_catalog_templates(),
            "categories": get_catalog_categories(),
        }
    except ImportError:
        try:
            from app.data.workflow_catalog import list_catalog_templates, get_catalog_categories
            return {
                "catalog": list_catalog_templates(),
                "categories": get_catalog_categories(),
            }
        except ImportError:
            return {"catalog": [], "categories": {}}


# ---------------------------------------------------------------------------
# Single workflow
# ---------------------------------------------------------------------------

@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get workflow detail."""
    try:
        workflow = _get_workflow_or_404(hub, workflow_id)
        data = workflow.to_dict() if hasattr(workflow, "to_dict") else workflow
        return data if isinstance(data, dict) else {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workflow CRUD
# ---------------------------------------------------------------------------

@router.post("/workflows")
async def manage_workflows(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create, update, or delete a workflow."""
    try:
        action = body.get("action", "create")

        if action == "create":
            workflow = hub.create_workflow(body) if hasattr(hub, "create_workflow") else {}
            return {"ok": True, "workflow": workflow}
        elif action == "update":
            workflow = hub.update_workflow(body.get("workflow_id"), body) if hasattr(hub, "update_workflow") else {}
            return {"ok": True, "workflow": workflow}
        elif action == "delete":
            hub.delete_workflow(body.get("workflow_id")) if hasattr(hub, "delete_workflow") else None
            return {"ok": True}
        else:
            raise HTTPException(400, f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workflow execution
# ---------------------------------------------------------------------------

@router.post("/workflows/{workflow_id}/start")
async def start_workflow(
    workflow_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Start a workflow."""
    try:
        workflow = _get_workflow_or_404(hub, workflow_id)
        params = body.get("params", {})

        if hasattr(workflow, "start"):
            result = workflow.start(params)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/{workflow_id}/abort")
async def abort_workflow(
    workflow_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Abort a running workflow."""
    try:
        workflow = _get_workflow_or_404(hub, workflow_id)

        if hasattr(workflow, "abort"):
            workflow.abort()

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
