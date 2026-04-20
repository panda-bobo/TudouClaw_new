"""Configuration and policy router — system config, tool policies, roles, audit, costs."""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse, parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.config")

router = APIRouter(prefix="/api/portal", tags=["config"])


# ---------------------------------------------------------------------------
# System configuration — matches legacy portal_routes_get /api/portal/config
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_system_config(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get system configuration (LLM config + role presets + providers)."""
    try:
        from ... import llm
        from ...agent import ROLE_PRESETS
        from ...llm import get_registry

        cfg = dict(llm.get_config())

        # Serialize role presets — AgentProfile is not JSON-serializable
        presets = {}
        for k, v in ROLE_PRESETS.items():
            preset = dict(v)
            if "profile" in preset and hasattr(preset["profile"], "__dataclass_fields__"):
                from dataclasses import asdict
                preset["profile"] = asdict(preset["profile"])
            presets[k] = preset
        cfg["role_presets"] = presets

        # Mask sensitive keys
        for key in ("openai_api_key", "claude_api_key", "unsloth_api_key"):
            if cfg.get(key):
                cfg[key] = "********"

        # Include providers from dynamic registry
        reg = get_registry()
        cfg["providers"] = [p.to_dict(mask_key=True) for p in reg.list(include_disabled=True)]
        cfg["available_models"] = reg.get_all_models()
        return cfg
    except Exception as e:
        logger.warning("get_system_config failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config")
async def update_system_config(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update system configuration."""
    try:
        from ... import llm
        cfg = llm.get_config()
        for k in ("provider", "model", "ollama_url", "openai_base_url",
                   "openai_api_key", "claude_api_key",
                   "unsloth_base_url", "unsloth_api_key"):
            if k in body and body[k]:
                cfg[k] = body[k]
        # global_system_prompt: allow empty string so users can clear it.
        if "global_system_prompt" in body:
            val = body.get("global_system_prompt")
            if isinstance(val, str):
                cfg["global_system_prompt"] = val
        # scene_prompts: list of {id, name, prompt, enabled}
        if "scene_prompts" in body:
            val = body.get("scene_prompts")
            if isinstance(val, list):
                cfg["scene_prompts"] = val
        llm.save_config()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool policy — matches legacy portal_routes_get /api/portal/policy
# ---------------------------------------------------------------------------

@router.get("/policy")
async def get_tool_policy(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get tool policy config."""
    try:
        from ...auth import get_auth
        auth = get_auth()
        return auth.tool_policy.get_policy_config()
    except Exception as e:
        logger.warning("get_tool_policy failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/policy")
async def update_tool_policy(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update tool policy."""
    try:
        from ...auth import get_auth
        auth = get_auth()
        auth.tool_policy.update_policy_config(body)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Tool approval workflow
# ---------------------------------------------------------------------------

@router.post("/approve")
async def approve_or_deny_tool_request(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Approve or deny a tool request — matches legacy portal_routes_post."""
    try:
        from ...auth import get_auth
        auth = get_auth()

        approval_id = body.get("approval_id", "")
        action = body.get("action", "")
        if not approval_id:
            raise HTTPException(400, "approval_id required")

        actor_name = getattr(user, "username", "") or getattr(user, "user_id", "")
        scope = body.get("scope", "once")
        ok = False

        if action == "approve":
            ok = auth.tool_policy.approve(approval_id, decided_by=actor_name, scope=scope)
        elif action == "deny":
            ok = auth.tool_policy.deny(approval_id, decided_by=actor_name)
        else:
            raise HTTPException(400, f"unknown action: {action}")

        if not ok:
            raise HTTPException(404, "approval not found or already decided")

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Web login request — human-in-the-loop credential submission
# ---------------------------------------------------------------------------

@router.post("/submit-login")
async def submit_web_login(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Submit credentials for a pending web login request from an agent."""
    try:
        from ...auth import submit_login
        request_id = body.get("request_id", "")
        if not request_id:
            raise HTTPException(400, "request_id required")
        credentials = {
            "username": body.get("username", ""),
            "password": body.get("password", ""),
            "cookies": body.get("cookies", ""),
            "token": body.get("token", ""),
        }
        ok = submit_login(request_id, credentials)
        if not ok:
            raise HTTPException(404, "Login request not found or already submitted")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset-login")
async def reset_login_guard(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Reset LoginGuard dedup for a domain so the user can retry login."""
    agent_id = body.get("agent_id", "")
    url = body.get("url", "")
    if not agent_id or not url:
        raise HTTPException(400, "agent_id and url required")
    try:
        agent = hub.agents.get(agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        guard = agent._get_login_guard()
        domain = guard._domain_key(url)
        if domain in guard._attempted:
            del guard._attempted[domain]
        return {"ok": True, "domain": domain}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Roles and permissions
# ---------------------------------------------------------------------------

@router.get("/roles")
async def get_available_roles(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get available user roles."""
    try:
        roles = hub.get_available_roles() if hasattr(hub, "get_available_roles") else []
        roles_list = [r.to_dict() if hasattr(r, "to_dict") else r for r in roles]
        return {"roles": roles_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Audit log — matches legacy: returns {"entries": [...]}
# ---------------------------------------------------------------------------

@router.get("/audit")
async def get_audit_log(
    action: str = Query("", description="Filter by action type"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get audit log entries."""
    try:
        from ...auth import get_auth
        auth = get_auth()
        entries = auth.get_audit_log(limit=500)
        if action:
            entries = [e for e in entries if e.get("action") == action]
        return {"entries": entries}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Cost analytics — matches legacy: returns hub.get_all_costs()
# ---------------------------------------------------------------------------

@router.get("/costs")
async def get_cost_analytics(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get cost analytics across all agents."""
    try:
        return hub.get_all_costs()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------

@router.get("/system-info")
async def get_system_info(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get system information."""
    try:
        return hub.get_system_info()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Hub state (dashboard)
# ---------------------------------------------------------------------------

@router.get("/state")
async def get_hub_state(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get full hub state for dashboard — matches legacy portal_routes_get."""
    try:
        top_agents = [a for a in hub.list_agents() if not a.get("parent_id")]
        _agent_name_map = {}
        for _a in hub.agents.values():
            _agent_name_map[_a.id] = f"{_a.role}-{_a.name}" if _a.role else _a.name

        def _enrich_msg(m):
            d = m.to_dict() if hasattr(m, "to_dict") else m
            if isinstance(d, dict):
                d["from_agent_name"] = _agent_name_map.get(d.get("from_agent", ""), d.get("from_agent", ""))
                d["to_agent_name"] = _agent_name_map.get(d.get("to_agent", ""), d.get("to_agent", ""))
            return d

        # Approvals
        approvals = {}
        try:
            from ...server.portal_routes_get import get_approvals
            approvals = get_approvals()
        except Exception:
            approvals = {"pending": [], "history": []}

        # Portal mode
        portal_mode = "hub"
        try:
            from ...server.portal_routes_get import get_portal_mode
            portal_mode = get_portal_mode()
        except Exception:
            pass

        return {
            "agents": top_agents,
            "nodes": hub.list_nodes(),
            "messages": [_enrich_msg(m) for m in hub.messages[-100:]],
            "approvals": approvals,
            "summary": hub.summary(),
            "portal_mode": portal_mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Pending reviews — matches legacy: returns {"count": N, "items": [...]}
# ---------------------------------------------------------------------------

@router.get("/pending-reviews")
async def get_pending_reviews(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get all pending review items across projects."""
    try:
        items = []
        for proj in hub.projects.values():
            for task in proj.tasks:
                for step in (getattr(task, "steps", None) or []):
                    if getattr(step, "status", "") != "awaiting_review":
                        continue
                    items.append({
                        "proj_id": proj.id,
                        "proj_name": proj.name,
                        "task_id": task.id,
                        "task_title": task.title,
                        "assignee": task.assigned_to,
                        "step": step.to_dict(),
                    })
        items.sort(key=lambda it: it["step"].get("completed_at", 0) or 0)
        return {"count": len(items), "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Enhancement presets
# ---------------------------------------------------------------------------

@router.get("/enhancement-presets")
async def get_enhancement_presets(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get list of enhancement preset templates."""
    try:
        from ...enhancement import list_enhancement_presets
        return {"presets": list_enhancement_presets()}
    except (ImportError, Exception):
        return {"presets": []}


# ---------------------------------------------------------------------------
# Config deployments
# ---------------------------------------------------------------------------

@router.get("/config-deployments")
async def get_config_deployments(
    deploy_id: str = Query("", alias="id", description="Filter by deployment ID"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get config deployment status."""
    try:
        if deploy_id:
            return hub.get_deployment_status(deploy_id)
        return {"deployments": hub.get_deployment_status()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Growth paths
# ---------------------------------------------------------------------------

@router.get("/growth-paths")
async def get_growth_paths(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get all role growth path templates."""
    try:
        from ...core.role_growth_path import ROLE_GROWTH_PATHS
        paths = {}
        for role, gp in ROLE_GROWTH_PATHS.items():
            paths[role] = gp.get_summary()
        return {"paths": paths, "total_roles": len(paths)}
    except (ImportError, Exception):
        return {"paths": {}, "total_roles": 0}


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

@router.get("/tool-surface")
async def get_tool_surface(
    q: str = Query("", description="Filter query"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get tool surface index."""
    try:
        return {"index": hub.get_tool_surface(q)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Parity report
# ---------------------------------------------------------------------------

@router.get("/parity-report")
async def get_parity_report(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get parity report."""
    try:
        return hub.get_parity_report()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workspace summary
# ---------------------------------------------------------------------------

@router.get("/workspace-summary")
async def get_workspace_summary(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get workspace summary."""
    try:
        return {"summary": hub.get_workspace_summary()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Smart routing
# ---------------------------------------------------------------------------

@router.get("/smart-route")
async def smart_route(
    q: str = Query("", description="Query to route"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Route a query to the best-fit agent."""
    if not q:
        raise HTTPException(status_code=400, detail="Missing q parameter")
    try:
        return hub.route_and_dispatch(q)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Orchestration graph
# ---------------------------------------------------------------------------

@router.get("/orchestration")
async def get_orchestration(
    project: str = Query("", description="Filter by project ID"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get orchestration topology graph (agents, projects, tasks)."""
    try:
        from ...server.portal_routes_get import _build_orchestration_graph
        return _build_orchestration_graph(hub, project)
    except ImportError:
        # Inline fallback
        graph = {"nodes": [], "edges": []}
        try:
            for a in hub.agents.values():
                graph["nodes"].append({"id": a.id, "type": "agent", "label": a.name})
            for p in hub.projects.values():
                graph["nodes"].append({"id": p.id, "type": "project", "label": p.name})
                for member in (p.members or []):
                    graph["edges"].append({"from": member, "to": p.id, "type": "member"})
        except Exception:
            pass
        return graph
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Aggregated audit log (cross-node)
# ---------------------------------------------------------------------------

@router.get("/audit/aggregated")
async def get_aggregated_audit(
    limit: int = Query(500, ge=1, le=5000),
    action: str = Query("", description="Filter by action"),
    actor: str = Query("", description="Filter by actor"),
    node: str = Query("", description="Filter by node"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get aggregated audit log across nodes."""
    try:
        from ...auth import get_auth
        auth_mgr = get_auth()
        entries = auth_mgr.get_audit_log(
            limit=max(1, min(limit, 5000)),
            action=action or None,
            actor=actor or None,
        )
        entries_list = [e.to_dict() if hasattr(e, "to_dict") else e for e in entries]
        if node:
            entries_list = [e for e in entries_list if e.get("node", "") == node]
        return {"entries": entries_list, "count": len(entries_list)}
    except (ImportError, Exception) as e:
        raise HTTPException(status_code=500, detail=str(e))
