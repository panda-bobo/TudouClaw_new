"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
Hub-Node communication handlers extracted from portal_routes_post.py.

Handles Hub-Node synchronisation, message delivery, config deployment,
broadcast and orchestration endpoints:

  - POST /api/hub/register
  - POST /api/hub/sync
  - POST /api/hub/refresh
  - POST /api/hub/message
  - POST /api/hub/deliver
  - POST /api/hub/dispatch-config
  - POST /api/hub/batch-dispatch-config
  - POST /api/hub/apply-config
  - POST /api/hub/confirm-config
  - POST /api/portal/node/{nid}/config/sync
  - POST /api/portal/node/{nid}/config
  - POST /api/hub/apply-node-config
  - POST /api/hub/broadcast
  - POST /api/hub/orchestrate
"""
import logging
import re
import threading
import uuid

from ...auth import Role
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_hub_mode():
    from ..portal_server import _portal_mode
    return _portal_mode == "hub"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth, actor_name: str, user_role: str) -> bool:
    """Handle Hub-Node communication endpoints.

    Returns True if the path was handled, False otherwise.
    """

    # ---- POST /api/hub/register ----
    if path == "/api/hub/register":
        nid = body.get("node_id") or uuid.uuid4().hex[:8]
        hub.register_node(
            node_id=nid,
            name=body.get("name", "remote"),
            url=body.get("url", ""),
            agents=body.get("agents", []),
        )
        auth.audit("register_node", actor=actor_name, role=user_role,
                   target=nid, ip=get_client_ip(handler))
        handler._json({"ok": True, "node_id": nid})
        return True

    # ---- POST /api/hub/sync ----
    elif path == "/api/hub/sync":
        nid = body.get("node_id", "")
        if nid:
            hub.update_node_agents(nid, body.get("agents", []))
        handler._json({"ok": True})
        return True

    # ---- POST /api/hub/refresh ----
    elif path == "/api/hub/refresh":
        nid = body.get("node_id", "")
        ok = hub.refresh_node(nid) if nid else False
        handler._json({"ok": ok})
        return True

    # ---- POST /api/hub/message ----
    elif path == "/api/hub/message":
        msg = hub.send_message(
            from_agent=body.get("from_agent", "hub"),
            to_agent=body.get("to_agent", ""),
            content=body.get("content", ""),
            msg_type=body.get("msg_type", "task"),
        )
        auth.audit("send_message", get_client_ip(handler),
                   role=user_role, target=body.get("to_agent", ""),
                   success=True)
        handler._json(msg.to_dict())
        return True

    # ---- POST /api/hub/deliver ----
    elif path == "/api/hub/deliver":
        to_id = body.get("to_agent", "")
        agent = hub.get_agent(to_id)
        if agent:
            content = body.get("content", "")
            from_id = body.get("from_agent", "remote")
            threading.Thread(
                target=agent.delegate, args=(content, from_id), daemon=True
            ).start()
            handler._json({"ok": True})
        else:
            handler._json({"error": "Agent not found"}, 404)
        return True

    # ---- Config deployment endpoints ----

    # ---- POST /api/hub/dispatch-config ----
    elif path == "/api/hub/dispatch-config":
        # Push config to a specific agent on a specific node
        from ...hub import AgentConfigPayload
        node_id = body.get("node_id", "local")
        agent_id = body.get("agent_id", "")
        config = AgentConfigPayload.from_dict(body.get("config", {}))
        dep = hub.dispatch_config(node_id, agent_id, config)
        auth.audit("dispatch_config", actor=actor_name, role=user_role,
                   target=f"{node_id}/{agent_id}", ip=get_client_ip(handler))
        handler._json(dep.to_dict())
        return True

    # ---- POST /api/hub/batch-dispatch-config ----
    elif path == "/api/hub/batch-dispatch-config":
        # Push configs to multiple agents across nodes
        configs = body.get("configs", [])
        deps = hub.batch_dispatch_config(configs)
        auth.audit("batch_dispatch_config", actor=actor_name, role=user_role,
                   target=f"{len(deps)} deployments", ip=get_client_ip(handler))
        handler._json({"deployments": [d.to_dict() for d in deps]})
        return True

    # ---- POST /api/hub/apply-config ----
    elif path == "/api/hub/apply-config":
        # Receive config from hub (called BY remote nodes, or by self for local)
        from ...hub import AgentConfigPayload
        deploy_id = body.get("deploy_id", "")
        agent_id = body.get("agent_id", "")
        config = AgentConfigPayload.from_dict(body.get("config", {}))
        ok = hub.apply_config_to_local_agent(agent_id, config)
        # If we have a deploy_id, confirm back to the caller
        handler._json({"ok": ok, "applied": ok,
                       "deploy_id": deploy_id,
                       "agent_id": agent_id})
        return True

    # ---- POST /api/hub/confirm-config ----
    elif path == "/api/hub/confirm-config":
        # Remote node confirms config was applied
        deploy_id = body.get("deploy_id", "")
        success = body.get("success", True)
        error = body.get("error", "")
        ok = hub.confirm_config_applied(deploy_id, success, error)
        handler._json({"ok": ok})
        return True

    # ---- Node-scoped config management ----

    # ---- POST /api/portal/node/{nid}/config/sync ----
    m = re.match(r"^/api/portal/node/([^/]+)/config/sync$", path)
    if m:
        # Push config to remote node
        nid = m.group(1)
        if not is_hub_mode():
            handler._json({"error": "Only Hub can sync config to nodes"}, 403)
            return True
        if not Role(user_role).can("manage_config"):
            handler._json({"error": "Admin role required"}, 403)
            return True
        result = hub.sync_node_config(nid)
        auth.audit("sync_node_config", actor=actor_name, role=user_role,
                   target=nid, ip=get_client_ip(handler))
        handler._json(result)
        return True

    # ---- POST /api/portal/node/{nid}/config ----
    m = re.match(r"^/api/portal/node/([^/]+)/config$", path)
    if m:
        # Set/update or delete a config item for a node
        nid = m.group(1)
        # Permission check: admin can edit any; node can only edit own
        if not is_hub_mode() and nid != "local":
            handler._json({"error": "Node mode: can only modify own config"}, 403)
            return True
        if is_hub_mode() and not Role(user_role).can("manage_config"):
            handler._json({"error": "Admin role required for config management"}, 403)
            return True
        action = body.get("action", "set")
        if action == "delete":
            key = body.get("key", "")
            ok = hub.delete_node_config_item(nid, key)
            auth.audit("delete_node_config", actor=actor_name, role=user_role,
                       target=f"{nid}/{key}", ip=get_client_ip(handler))
            handler._json({"ok": ok, "deleted": key})
        else:
            item = hub.set_node_config_item(
                node_id=nid,
                key=body.get("key", ""),
                value=body.get("value", ""),
                description=body.get("description", ""),
                category=body.get("category", "general"),
                is_secret=body.get("is_secret", False),
                created_by=actor_name,
            )
            auth.audit("set_node_config", actor=actor_name, role=user_role,
                       target=f"{nid}/{item.key}", ip=get_client_ip(handler))
            handler._json(item.to_dict(mask=True))
        return True

    # ---- POST /api/hub/apply-node-config ----
    elif path == "/api/hub/apply-node-config":
        # Receive config push from Hub (called on remote nodes)
        result = hub.apply_received_node_config(body)
        handler._json(result)
        return True

    # ---- POST /api/hub/broadcast ----
    elif path == "/api/hub/broadcast":
        msgs = hub.broadcast(body.get("content", ""))
        handler._json({"sent": len(msgs)})
        return True

    # ---- POST /api/hub/orchestrate ----
    elif path == "/api/hub/orchestrate":
        results = hub.orchestrate(
            body.get("task", ""),
            body.get("agent_ids"),
        )
        handler._json({"results": {k: v[:2000] for k, v in results.items()}})
        return True

    return False
