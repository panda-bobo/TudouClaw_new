"""Node management router — list nodes, configure nodes, sync configuration."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.nodes")

router = APIRouter(prefix="/api/portal", tags=["nodes"])


def _get_node_or_404(hub, node_id: str):
    """Get node or raise 404."""
    try:
        node = hub.get_node(node_id) if hasattr(hub, "get_node") else None
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        return node
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Node listing
# ---------------------------------------------------------------------------

@router.get("/nodes")
async def list_nodes(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all nodes — matches legacy portal_routes_get.

    Includes a virtual "local" node entry and per-node project/task counts.
    """
    try:
        # Remote nodes from node manager
        nodes_list = []
        try:
            from ...server.infra.node_manager import get_node_manager
            nm = get_node_manager()
            nodes_list = [n.__dict__ if hasattr(n, '__dict__') else n
                          for n in (nm.list_nodes() if nm else [])]
        except Exception:
            pass

        # All projects for counting
        try:
            all_projects = list(hub.projects.values()) if hub and hasattr(hub, "projects") else []
        except Exception:
            all_projects = []

        def _count_for_node(nid: str) -> dict:
            ps = [p for p in all_projects
                  if (getattr(p, "node_id", "local") or "local") == nid]
            t = sum(len(getattr(p, "tasks", []) or []) for p in ps)
            paused_count = sum(1 for p in ps if getattr(p, "paused", False))
            return {"project_count": len(ps), "task_count": t,
                    "paused_count": paused_count}

        # Build local virtual node
        local_node = {
            "node_id": "local",
            "name": "本机 (Local)",
            "status": "online",
            "agent_count": len([a for a in (hub.agents if hub else {}).values()
                                if (getattr(a, 'node_id', 'local') or 'local') == 'local']),
            "capabilities": {},
        }
        local_node.update(_count_for_node("local"))

        # Add counts to remote nodes
        for n in nodes_list:
            nid = n.get("node_id", "")
            if nid:
                n.update(_count_for_node(nid))

        return {"nodes": [local_node] + nodes_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/node-configs")
async def list_node_configs(
    mask: str = Query("1", description="Mask sensitive values (0=no)"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all node configurations — matches legacy portal_routes_get."""
    try:
        do_mask = mask != "0"
        return {"configs": hub.list_all_node_configs(mask=do_mask)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Node configuration
# ---------------------------------------------------------------------------

@router.get("/node/{node_id}/config")
async def get_node_config(
    node_id: str,
    mask: str = Query("1", description="Mask sensitive values (0=no)"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get config for a specific node — matches legacy portal_routes_get."""
    try:
        do_mask = mask != "0"
        return hub.get_node_config(node_id, mask=do_mask)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/node/{node_id}/config")
async def update_node_config(
    node_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update node configuration."""
    try:
        node = _get_node_or_404(hub, node_id)

        if hasattr(node, "update_config"):
            node.update_config(body)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/node/{node_id}/config-status")
async def get_node_config_status(
    node_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get config deployment status for a specific node."""
    try:
        status = hub.get_node_config_status(node_id) if hasattr(hub, "get_node_config_status") else {}
        return status if isinstance(status, dict) else {"status": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/node/{node_id}/config/sync")
async def sync_config_to_node(
    node_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Sync configuration to a node."""
    try:
        node = _get_node_or_404(hub, node_id)

        if hasattr(node, "sync_config"):
            result = node.sync_config(body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
