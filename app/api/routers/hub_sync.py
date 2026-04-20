"""Hub sync and inter-node communication router — agent sync, registration, messaging."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.hub_sync")

router = APIRouter(prefix="/api/hub", tags=["hub"])


# ---------------------------------------------------------------------------
# Local agents for inter-node sync
# ---------------------------------------------------------------------------

@router.get("/agents")
async def get_local_agents_for_sync(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get local agents for inter-node synchronization."""
    try:
        agents = hub.get_local_agents_for_sync() if hasattr(hub, "get_local_agents_for_sync") else []
        agents_list = [a.to_dict() if hasattr(a, "to_dict") else a for a in agents]
        return {"agents": agents_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/node/{node_id}")
async def unregister_node(
    node_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Unregister a remote node from this Hub.

    Parity with legacy ``DELETE /api/hub/node/{id}`` in
    ``portal_routes_post.py``. Used when a node is decommissioned or
    offline long enough that the operator wants it removed from the
    dashboard.
    """
    try:
        hub.unregister_node(node_id)
        try:
            from ...auth import get_auth
            auth = get_auth()
            auth.audit("delete_node", actor=user.user_id,
                       role=user.role, target=node_id)
        except Exception:
            pass
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Remote node registration
# ---------------------------------------------------------------------------

@router.post("/register")
async def register_remote_node(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Register a remote node for synchronization."""
    try:
        node_id = body.get("node_id", "")
        endpoint = body.get("endpoint", "")
        if not node_id or not endpoint:
            raise HTTPException(400, "Missing node_id or endpoint")

        if hasattr(hub, "register_remote_node"):
            result = hub.register_remote_node(node_id, endpoint, body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Agent synchronization
# ---------------------------------------------------------------------------

@router.post("/sync")
async def sync_agents(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Sync agents with remote nodes."""
    try:
        node_id = body.get("node_id", "")
        agent_ids = body.get("agent_ids", [])

        if hasattr(hub, "sync_agents"):
            result = hub.sync_agents(node_id, agent_ids, body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Heartbeat (keepalive)
# ---------------------------------------------------------------------------

@router.post("/heartbeat")
async def send_heartbeat(
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Send keepalive heartbeat to hub and remote nodes."""
    try:
        node_id = body.get("node_id", "")

        if hasattr(hub, "send_heartbeat"):
            result = hub.send_heartbeat(node_id, body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------

@router.post("/message")
async def send_inter_agent_message(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Send a message between agents across nodes."""
    try:
        from_agent_id = body.get("from_agent_id", "")
        to_agent_id = body.get("to_agent_id", "")
        message = body.get("message", "")
        if not from_agent_id or not to_agent_id or not message:
            raise HTTPException(400, "Missing from_agent_id, to_agent_id, or message")

        if hasattr(hub, "send_inter_agent_message"):
            result = hub.send_inter_agent_message(from_agent_id, to_agent_id, message, body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Broadcast messaging
# ---------------------------------------------------------------------------

@router.post("/broadcast")
async def broadcast_message(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Broadcast a message to all nodes."""
    try:
        message = body.get("message", "")
        message_type = body.get("type", "info")
        if not message:
            raise HTTPException(400, "Missing message")

        if hasattr(hub, "broadcast"):
            msgs = hub.broadcast(body.get("content", message))
            return {"ok": True, "sent": len(msgs) if isinstance(msgs, list) else 0}

        if hasattr(hub, "broadcast_message"):
            result = hub.broadcast_message(message, message_type=message_type, data=body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Node refresh
# ---------------------------------------------------------------------------

@router.post("/refresh")
async def refresh_node(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Refresh a remote node's agent data."""
    try:
        node_id = body.get("node_id", "")
        if not node_id:
            raise HTTPException(400, "Missing node_id")

        ok = hub.refresh_node(node_id) if hasattr(hub, "refresh_node") else False
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Message delivery (delegate to local agent)
# ---------------------------------------------------------------------------

@router.post("/deliver")
async def deliver_message(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Deliver a message to a local agent (trigger delegation)."""
    import threading

    try:
        to_id = body.get("to_agent", "")
        if not to_id:
            raise HTTPException(400, "Missing to_agent")

        agent = hub.get_agent(to_id) if hasattr(hub, "get_agent") else None
        if not agent:
            raise HTTPException(404, "Agent not found")

        content = body.get("content", "")
        from_id = body.get("from_agent", "remote")
        threading.Thread(
            target=agent.delegate, args=(content, from_id), daemon=True
        ).start()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Config deployment: dispatch, batch-dispatch, apply, confirm
# ---------------------------------------------------------------------------

@router.post("/dispatch-config")
async def dispatch_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Push config to a specific agent on a specific node."""
    try:
        from ...hub import AgentConfigPayload

        node_id = body.get("node_id", "local")
        agent_id = body.get("agent_id", "")
        if not agent_id:
            raise HTTPException(400, "Missing agent_id")

        config = AgentConfigPayload.from_dict(body.get("config", {}))
        dep = hub.dispatch_config(node_id, agent_id, config)
        return dep.to_dict() if hasattr(dep, "to_dict") else {"ok": True, "result": dep}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch-dispatch-config")
async def batch_dispatch_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Push configs to multiple agents across nodes."""
    try:
        configs = body.get("configs", [])
        if not configs:
            raise HTTPException(400, "Missing configs")

        deps = hub.batch_dispatch_config(configs)
        return {"deployments": [d.to_dict() for d in deps]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-config")
async def apply_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Receive config from hub (called by remote nodes, or by self for local)."""
    try:
        from ...hub import AgentConfigPayload

        deploy_id = body.get("deploy_id", "")
        agent_id = body.get("agent_id", "")
        config = AgentConfigPayload.from_dict(body.get("config", {}))
        ok = hub.apply_config_to_local_agent(agent_id, config) if hasattr(hub, "apply_config_to_local_agent") else False
        return {
            "ok": ok,
            "applied": ok,
            "deploy_id": deploy_id,
            "agent_id": agent_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/confirm-config")
async def confirm_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Remote node confirms config was applied."""
    try:
        deploy_id = body.get("deploy_id", "")
        success = body.get("success", True)
        error = body.get("error", "")
        ok = hub.confirm_config_applied(deploy_id, success, error) if hasattr(hub, "confirm_config_applied") else False
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-node-config")
async def apply_node_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Receive config push from Hub (called on remote nodes)."""
    try:
        result = hub.apply_received_node_config(body) if hasattr(hub, "apply_received_node_config") else {"ok": False}
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Multi-agent orchestration
# ---------------------------------------------------------------------------

@router.post("/orchestrate")
async def orchestrate_agents(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Orchestrate a task across multiple agents."""
    try:
        task = body.get("task", "")
        agent_ids = body.get("agent_ids")
        if not task:
            raise HTTPException(400, "Missing task")

        results = hub.orchestrate(task, agent_ids) if hasattr(hub, "orchestrate") else {}
        return {"results": {k: v[:2000] if isinstance(v, str) else v for k, v in results.items()}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Cross-node audit ingest
# ---------------------------------------------------------------------------

@router.post("/audit/ingest")
async def audit_ingest(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Ingest audit entries from remote nodes."""
    try:
        entries = body.get("entries") or []
        source_node = body.get("source_node", "")
        if not isinstance(entries, list):
            raise HTTPException(400, "entries must be a list")

        from ...auth import get_auth
        auth = get_auth()
        count = auth.ingest_remote_audit(entries, source_node=source_node) if hasattr(auth, "ingest_remote_audit") else 0
        return {"ok": True, "ingested": count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
