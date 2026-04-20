"""
Portal GET route handlers.

╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib HTTP handler                          ║
║                                                                        ║
║  FastAPI (app/api/routers/*) is now the authoritative production       ║
║  path. This file runs ONLY when TUDOU_USE_STDLIB=1 is set at launch.   ║
║                                                                        ║
║  Do NOT add new routes here. New GETs go into                          ║
║  app/api/routers/<domain>.py with @router.get(...).                    ║
║                                                                        ║
║  Scheduled for deletion: after one full release cycle of stable        ║
║  FastAPI operation (≥30 days without rollback).                        ║
╚════════════════════════════════════════════════════════════════════════╝
"""
import json
import logging
import os
import re
import time
from urllib.parse import urlparse, parse_qs

from ..hub import get_hub
from ..auth import get_auth
from .. import llm, tools, knowledge
from ..defaults import MAX_FILE_SERVE, PROJECT_SCAN_MAX_FILES
from ..agent import (Agent, AgentStatus, AgentEvent, AgentTask, TaskStatus,
                     ROLE_PRESETS, AgentProfile, MCPServerConfig,
                     ChatTask, ChatTaskStatus, get_chat_task_manager)
from ..enhancement import list_enhancement_presets, ENHANCEMENT_PRESET_INFO
from ..scheduler import get_scheduler, PRESET_JOBS
from ..mcp.manager import get_mcp_manager, MCP_CATALOG
from ..template_library import get_template_library
from ..llm import get_registry
from ..channel import get_router

from .portal_templates import _LOGIN_HTML, _PORTAL_HTML
from .portal_auth import (get_client_ip, get_session_cookie, set_session_cookie,
                           require_auth, get_auth_info, get_admin_context,
                           is_super_admin, get_visible_agents)

logger = logging.getLogger("tudou.portal")


# ───────────── MCP source resolver (supports legacy + new layout) ─────────────
_MCP_SOURCE_MODULE_RE = re.compile(
    r"python\s+-m\s+(app(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b"
)


def _ct_compact(t) -> dict:
    """Compact serialisation of a ConversationTask for list endpoints.

    Drops heavy per-step tool_calls bodies (UI fetches detail per task
    when expanded) so the list payload stays small even with a backlog
    of 50 rows. Kept fields are everything the TASK QUEUE row renderer
    needs to show progress + summary.
    """
    steps = []
    for s in (t.steps or []):
        steps.append({
            "id": s.id,
            "goal": s.goal,
            "tool_hint": s.tool_hint,
            "status": s.status,
            "tool_call_count": len(s.tool_calls or []),
        })
    return {
        "id": t.id,
        "agent_id": t.agent_id,
        "title": t.title or (t.intent[:40] + "…" if t.intent else ""),
        "intent": t.intent,
        "status": t.status,
        "steps": steps,
        "current_step_idx": t.current_step_idx,
        "tool_call_total": t.tool_call_total,
        "chat_task_id": t.chat_task_id,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "completed_at": t.completed_at,
    }


def _mcp_source_resolver():
    """Return (app_dir, proj_root, resolve_fn).

    resolve_fn(command_template) → (dotted_module, abs_file_path) | None

    Accepts any ``python -m app.X.Y.Z`` command and maps it to
    ``<proj_root>/app/X/Y/Z.py``. Both the new layout
    ``app.mcp.builtins.jimeng_video`` and the legacy flat layout
    ``app.tudou_jimeng_video_mcp`` are resolved uniformly. The caller
    is still responsible for a path-traversal check against ``app_dir``.
    """
    app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    proj_root = os.path.normpath(os.path.join(app_dir, ".."))

    def resolve(cmd_tmpl: str):
        if not cmd_tmpl:
            return None
        m = _MCP_SOURCE_MODULE_RE.search(cmd_tmpl)
        if not m:
            return None
        dotted = m.group(1)
        rel = dotted.replace(".", os.sep) + ".py"
        fpath = os.path.normpath(os.path.join(proj_root, rel))
        return dotted, fpath

    return app_dir, proj_root, resolve


def get_approvals() -> dict:
    """Get pending and recent approval records from auth module."""
    auth = get_auth()
    return {
        "pending": auth.tool_policy.list_pending(),
        "history": auth.tool_policy.list_history(50),
    }


def get_portal_mode() -> str:
    from .portal_server import get_portal_mode as _get_portal_mode
    return _get_portal_mode()


def is_hub_mode() -> bool:
    from .portal_server import is_hub_mode as _is_hub_mode
    return _is_hub_mode()


def _build_orchestration_graph(hub, project_filter: str = "") -> dict:
    """Build a node/edge graph for the orchestration visualization view.

    Node types:
      - project        (entity:project)
      - agent          (top-level)
      - subagent       (parent_id != "")
      - task           (project task)
    Edge types:
      - member         (project → agent)
      - assigned       (agent → task)
      - parent         (parent agent → sub-agent)
      - belongs_to     (task → project)
    """
    nodes: list = []
    edges: list = []
    seen_agents = set()

    projects = list(hub.projects.values()) if hasattr(hub, "projects") else []
    if project_filter:
        projects = [p for p in projects if p.id == project_filter]

    # Project nodes + member edges + task nodes
    for proj in projects:
        nodes.append({
            "id": f"proj:{proj.id}",
            "type": "project",
            "label": proj.name or proj.id,
            "status": "paused" if getattr(proj, "paused", False) else "active",
            "task_count": len(getattr(proj, "tasks", []) or []),
            "member_count": len(getattr(proj, "members", []) or []),
        })
        for m in getattr(proj, "members", []) or []:
            aid = getattr(m, "agent_id", "") if not isinstance(m, dict) else m.get("agent_id", "")
            if not aid:
                continue
            edges.append({"from": f"proj:{proj.id}", "to": f"agent:{aid}", "type": "member"})
        for t in getattr(proj, "tasks", []) or []:
            done, total = (t.step_progress() if hasattr(t, "step_progress") else (0, 0))
            nodes.append({
                "id": f"task:{proj.id}:{t.id}",
                "type": "task",
                "label": (t.title or "")[:48],
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "assigned_to": t.assigned_to,
                "step_done": done,
                "step_total": total,
            })
            edges.append({
                "from": f"task:{proj.id}:{t.id}",
                "to": f"proj:{proj.id}",
                "type": "belongs_to",
            })
            if t.assigned_to:
                edges.append({
                    "from": f"agent:{t.assigned_to}",
                    "to": f"task:{proj.id}:{t.id}",
                    "type": "assigned",
                })

    # Agent + sub-agent nodes
    for ag in (hub.agents.values() if hasattr(hub, "agents") else []):
        if project_filter:
            # Only include agents that are members of the filtered project(s)
            in_proj = any(
                any((getattr(m, "agent_id", "") == ag.id if not isinstance(m, dict)
                     else m.get("agent_id", "") == ag.id)
                    for m in (getattr(p, "members", []) or []))
                for p in projects
            )
            if not in_proj and not ag.parent_id:
                continue
        is_sub = bool(ag.parent_id)
        nodes.append({
            "id": f"agent:{ag.id}",
            "type": "subagent" if is_sub else "agent",
            "label": f"{ag.role or ''}-{ag.name}".strip("-"),
            "role": ag.role,
            "status": getattr(ag, "status", ""),
            "node_id": getattr(ag, "node_id", "") or "local",
            "parent_id": ag.parent_id,
            "task_count": len([t for t in getattr(ag, "tasks", []) or []
                               if getattr(t.status, "value", t.status) != "done"]),
            "granted_skills": len(getattr(ag, "granted_skills", []) or []),
        })
        seen_agents.add(ag.id)
        if ag.parent_id:
            edges.append({
                "from": f"agent:{ag.parent_id}",
                "to": f"agent:{ag.id}",
                "type": "parent",
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "projects": len(projects),
            "agents": sum(1 for n in nodes if n["type"] == "agent"),
            "subagents": sum(1 for n in nodes if n["type"] == "subagent"),
            "tasks": sum(1 for n in nodes if n["type"] == "task"),
        },
    }


def handle_get(handler):
    """Main GET dispatcher - called from _PortalHandler.do_GET().

    DEPRECATED — see the file-level deprecation block. Active only when
    TUDOU_USE_STDLIB=1 is set at launch.
    """
    path = urlparse(handler.path).path
    # Static & documents don't need the noisy warning.
    if not (path.startswith("/static") or path == "/favicon.ico"):
        logger.warning(
            "[stdlib-fallback] GET %s hitting legacy handler "
            "(TUDOU_USE_STDLIB=1 is active). Migrate to FastAPI.", path)
    try:
        _do_get_inner(handler, path)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
        # 客户端提前断开连接（刷新/导航/移动端切后台）不是服务端 bug，
        # 只在 debug 级别记录，避免日志噪音。
        logger.debug("Client disconnected during GET %s: %s", path, e)
        return
    except Exception as e:
        import traceback as _tb
        logger.error("Unhandled exception in GET %s: %s\n%s", path, e, _tb.format_exc())
        if path.startswith("/api/"):
            try:
                handler._json({"error": f"{type(e).__name__}: {e}"}, status=500)
            except Exception:
                pass
        else:
            try:
                handler.send_error(500, f"Server error: {e}")
            except Exception:
                pass


def _enrich_meeting_messages_with_refs(hub, msg_dicts):
    """In-place: walk meeting message dicts, look up each sender as an
    agent, and attach a ``refs`` list extracted from message content.

    Each message's ``base_dir`` is the SENDER agent's effective working
    directory, so a participant referencing files in their own workspace
    gets correct cards even when the meeting spans multiple agents.

    The artifact URL routes through ``html_tag_router`` for that
    sender. To make the click resolve, this function ALSO ingests the
    extracted candidates into the sender agent's shadow store (idempotent
    via dedup-by-value), mirroring how live agent chat works. If the
    sender has no shadow / no agent record, refs come back as URL-only
    or get dropped — never raises.
    """
    if not msg_dicts:
        return
    try:
        from ..agent_state.file_refs import build_refs_from_text
        from ..agent_state.extractors import (
            extract_from_text, normalize_path_candidates, ingest_into_store,
        )
        from ..agent_state.artifact import ProducedBy
        from ..agent_state.shadow import install_into_agent
        from .html_tag_router import build_artifact_url
    except Exception as _e:
        logger.debug("meeting enrich: import failed: %s", _e)
        for md in msg_dicts:
            md.setdefault("refs", [])
        return

    # Cache per-sender lookups so a chatty meeting doesn't hit the hub
    # once per message.
    agent_cache: dict = {}

    def _agent_for(sender_id: str):
        if not sender_id or sender_id == "user":
            return None
        if sender_id in agent_cache:
            return agent_cache[sender_id]
        try:
            ag = hub.get_agent(sender_id) if hub else None
        except Exception:
            ag = None
        agent_cache[sender_id] = ag
        return ag

    for md in msg_dicts:
        md.setdefault("refs", [])
        try:
            content = md.get("content") or ""
            if not content:
                continue
            sender = md.get("sender") or ""
            agent = _agent_for(sender)
            if agent is None:
                # No sender agent — still try to surface URL refs (no
                # local-path resolution since we have no base_dir).
                try:
                    md["refs"] = build_refs_from_text(
                        content, "", url_for_path=lambda _p, _aid: "",
                    )
                except Exception:
                    md["refs"] = []
                continue

            # Use the agent's effective working directory as base_dir.
            try:
                base_dir = str(agent._effective_working_dir())  # noqa: SLF001
            except Exception:
                base_dir = getattr(agent, "working_dir", "") or ""

            # Ingest into the agent's shadow store so the html_tag_router
            # click resolves. install_into_agent is idempotent and respects
            # the TUDOU_AGENT_STATE_SHADOW=0 opt-out.
            try:
                shadow = getattr(agent, "_shadow", None)
                if shadow is None:
                    shadow = install_into_agent(agent)
                if shadow is not None and base_dir:
                    cands = extract_from_text(content)
                    if cands:
                        normalize_path_candidates(cands, base_dir)
                        ingest_into_store(
                            shadow.state.artifacts, cands,
                            produced_by=ProducedBy(
                                tool_id="meeting_message",
                                agent_id=getattr(agent, "id", "") or "",
                            ),
                            return_existing=True,
                        )
            except Exception as _e:
                logger.debug("meeting enrich: ingest failed: %s", _e)

            def _url(_p, _aid, _aid_for=getattr(agent, "id", "") or ""):
                return build_artifact_url(_aid_for, _aid)

            try:
                md["refs"] = build_refs_from_text(
                    content, base_dir, url_for_path=_url,
                    require_inside_base=False,
                )
            except Exception as _e:
                logger.debug("meeting enrich: build refs failed: %s", _e)
                md["refs"] = []
        except Exception as _e:
            logger.debug("meeting enrich: per-message failed: %s", _e)
            md.setdefault("refs", [])


def _do_get_inner(handler, path: str):
    """Main GET handler logic extracted from _PortalHandler._do_GET_inner"""
    hub = get_hub()
    if path.startswith("/api/"):
        logger.debug("GET %s from %s", path, get_client_ip(handler))

    # html_tag_router: artifact streaming for inline <video>/<img>/<audio>.
    # Dispatched BEFORE auth so phase-1 grey rollout can be exercised
    # without portal session cookies. Auth will be added in phase 2.
    try:
        from . import html_tag_router
        if html_tag_router.matches(path):
            html_tag_router.handle(handler, path)
            return
    except Exception as _e:
        logger.debug("html_tag_router dispatch failed: %s", _e)

    # Check auth for non-public endpoints
    # Static assets and public pages are exempt from auth so the login page can load its JS/CSS/images
    _public = path in ("/", "/index.html", "/api/health") or path.startswith("/static/")
    if not _public and not require_auth(handler):
        return

    if path in ("/", "/index.html"):
        # Node mode: no login — go straight to portal
        if not is_hub_mode():
            handler._html(_PORTAL_HTML)
            return
        session_id = get_session_cookie(handler)
        auth = get_auth()
        if not auth.validate_session(session_id):
            handler._html(_LOGIN_HTML)
        else:
            handler._html(_PORTAL_HTML)

    elif path == "/api/health":
        handler._json({"status": "ok", "summary": hub.summary()})

    elif path == "/api/portal/agents":
        # Return agent list with node_id so UI can filter per-node.
        # Sub-agents (those with parent_id) are hidden unless explicitly requested.
        # Filter by admin permissions if applicable.
        qs = parse_qs(urlparse(handler.path).query)
        include_sub = qs.get("include_subagents", ["0"])[0] in ("1", "true", "yes")

        # Get admin context to filter agents if needed
        admin_user_id = get_admin_context(handler)
        agents_list = get_visible_agents(handler, hub, admin_user_id)

        if not include_sub:
            agents_list = [a for a in agents_list if not a.get("parent_id")]
        for a in agents_list:
            if a.get("location") == "local" and not a.get("node_id"):
                a["node_id"] = hub.node_id
        handler._json({"agents": agents_list})

    elif path == "/api/portal/nodes":
        # Return list of nodes with basic info (id, name, status, agent counts, capabilities)
        try:
            from .infra.node_manager import get_node_manager
            nm = get_node_manager()
            nodes_list = [n.__dict__ if hasattr(n, '__dict__') else n for n in (nm.list_nodes() if nm else [])]
        except Exception:
            nodes_list = []

        # Include "local" node as a virtual entry
        # 加上各节点的项目数 / 任务数统计
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

        local_node = {
            "node_id": "local",
            "name": "本机 (Local)",
            "status": "online",
            "agent_count": len([a for a in (hub.agents if hub else {}).values() if (getattr(a, 'node_id', 'local') or 'local') == 'local']),
            "capabilities": {},
        }
        local_node.update(_count_for_node("local"))
        for n in nodes_list:
            nid = n.get("node_id", "")
            if nid:
                n.update(_count_for_node(nid))
        handler._json({"nodes": [local_node] + nodes_list})

    elif path == "/api/portal/state":
        # Hide sub-agents from the main dashboard/sidebar state too
        top_agents = [a for a in hub.list_agents() if not a.get("parent_id")]
        # Build agent ID → name map for message display
        _agent_name_map = {}
        for _a in hub.agents.values():
            _agent_name_map[_a.id] = f"{_a.role}-{_a.name}" if _a.role else _a.name
        def _enrich_msg(m):
            d = m.to_dict()
            d["from_agent_name"] = _agent_name_map.get(m.from_agent, m.from_agent)
            d["to_agent_name"] = _agent_name_map.get(m.to_agent, m.to_agent)
            return d
        handler._json({
            "agents": top_agents,
            "nodes": hub.list_nodes(),
            "messages": [_enrich_msg(m) for m in hub.messages[-100:]],
            "approvals": get_approvals(),
            "summary": hub.summary(),
            "portal_mode": get_portal_mode(),
        })

    elif path == "/api/portal/config":
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
        for key in ("openai_api_key", "claude_api_key", "unsloth_api_key"):
            if cfg.get(key):
                cfg[key] = "********"
        # Include providers from dynamic registry
        reg = get_registry()
        cfg["providers"] = [p.to_dict(mask_key=True) for p in reg.list(include_disabled=True)]
        cfg["available_models"] = reg.get_all_models()
        handler._json(cfg)

    elif path == "/api/portal/policy":
        auth = get_auth()
        handler._json(auth.tool_policy.get_policy_config())

    elif path == "/api/portal/pending-reviews":
        # Aggregate every awaiting_review step across all projects.
        # Cheap to compute (in-memory walk) and used by the dashboard badge
        # + the Pending Review section. Returns oldest-first.
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
        handler._json({"count": len(items), "items": items})

    elif path == "/api/portal/providers":
        reg = get_registry()
        handler._json({"providers": [p.to_dict(mask_key=True) for p in reg.list(include_disabled=True)]})

    elif path.startswith("/api/portal/providers/") and path.endswith("/models"):
        provider_id = path.split("/")[4]
        reg = get_registry()
        models = reg.detect_models(provider_id)
        handler._json({"provider_id": provider_id, "models": models})

    elif path.startswith("/api/portal/agent/") and path.endswith("/events"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent:
            handler._json({"events": [e.to_dict() for e in agent.events[-500:]]})
        else:
            # Try proxy to remote node
            data = hub.proxy_remote_agent_get(agent_id, "/events")
            if data:
                handler._json(data)
            else:
                handler._json({"events": []})  # Return empty instead of 404 for remote

    elif path == "/api/portal/audit":
        auth = get_auth()
        entries = auth.get_audit_log(limit=500)
        query = parse_qs(urlparse(handler.path).query)
        action_filter = query.get("action", [""])[0]
        if action_filter:
            entries = [e for e in entries if e.get("action") == action_filter]
        handler._json({"entries": entries})

    elif path == "/api/auth/tokens":
        auth = get_auth()
        tokens_list = auth.list_tokens()
        # Enrich with admin display name
        for t in tokens_list:
            aid = t.get("admin_user_id", "")
            if aid:
                adm = auth.admin_mgr.get_admin(aid)
                t["admin_display_name"] = adm.display_name if adm else aid
            else:
                t["admin_display_name"] = ""
        handler._json({"tokens": tokens_list})

    elif path == "/api/portal/admin/me":
        # Get current admin user info + manageable agents.
        # Works for both admin-login sessions AND token-based sessions
        # (tokens now carry admin_user_id).
        auth = get_auth()
        admin_user_id = get_admin_context(handler)
        if admin_user_id:
            admin = auth.admin_mgr.get_admin(admin_user_id)
            if admin:
                manageable_agents = get_visible_agents(handler, hub, admin_user_id)
                handler._json({
                    "admin": admin.to_dict(include_secrets=False),
                    "manageable_agents": manageable_agents,
                })
                return

        # Fallback for legacy tokens with role=admin but no admin_user_id:
        # synthesize a virtual superAdmin response
        actor_name, user_role = get_auth_info(handler)
        if user_role == "admin":
            handler._json({
                "admin": {
                    "user_id": "",
                    "username": actor_name,
                    "role": "superAdmin",
                    "display_name": actor_name,
                    "agent_ids": [],
                    "active": True,
                },
                "manageable_agents": hub.list_agents(),
            })
            return

        # Regular operator/viewer token — no admin binding
        handler._json({"admin": None, "manageable_agents": []})

    elif path == "/api/hub/agents":
        # Inter-node sync: return only LOCAL agents to prevent
        # circular duplicates when nodes mutually register each other.
        local_only = []
        for a in hub.agents.values():
            d = a.to_dict()
            d["location"] = "local"
            local_only.append(d)
        handler._json({"agents": local_only})

    elif path == "/api/portal/personas":
        from ..persona import list_personas as _list_personas
        handler._json({"personas": _list_personas()})

    elif path.startswith("/api/portal/personas/"):
        persona_id = path.split("/")[-1]
        from ..persona import get_persona as _get_persona
        p = _get_persona(persona_id)
        if p:
            handler._json(p.to_dict())
        else:
            handler._json({"error": "Persona not found"}, 404)

    elif path == "/api/portal/workflows":
        handler._json({"workflows": hub.list_workflows()})

    elif path == "/api/portal/workflow-templates":
        from ..workflow import list_workflow_templates as _lwt
        handler._json({"templates": _lwt()})

    elif path == "/api/portal/workflow-catalog":
        try:
            from .data.workflow_catalog import list_catalog_templates, get_catalog_categories
            handler._json({
                "catalog": list_catalog_templates(),
                "categories": get_catalog_categories(),
            })
        except ImportError:
            try:
                from app.data.workflow_catalog import list_catalog_templates, get_catalog_categories
                handler._json({
                    "catalog": list_catalog_templates(),
                    "categories": get_catalog_categories(),
                })
            except ImportError:
                handler._json({"catalog": [], "categories": {}})

    elif path.startswith("/api/portal/workflows/"):
        wf_id = path.split("/")[-1]
        wf = hub.get_workflow(wf_id)
        if wf:
            handler._json(wf.to_dict())
        else:
            handler._json({"error": "Workflow not found"}, 404)

    elif path == "/api/portal/projects":
        handler._json({"projects": hub.list_projects()})

    elif path.startswith("/api/portal/projects/") and path.endswith("/chat"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            limit = int(parse_qs(urlparse(handler.path).query).get("limit", ["50"])[0])
            msgs = proj.get_chat_history(limit=limit)
            msg_dicts = [m.to_dict() for m in msgs]
            # Enrich each message with FileCard refs extracted from its
            # content. base_dir is the project's working_directory; the
            # URL is routed through the project_artifact streaming route
            # so the click resolves regardless of which agent produced
            # the file. Path-extraction matches the agent-chat path
            # exactly, so users see the same dedup / icon / size UX.
            try:
                base_dir = (getattr(proj, "working_directory", "") or "").strip()
                if base_dir:
                    from ..agent_state.file_refs import build_refs_from_text
                    from .html_tag_router import build_project_artifact_url
                    def _url(_p, _aid, _pid=proj.id):
                        return build_project_artifact_url(_pid, _aid)
                    for md in msg_dicts:
                        try:
                            md["refs"] = build_refs_from_text(
                                md.get("content", "") or "",
                                base_dir,
                                url_for_path=_url,
                            )
                        except Exception as _e:
                            logger.debug("project chat ref enrich failed: %s", _e)
                            md["refs"] = []
                else:
                    for md in msg_dicts:
                        md["refs"] = []
            except Exception as _e:
                logger.debug("project chat enrich block failed: %s", _e)
                for md in msg_dicts:
                    md.setdefault("refs", [])
            handler._json({"messages": msg_dicts})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/tasks"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            handler._json({"tasks": [t.to_dict() for t in proj.tasks]})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/milestones"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            handler._json({"milestones": [m.to_dict() for m in proj.milestones]})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/goals"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            handler._json({"goals": [g.to_dict() for g in proj.goals]})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/deliverables"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            qs = parse_qs(urlparse(handler.path).query)
            status_f = (qs.get("status", [""])[0] or "").strip()
            items = proj.deliverables
            if status_f:
                items = [dv for dv in items
                         if (dv.status.value if hasattr(dv.status, "value") else str(dv.status)) == status_f]
            handler._json({"deliverables": [dv.to_dict() for dv in items]})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/deliverables-by-agent"):
        # Project deliverables view.
        # Single source of truth: the project's shared workspace at
        # ~/.tudou_claw/workspaces/shared/<project_id>/. We do NOT scan each
        # agent's private workspace (that surfaces code / skill.md / MCP.md /
        # logs — noise, not deliverables). Explicit Deliverable rows are
        # filtered to those referencing a path inside the shared dir (or
        # content-only rows that get materialized there by submit_deliverable).
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
        else:
            import os as _os
            try:
                from ..agent_state.extractors import scan_deliverable_dir
                from ..agent_state.artifact import ArtifactStore
            except Exception as _e:
                logger.debug("deliverables-by-agent: imports failed: %s", _e)
                scan_deliverable_dir = None  # type: ignore
                ArtifactStore = None         # type: ignore

            # Resolve the canonical project shared dir
            from ..agent import Agent as _Agent
            try:
                shared_dir = _Agent.get_shared_workspace_path(proj_id)
            except Exception:
                shared_dir = ""
            shared_base_real = ""
            if shared_dir:
                try:
                    shared_base_real = _os.path.realpath(shared_dir)
                except Exception:
                    shared_base_real = ""

            def _under_shared(p: str) -> bool:
                if not p or not shared_base_real:
                    return False
                try:
                    if _os.path.isabs(p):
                        real = _os.path.realpath(p)
                    else:
                        real = _os.path.realpath(_os.path.join(shared_dir, p))
                except Exception:
                    return False
                return (real == shared_base_real
                        or real.startswith(shared_base_real + _os.sep))

            # Filter explicit deliverables:
            #  - drop legacy "(auto-registered from chat reply)" sentinel rows
            #  - drop rows with file_path outside the shared dir
            #  - keep content-only rows unconditionally
            AUTO_SENTINEL = "(auto-registered from chat reply)"
            explicit_by_author: dict = {}
            unassigned_explicit = []
            for dv in proj.deliverables:
                if (getattr(dv, "content_text", "") or "").strip() == AUTO_SENTINEL:
                    continue
                fp = (getattr(dv, "file_path", "") or "").strip()
                if fp and not _under_shared(fp):
                    continue
                aid = (dv.author_agent_id or "").strip()
                if aid:
                    explicit_by_author.setdefault(aid, []).append(dv.to_dict())
                else:
                    unassigned_explicit.append(dv.to_dict())

            agents_out = []
            seen_agent_ids = set()
            for m in proj.members:
                aid = (m.agent_id or "").strip()
                if not aid or aid in seen_agent_ids:
                    continue
                seen_agent_ids.add(aid)
                agent = hub.get_agent(aid)
                agent_name = getattr(agent, "name", aid) if agent else aid
                role = getattr(agent, "role", "") if agent else ""
                explicit_list = explicit_by_author.get(aid, [])
                agents_out.append({
                    "agent_id": aid,
                    "agent_name": agent_name,
                    "role": role,
                    "responsibility": getattr(m, "responsibility", "") or "",
                    "deliverable_dir": shared_dir,   # canonical shared dir
                    "explicit_deliverables": explicit_list,
                    "files": [],                     # per-agent scan removed
                    "file_count": 0,
                    "explicit_count": len(explicit_list),
                })

            # Unassigned: explicit rows whose author isn't a current member
            for aid, items in explicit_by_author.items():
                if aid not in seen_agent_ids:
                    unassigned_explicit.extend(items)

            # Depth-1 listing of the shared dir (files + folders at top level).
            # Subdirectories are shown as folder markers, not recursed into —
            # nested code / build artifacts are NOT surfaced as deliverables.
            shared_files = []
            import mimetypes as _mt
            if shared_dir and _os.path.isdir(shared_dir):
                try:
                    for name in _os.listdir(shared_dir):
                        if name.startswith("."):
                            continue
                        full = _os.path.join(shared_dir, name)
                        try:
                            st = _os.stat(full)
                        except OSError:
                            continue
                        is_dir = _os.path.isdir(full)
                        mime = ""
                        if not is_dir:
                            mime = (_mt.guess_type(full)[0] or "")
                        shared_files.append({
                            "id": name,
                            "name": name,
                            "path": full,
                            "rel_path": name,
                            "kind": "directory" if is_dir else "file",
                            "mime": mime or ("inode/directory" if is_dir else ""),
                            "size": None if is_dir else st.st_size,
                            "mtime": st.st_mtime,
                            "url": "" if is_dir else f"/workspace/shared/{proj_id}/{name}",
                            "is_remote": False,
                            "is_dir": is_dir,
                        })
                    shared_files.sort(key=lambda f: (
                        0 if f.get("is_dir") else 1,
                        f.get("name", "").lower() if f.get("is_dir")
                            else -(f.get("mtime") or 0),
                    ))
                except Exception as _e:
                    logger.debug("shared files listing failed: %s", _e)

            handler._json({
                "agents": agents_out,
                "unassigned_deliverables": unassigned_explicit,
                "shared_dir": shared_dir,
                "shared_files": shared_files,
                "shared_file_count": len(shared_files),
            })

    elif path.startswith("/api/portal/projects/") and path.endswith("/issues"):
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            qs = parse_qs(urlparse(handler.path).query)
            status_f = (qs.get("status", [""])[0] or "").strip()
            items = proj.issues
            if status_f:
                items = [i for i in items if i.status == status_f]
            handler._json({"issues": [i.to_dict() for i in items]})
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and path.endswith("/overview"):
        # Aggregated view: goals + milestones + deliverables + issues for
        # the redesigned project detail page. One round-trip, one render.
        proj_id = path.split("/")[4]
        proj = hub.get_project(proj_id)
        if proj:
            d = proj.to_dict()
            handler._json({
                "project": {
                    "id": d["id"], "name": d["name"],
                    "description": d["description"], "status": d["status"],
                    "members": d["members"], "working_directory": d["working_directory"],
                    "node_id": d["node_id"], "created_at": d["created_at"],
                    "updated_at": d["updated_at"],
                },
                "goals": d.get("goals", []),
                "goal_summary": d.get("goal_summary", {}),
                "milestones": d.get("milestones", []),
                "deliverables": d.get("deliverables", []),
                "deliverable_summary": d.get("deliverable_summary", {}),
                "issues": d.get("issues", []),
                "issue_summary": d.get("issue_summary", {}),
                "task_summary": d.get("task_summary", {}),
            })
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path.startswith("/api/portal/projects/") and not path.endswith("/chat") and not path.endswith("/tasks") and not path.endswith("/milestones") and not path.endswith("/goals") and not path.endswith("/deliverables") and not path.endswith("/issues") and not path.endswith("/overview"):
        proj_id = path.split("/")[-1]
        proj = hub.get_project(proj_id)
        if proj:
            handler._json(proj.to_dict())
        else:
            handler._json({"error": "Project not found"}, 404)

    elif path == "/api/portal/conversation-tasks/resumable":
        # Global GET /api/portal/conversation-tasks/resumable
        # Returns paused tasks across all agents — the login banner
        # uses this to prompt the admin to continue unfinished work.
        try:
            from ..conversation_task import get_store as _get_ct_store
            ct_store = _get_ct_store()
            tasks = ct_store.list_resumable("")
            handler._json({
                "tasks": [_ct_compact(t) for t in tasks],
                "count": len(tasks),
            })
        except Exception as e:
            logger.warning("resumable list failed: %s", e)
            handler._json({"tasks": [], "count": 0})

    elif path.startswith("/api/portal/agent/") and path.endswith("/conversation-tasks"):
        # Conversation-task queue: complex user messages that got
        # promoted to a tracked task. Returns newest first. Query
        # params:
        #   active=1     — only non-terminal rows (default shows all)
        #   limit=N      — cap (default 50)
        agent_id = path.split("/")[4]
        qs = parse_qs(urlparse(handler.path).query)
        active_only = qs.get("active", ["0"])[0] in ("1", "true", "yes")
        try:
            limit = max(1, min(200, int(qs.get("limit", ["50"])[0])))
        except (ValueError, TypeError):
            limit = 50
        try:
            from ..conversation_task import get_store as _get_ct_store
            ct_store = _get_ct_store()
            tasks = ct_store.list_for_agent(
                agent_id, include_terminal=not active_only, limit=limit)
            handler._json({
                "tasks": [_ct_compact(t) for t in tasks],
                "active_only": active_only,
            })
        except Exception as e:
            logger.warning("conversation-tasks list failed: %s", e)
            handler._json({"tasks": []})

    elif path.startswith("/api/portal/agent/") and path.endswith("/tasks"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent:
            status_filter = parse_qs(urlparse(handler.path).query).get("status", [""])[0]
            tasks = agent.list_tasks(status=status_filter)
            handler._json({"tasks": [t.to_dict() for t in tasks]})
        else:
            # Try proxy to remote node
            qs = urlparse(handler.path).query
            sub = "/tasks" + (f"?{qs}" if qs else "")
            data = hub.proxy_remote_agent_get(agent_id, sub)
            if data:
                handler._json(data)
            else:
                handler._json({"tasks": []})

    elif path.startswith("/api/portal/agent/") and path.endswith("/runtime-stats"):
        # GET /api/portal/agent/{agent_id}/runtime-stats
        # 返回 token 用量 + 记忆占比，供 portal agent 详情页展示。
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent:
            handler._json({
                "tokens": agent.get_token_stats(),
                "memory": agent.get_memory_usage_stats(),
            })
        else:
            handler._json({"error": "Agent not found"}, 404)

    elif path.startswith("/api/portal/agent/") and path.endswith("/pending-tasks"):
        # GET /api/portal/agent/{agent_id}/pending-tasks
        # Cross-project view: every project task assigned to this agent that
        # is still todo / in_progress.
        agent_id = path.split("/")[4]
        items = hub.list_agent_pending_tasks(agent_id)
        handler._json({"pending": items, "count": len(items)})

    # --- Persistent file list (deliverable_dir) ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/files"):
        # GET /api/portal/agent/{agent_id}/files
        # Refreshes the artifact store from deliverable_dir then returns
        # every file-kind artifact as a FileCard-ready dict. Persistent
        # alternative to the SSE artifact_refs envelope: works after
        # page reload, picks up files dropped by side-channels, and
        # backfills artifacts from previous portal runs.
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if not agent:
            handler._json({"error": "Agent not found"}, 404)
        else:
            # Shadow is lazily attached on first user message in chat();
            # opening the file list before sending anything would otherwise
            # return empty. Try to attach now — install_into_agent is
            # idempotent and respects the TUDOU_AGENT_STATE_SHADOW=0 opt-out.
            shadow = getattr(agent, "_shadow", None)
            if shadow is None:
                try:
                    from ..agent_state.shadow import install_into_agent
                    shadow = install_into_agent(agent)
                except Exception:
                    shadow = None
            if shadow is None:
                # shadow opted out via env flag — no persistent file list
                handler._json({
                    "files": [], "count": 0, "shadow": False,
                    "turns": [], "orphans": [],
                })
            else:
                try:
                    rescanned = shadow.rescan_deliverable_dir()
                except Exception:
                    rescanned = 0
                # Deterministic file → assistant-turn-index mapping
                # built by walking events (no timestamps).
                try:
                    idx = shadow.compute_file_index_from_events()
                except Exception:
                    idx = {"turns": [], "orphans": [], "total_assistant_turns": 0}
                files = shadow.list_all_file_refs()
                handler._json({
                    "files": files,
                    "count": len(files),
                    "rescanned": rescanned,
                    "shadow": True,
                    "turns": idx.get("turns", []),
                    "orphans": idx.get("orphans", []),
                    "total_assistant_turns": idx.get("total_assistant_turns", 0),
                })

    # --- Execution Analyzer: recent analyses ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/analyses"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent and agent._execution_analyzer:
            analyses = agent._execution_analyzer.get_recent_analyses(20)
            handler._json({"analyses": [a.to_dict() for a in analyses]})
        else:
            handler._json({"analyses": []})

    # --- Skill System: agent skills ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/prompt-packs"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent:
            from ..core.prompt_enhancer import get_prompt_pack_registry
            registry = get_prompt_pack_registry()
            bound = []
            for sid in agent.bound_prompt_packs:
                rec = registry.store.get(sid)
                if rec:
                    bound.append(rec.to_dict())
            handler._json({
                "bound_skills": bound,
                "bound_prompt_packs": agent.bound_prompt_packs,
                "registry_stats": registry.store.get_stats(),
            })
        else:
            handler._json({"error": "Agent not found"}, 404)

    elif path == "/api/portal/prompt-packs":
        from ..core.prompt_enhancer import get_prompt_pack_registry
        registry = get_prompt_pack_registry()
        skills = [s.to_dict() for s in registry.store.get_active()]
        handler._json({
            "skills": skills,
            "stats": registry.store.get_stats(),
        })

    elif path == "/api/portal/skill-store":
        # Hub-level Skill Store catalog (browse / filter)
        store = getattr(hub, "skill_store", None)
        if store is None:
            handler._json({"error": "skill store not initialized"}, 503)
            return
        qs = parse_qs(urlparse(handler.path).query)
        source_filter = (qs.get("source", [""])[0] or "").strip()
        tag = (qs.get("tag", [""])[0] or "").strip()
        query = (qs.get("q", [""])[0] or "").strip()
        include_all = qs.get("all", ["0"])[0] in ("1", "true", "yes")
        entries = store.list_catalog(
            source_filter=source_filter, tag=tag, query=query,
            include_disallowed=include_all,
        )
        # Pull installed-skill metadata (granted_to etc.) for UI
        installed_map: dict[str, dict] = {}
        reg = getattr(hub, "skill_registry", None)
        if reg is not None:
            try:
                for inst in reg.list_all():
                    installed_map[inst.id] = {
                        "id": inst.id,
                        "name": inst.manifest.name,
                        "status": inst.status,
                        "granted_to": list(inst.granted_to),
                        "runtime": inst.manifest.runtime,
                    }
            except Exception:
                pass
        handler._json({
            "entries": [e.to_dict() for e in entries],
            "installed": installed_map,
            "annotations": store.list_annotations(),
            "stats": store.stats(),
        })

    # --- Meetings ---
    elif path == "/api/portal/meetings":
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            handler._json({"meetings": []})
        else:
            qs = parse_qs(urlparse(handler.path).query)
            proj_id = qs.get("project_id", [None])[0]
            status_f = (qs.get("status", [""])[0] or "") or None
            participant = (qs.get("participant", [""])[0] or "") or None
            items = reg.list(project_id=proj_id, status=status_f, participant=participant)
            handler._json({"meetings": [m.to_summary_dict() for m in items]})

    elif path.startswith("/api/portal/meetings/"):
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            handler._json({"error": "meeting registry not initialized"}, 503)
            return
        parts = path.split("/")
        # /api/portal/meetings/{id} or /api/portal/meetings/{id}/messages|assignments
        mid = parts[4] if len(parts) >= 5 else ""
        m = reg.get(mid)
        if not m:
            handler._json({"error": "Meeting not found"}, 404)
            return
        tail = parts[5] if len(parts) >= 6 else ""
        if tail == "messages":
            msg_dicts = [x.to_dict() for x in m.messages]
            _enrich_meeting_messages_with_refs(hub, msg_dicts)
            handler._json({"messages": msg_dicts})
        elif tail == "assignments":
            handler._json({"assignments": [a.to_dict() for a in m.assignments]})
        else:
            full = m.to_dict()
            try:
                _enrich_meeting_messages_with_refs(hub, full.get("messages") or [])
            except Exception as _e:
                logger.debug("meeting detail enrich failed: %s", _e)
            handler._json(full)

    # --- Standalone (non-project) tasks ---
    elif path == "/api/portal/standalone-tasks":
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is None:
            handler._json({"tasks": []})
        else:
            qs = parse_qs(urlparse(handler.path).query)
            assignee = (qs.get("assignee", [""])[0] or "") or None
            status_f = (qs.get("status", [""])[0] or "") or None
            items = reg.list(assignee=assignee, status=status_f)
            handler._json({"tasks": [t.to_dict() for t in items]})

    # --- Unified agent task view: project tasks + standalone ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/all-tasks"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        proj_tasks = []
        if agent:
            try:
                proj_tasks = hub.list_agent_pending_tasks(agent_id)
            except Exception:
                proj_tasks = []
        standalone = []
        reg = getattr(hub, "standalone_task_registry", None)
        if reg is not None:
            standalone = [t.to_dict() for t in reg.list(assignee=agent_id)]
        handler._json({
            "project_tasks": proj_tasks,
            "standalone_tasks": standalone,
            "counts": {
                "project": len(proj_tasks),
                "standalone": len(standalone),
                "total": len(proj_tasks) + len(standalone),
            },
        })

    # --- Role Growth Path ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/growth"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if agent:
            gp = agent.ensure_growth_path()
            if gp:
                handler._json({
                    "growth_path": gp.to_dict(),
                    "summary": gp.get_summary(),
                })
            else:
                handler._json({"growth_path": None, "summary": None,
                            "message": f"No growth path template for role '{agent.role}'"})
        else:
            handler._json({"error": "Agent not found"}, 404)

    elif path == "/api/portal/growth-paths":
        from ..core.role_growth_path import ROLE_GROWTH_PATHS
        paths = {}
        for role, gp in ROLE_GROWTH_PATHS.items():
            paths[role] = gp.get_summary()
        handler._json({"paths": paths, "total_roles": len(paths)})

    # --- src integration: cost / history / session / tools ---
    elif path.startswith("/api/portal/agent/") and path.endswith("/cost"):
        agent_id = path.split("/")[4]
        cost = hub.get_agent_cost(agent_id)
        if cost:
            handler._json(cost)
        else:
            handler._json({"error": "Agent not found"}, 404)

    elif path.startswith("/api/portal/agent/") and path.endswith("/history"):
        agent_id = path.split("/")[4]
        md = hub.get_agent_history(agent_id)
        handler._json({"markdown": md})

    elif path == "/api/portal/costs":
        handler._json(hub.get_all_costs())

    elif path == "/api/portal/tool-surface":
        query = parse_qs(urlparse(handler.path).query).get("q", [""])[0]
        handler._json({"index": hub.get_tool_surface(query)})

    elif path == "/api/portal/system-info":
        handler._json(hub.get_system_info())

    elif path == "/api/portal/parity-report":
        handler._json(hub.get_parity_report())

    elif path == "/api/portal/workspace-summary":
        handler._json({"summary": hub.get_workspace_summary()})

    elif path == "/api/portal/smart-route":
        query = parse_qs(urlparse(handler.path).query).get("q", [""])[0]
        if query:
            result = hub.route_and_dispatch(query)
            handler._json(result)
        else:
            handler._json({"error": "Missing q parameter"}, 400)

    # --- Config deployment status endpoints ---
    elif path == "/api/portal/config-deployments":
        deploy_id = parse_qs(urlparse(handler.path).query).get("id", [""])[0]
        if deploy_id:
            handler._json(hub.get_deployment_status(deploy_id))
        else:
            handler._json({"deployments": hub.get_deployment_status()})

    elif re.match(r"^/api/portal/node/([^/]+)/config-status$", path):
        nid = re.match(r"^/api/portal/node/([^/]+)/config-status$", path).group(1)
        handler._json(hub.get_node_config_status(nid))

    # --- Node-scoped configuration endpoints ---
    elif path == "/api/portal/node-configs":
        # List all node configs (admin overview)
        mask = parse_qs(urlparse(handler.path).query).get("mask", ["1"])[0] != "0"
        handler._json({"configs": hub.list_all_node_configs(mask=mask)})

    elif re.match(r"^/api/portal/node/([^/]+)/config$", path):
        # Get config for a specific node
        nid = re.match(r"^/api/portal/node/([^/]+)/config$", path).group(1)
        mask = parse_qs(urlparse(handler.path).query).get("mask", ["1"])[0] != "0"
        # Permission: admin can view any, node can only view own
        if not is_hub_mode() and nid != "local":
            handler._json({"error": "Node mode: can only view own config"}, 403)
        else:
            handler._json(hub.get_node_config(nid, mask=mask))

    # --- src memory engine API endpoints ---
    elif re.match(r"^/api/portal/agent/([^/]+)/engine$", path):
        aid = re.match(r"^/api/portal/agent/([^/]+)/engine$", path).group(1)
        handler._json(hub.get_agent_engine_info(aid))

    elif re.match(r"^/api/portal/agent/([^/]+)/transcript$", path):
        aid = re.match(r"^/api/portal/agent/([^/]+)/transcript$", path).group(1)
        handler._json({"transcript": hub.get_agent_transcript(aid)})

    elif re.match(r"^/api/portal/agent/([^/]+)/route$", path):
        m = re.match(r"^/api/portal/agent/([^/]+)/route$", path)
        aid = m.group(1)
        query = parse_qs(urlparse(handler.path).query).get("q", [""])[0]
        handler._json(hub.route_agent_prompt(aid, query))

    # --- Enhancement module endpoints ---
    elif path == "/api/portal/enhancement-presets":
        handler._json({"presets": list_enhancement_presets()})

    elif re.match(r"^/api/portal/agent/([^/]+)/enhancement$", path):
        aid = re.match(r"^/api/portal/agent/([^/]+)/enhancement$", path).group(1)
        agent = hub.get_agent(aid)
        if not agent:
            handler._json({"error": "Agent not found"}, 404)
        else:
            info = agent.get_enhancement_info()
            if info:
                # Add list versions for UI rendering, then replace
                # entry fields with counts (stats cards expect numbers)
                kl = info.get("knowledge_entries", []) or []
                rl = info.get("reasoning_patterns", []) or []
                ml = info.get("memory_nodes", []) or []
                tl = info.get("tool_chains", []) or []
                info["knowledge_list"] = kl
                info["reasoning_list"] = rl
                info["memory_list"] = ml
                info["tool_chain_list"] = tl
                info["knowledge_entries"] = len(kl) if isinstance(kl, list) else kl
                info["reasoning_patterns"] = len(rl) if isinstance(rl, list) else rl
                info["memory_nodes"] = len(ml) if isinstance(ml, list) else ml
                info["tool_chains"] = len(tl) if isinstance(tl, list) else tl
            handler._json({"enhancement": info, "presets": list_enhancement_presets()})

    elif re.match(r"^/api/portal/agent/([^/]+)/plans$", path):
        # Get execution plans for an agent
        aid = re.match(r"^/api/portal/agent/([^/]+)/plans$", path).group(1)
        agent = hub.get_agent(aid)
        if not agent:
            handler._json({"error": "Agent not found"}, 404)
        else:
            current = agent.get_current_plan()
            plans = agent.get_execution_plans(limit=10)
            handler._json({"current_plan": current, "plans": plans})

    # --- Scheduler API ---
    elif path == "/api/portal/scheduler/jobs":
        scheduler = get_scheduler()
        params = parse_qs(urlparse(handler.path).query)
        agent_id = params.get("agent_id", [""])[0]
        jobs = scheduler.list_jobs(agent_id=agent_id)
        handler._json({"jobs": [j.to_dict() for j in jobs]})

    elif re.match(r"^/api/portal/scheduler/jobs/([^/]+)$", path):
        jid = re.match(r"^/api/portal/scheduler/jobs/([^/]+)$", path).group(1)
        scheduler = get_scheduler()
        job = scheduler.get_job(jid)
        if job:
            handler._json(job.to_dict())
        else:
            handler._json({"error": "Job not found"}, 404)

    elif re.match(r"^/api/portal/scheduler/jobs/([^/]+)/history$", path):
        jid = re.match(r"^/api/portal/scheduler/jobs/([^/]+)/history$", path).group(1)
        scheduler = get_scheduler()
        history = scheduler.get_execution_history(jid, limit=30)
        handler._json({"history": [r.to_dict() for r in history]})

    elif path == "/api/portal/scheduler/presets":
        handler._json({"presets": {k: v for k, v in PRESET_JOBS.items()}})

    # --- MCP Manager API ---
    elif path == "/api/portal/mcp/catalog":
        handler._json({"catalog": {k: v.to_dict() for k, v in MCP_CATALOG.items()}})

    elif path == "/api/portal/mcp/source/list":
        # Admin-only: enumerate builtin MCP server source files that are
        # editable from the Portal. Accepts both new layout
        # (``python -m app.mcp.builtins.jimeng_video``) and the legacy
        # layout (``python -m app.tudou_jimeng_video_mcp``). The module is
        # resolved to a real file under the project root; any resolved
        # path must stay under ``app/`` to pass the traversal check.
        if not is_super_admin(handler):
            handler._json({"error": "admin only"}, 403)
            return
        app_dir, proj_root, _resolve_mcp_source = _mcp_source_resolver()
        items: list[dict] = []
        for mcp_id, cap in MCP_CATALOG.items():
            ct = getattr(cap, "command_template", "") or ""
            info = _resolve_mcp_source(ct)
            if info is None:
                continue
            dotted, fpath = info
            if not os.path.isfile(fpath):
                continue
            try:
                st = os.stat(fpath)
                size = st.st_size
                mtime = st.st_mtime
            except OSError:
                size, mtime = 0, 0
            rel = os.path.relpath(fpath, proj_root)
            items.append({
                "mcp_id": mcp_id,
                "name": cap.name,
                "module": dotted,
                "rel_path": rel,
                "size": size,
                "mtime": mtime,
            })
        items.sort(key=lambda x: x["mcp_id"])
        handler._json({"items": items})

    elif path.startswith("/api/portal/mcp/source/"):
        # GET /api/portal/mcp/source/{mcp_id} → read file text
        if not is_super_admin(handler):
            handler._json({"error": "admin only"}, 403)
            return
        mcp_id = path.split("/")[-1]
        cap = MCP_CATALOG.get(mcp_id)
        if cap is None:
            handler._json({"error": "unknown mcp_id"}, 404)
            return
        app_dir, _proj_root, _resolve_mcp_source = _mcp_source_resolver()
        info = _resolve_mcp_source(getattr(cap, "command_template", "") or "")
        if info is None:
            handler._json({"error": "mcp has no editable python source"}, 400)
            return
        _dotted, fpath = info
        fpath = os.path.normpath(fpath)
        # Path-traversal defense: the resolved file must still live under app_dir.
        if not fpath.startswith(app_dir + os.sep):
            handler._json({"error": "forbidden path"}, 403)
            return
        if not os.path.isfile(fpath):
            handler._json({"error": "source file missing"}, 404)
            return
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            st = os.stat(fpath)
            handler._json({
                "mcp_id": mcp_id,
                "module": mod,
                "rel_path": f"app/{mod}.py",
                "content": text,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except Exception as _re_err:
            handler._json({"error": f"read failed: {_re_err}"}, 500)

    elif path == "/api/portal/mcp/nodes":
        mcp_mgr = get_mcp_manager()
        # Return full per-node config so the UI can list MCPs + bindings
        nodes_out = {}
        for nid, cfg in mcp_mgr.node_configs.items():
            nodes_out[nid] = cfg.to_dict()
        handler._json({"nodes": nodes_out,
                    "summary": mcp_mgr.list_all_node_mcps()})

    elif path == "/api/portal/mcp/global":
        mcp_mgr = get_mcp_manager()
        handler._json({"global_mcps": {
            mid: cfg.to_dict()
            for mid, cfg in mcp_mgr.list_global_mcps().items()
        }})

    elif re.match(r"^/api/portal/mcp/node/([^/]+)$", path):
        nid = re.match(r"^/api/portal/mcp/node/([^/]+)$", path).group(1)
        mcp_mgr = get_mcp_manager()
        node_cfg = mcp_mgr.get_node_mcp_config(nid)
        handler._json(node_cfg.to_dict())

    elif re.match(r"^/api/portal/mcp/node/([^/]+)/agent/([^/]+)$", path):
        m = re.match(r"^/api/portal/mcp/node/([^/]+)/agent/([^/]+)$", path)
        nid, aid = m.group(1), m.group(2)
        mcp_mgr = get_mcp_manager()
        mcps = mcp_mgr.get_agent_effective_mcps(nid, aid)
        handler._json({"mcps": [m.to_dict() for m in mcps]})

    elif re.match(r"^/api/portal/mcp/recommend/([^/]+)$", path):
        role = re.match(r"^/api/portal/mcp/recommend/([^/]+)$", path).group(1)
        mcp_mgr = get_mcp_manager()
        recs = mcp_mgr.resolve_mcp_for_role(role)
        handler._json({"recommendations": [r.to_dict() for r in recs]})

    elif path == "/api/portal/templates":
        # List all templates
        lib = get_template_library()
        role = parse_qs(urlparse(handler.path).query).get("role", [""])[0]
        category = parse_qs(urlparse(handler.path).query).get("category", [""])[0]
        templates = lib.list_templates(role=role, category=category)
        handler._json({"templates": [t.to_dict() for t in templates]})

    elif re.match(r"^/api/portal/templates/([^/]+)$", path):
        # Get template content
        tid = re.match(r"^/api/portal/templates/([^/]+)$", path).group(1)
        lib = get_template_library()
        tpl = lib.get_template(tid)
        if tpl:
            handler._json(tpl.to_dict(include_content=True))
        else:
            handler._json({"error": "Template not found"}, 404)

    elif "/chat-task/" in path and path.endswith("/stream"):
        # SSE stream for a background chat task
        # URL: /api/portal/chat-task/{task_id}/stream?cursor=0
        parts = path.split("/")
        task_id = parts[4]  # /api/portal/chat-task/{task_id}/stream
        query = parse_qs(urlparse(handler.path).query)
        cursor = int(query.get("cursor", ["0"])[0])

        mgr = get_chat_task_manager()
        task = mgr.get_task(task_id)
        if not task:
            handler._json({"error": "Task not found"}, 404)
            return

        handler._sse_start()
        # Send current status
        last_status = ""
        last_progress = -1
        def _send_status():
            nonlocal last_status, last_progress
            s = task.status.value
            p = task.progress
            if s != last_status or p != last_progress:
                handler._sse_send({
                    "type": "status",
                    "status": s,
                    "progress": p,
                    "phase": task.phase,
                })
                last_status = s
                last_progress = p

        _send_status()
        # Stream events with long-poll loop — keep alive as long as
        # the task is still running (no arbitrary timeout)
        poll_interval = 0.1  # 100ms
        status_interval = 0.3  # check status every 300ms for responsive progress
        keepalive_interval = 15.0  # send SSE comment every 15s to prevent proxy timeout
        last_status_check = time.time()
        last_keepalive = time.time()
        task_finished = False
        try:
            while True:
                events, new_cursor = task.get_events_since(cursor)
                for evt in events:
                    handler._sse_send(evt)
                cursor = new_cursor
                # Send status when events arrive OR periodically
                now = time.time()
                if events or (now - last_status_check) >= status_interval:
                    _send_status()
                    last_status_check = now
                # If task is done, send final and break
                if task.status in (ChatTaskStatus.COMPLETED,
                                   ChatTaskStatus.FAILED,
                                   ChatTaskStatus.ABORTED):
                    # Make sure we got all events
                    remaining, _ = task.get_events_since(cursor)
                    for evt in remaining:
                        handler._sse_send(evt)
                    task_finished = True
                    break
                # Send SSE keepalive comment to prevent connection timeout
                if (now - last_keepalive) >= keepalive_interval:
                    try:
                        handler.wfile.write(b": keepalive\n\n")
                        handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    last_keepalive = now
                time.sleep(poll_interval)
        except (BrokenPipeError, ConnectionResetError):
            pass
        # Only send [DONE] if the task actually finished
        if task_finished:
            try:
                handler.wfile.write(b"data: [DONE]\n\n")
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    elif "/chat-task/" in path and path.endswith("/status"):
        # Quick status check (no SSE, just JSON)
        parts = path.split("/")
        task_id = parts[4]
        mgr = get_chat_task_manager()
        task = mgr.get_task(task_id)
        if task:
            handler._json(task.to_dict())
        else:
            handler._json({"error": "Task not found"}, 404)

    elif path == "/api/portal/channels":
        router = get_router()
        channels = router.list_channels()
        handler._json({"channels": [ch.to_dict(mask_secrets=True) for ch in channels]})

    elif path == "/api/portal/channels/events":
        router = get_router()
        query = parse_qs(urlparse(handler.path).query)
        limit = int(query.get("limit", ["100"])[0])
        events = router.get_event_log(limit=limit)
        handler._json({"events": events})

    # -- Static asset serving: robots, souls, roles --
    elif path == "/api/portal/attachment":
        # Serve a file from an agent's workspace (or the global workspaces tree)
        # so chat bubbles can render `![](local-path)` markdown inline.
        # Query params:
        #   path      (required) — absolute path OR relative-to-workspace path
        #   agent_id  (optional) — if given and the file sits under that agent's
        #                          working_dir, it's also allowed even when
        #                          working_dir is outside the standard tree
        import mimetypes
        from pathlib import Path as _P
        from .. import DEFAULT_DATA_DIR as _DDD
        qs = parse_qs(urlparse(handler.path).query)
        req_path = (qs.get("path", [""])[0] or "").strip()
        req_agent = (qs.get("agent_id", [""])[0] or "").strip()
        if not req_path:
            handler._json({"error": "path required"}, 400)
            return
        # Build the set of allowed base dirs. The global `workspaces/` tree is
        # always included so any file under any agent's workspace is reachable.
        # Per-agent we also add every plausible "home" path the agent might
        # have used, because agents often report paths relative to whatever
        # `cwd` they perceive (effective working_dir, agent home, private
        # workspace, shared project workspace).
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or _DDD
        allowed_bases: list[str] = [
            os.path.normpath(str(_P(data_dir) / "workspaces")),
        ]
        agent_obj = None
        if req_agent:
            try:
                agent_obj = hub.get_agent(req_agent)
            except Exception as _e_ga:
                logger.debug("attachment: get_agent failed: %s", _e_ga)
            if agent_obj is not None:
                try:
                    for getter in (
                        "_effective_working_dir",
                        "_get_agent_workspace",
                        "_get_agent_home",
                    ):
                        fn = getattr(agent_obj, getter, None)
                        if callable(fn):
                            try:
                                p = fn()
                                if p:
                                    allowed_bases.append(os.path.normpath(str(p)))
                            except Exception as _e_gw:
                                logger.debug("attachment: %s failed: %s", getter, _e_gw)
                    sw = getattr(agent_obj, "shared_workspace", "") or ""
                    if sw:
                        allowed_bases.append(os.path.normpath(str(sw)))
                    wd_attr = getattr(agent_obj, "working_dir", "") or ""
                    if wd_attr:
                        allowed_bases.append(os.path.normpath(str(wd_attr)))
                except Exception as _e_bases:
                    logger.debug("attachment: base collection failed: %s", _e_bases)
        # De-dupe preserving order
        seen_b: set = set()
        uniq_bases: list[str] = []
        for b in allowed_bases:
            if b and b not in seen_b:
                seen_b.add(b)
                uniq_bases.append(b)
        allowed_bases = uniq_bases

        # Normalise the requested path for lookup. Strip `./` and leading `/`
        # on the client-supplied path so `./foo.png` and `foo.png` behave the same.
        req_path_norm = req_path.replace("\\", "/").lstrip("./").lstrip("/")

        # Resolve requested path. If absolute, keep as-is; otherwise try each
        # allowed base in turn.
        candidate: str = ""
        tried: list[str] = []
        if os.path.isabs(req_path):
            candidate = os.path.normpath(req_path)
            tried.append(candidate)
        else:
            for base in allowed_bases:
                maybe = os.path.normpath(os.path.join(base, req_path_norm))
                tried.append(maybe)
                if os.path.isfile(maybe):
                    candidate = maybe
                    break

        # Last-resort: basename walk within the global workspaces tree, bounded
        # to keep this cheap. Only triggers when a relative lookup missed. This
        # rescues cases where the agent reported only a file name but placed
        # the file in a subdir (e.g. `./blog-screenshot.png` but file lives at
        # `.../workspace/outputs/blog-screenshot.png`).
        if not candidate and not os.path.isabs(req_path):
            basename = os.path.basename(req_path_norm)
            if basename:
                MAX_ENTRIES = PROJECT_SCAN_MAX_FILES
                scanned = 0
                # Narrow the walk: prefer the agent's own tree first
                walk_roots: list[str] = []
                if agent_obj is not None:
                    try:
                        home_fn = getattr(agent_obj, "_get_agent_home", None)
                        if callable(home_fn):
                            walk_roots.append(str(home_fn()))
                    except Exception:
                        pass
                # Always also search under shared/ and agents/ global trees
                walk_roots.append(str(_P(data_dir) / "workspaces"))
                seen_roots: set = set()
                for root in walk_roots:
                    if not root or root in seen_roots or not os.path.isdir(root):
                        continue
                    seen_roots.add(root)
                    found = False
                    for dirpath, dirnames, filenames in os.walk(root):
                        # Skip noisy dirs
                        dirnames[:] = [d for d in dirnames
                                       if not d.startswith(".")
                                       and d not in ("node_modules", "__pycache__",
                                                     "large_results", "memories")]
                        scanned += len(filenames)
                        if basename in filenames:
                            candidate = os.path.normpath(
                                os.path.join(dirpath, basename))
                            tried.append(candidate)
                            found = True
                            break
                        if scanned > MAX_ENTRIES:
                            break
                    if found:
                        break

        # Path-traversal check: resolved path must live under an allowed base.
        safe = False
        for base in allowed_bases:
            try:
                if candidate and (candidate == base
                                  or candidate.startswith(base + os.sep)):
                    safe = True
                    break
            except Exception:
                pass
        if not candidate or not safe:
            logger.info(
                "attachment: not found — req=%r agent=%s bases=%s tried=%s",
                req_path, req_agent[:8] if req_agent else "-",
                [os.path.basename(b) for b in allowed_bases],
                [os.path.basename(t) for t in tried[:5]],
            )
            handler._json({
                "error": "not found",
                "hint": "File must live under the agent's workspace tree.",
            }, 404)
            return
        if not os.path.isfile(candidate):
            handler._json({"error": "not found"}, 404)
            return
        # Only serve common image / small media types to keep this endpoint
        # narrow — it's meant for inline display, not a general file download.
        ALLOWED_EXT = {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico",
        }
        ext = os.path.splitext(candidate)[1].lower()
        if ext not in ALLOWED_EXT:
            handler._json({"error": "file type not allowed for inline view"}, 415)
            return
        try:
            size = os.path.getsize(candidate)
        except OSError:
            size = 0
        # 25 MB cap to avoid accidentally streaming huge files.
        if size > MAX_FILE_SERVE:
            handler._json({"error": "file too large"}, 413)
            return
        mime = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        try:
            with open(candidate, "rb") as f:
                data = f.read()
            handler.send_response(200)
            handler.send_header("Content-Type", mime)
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "private, max-age=300")
            handler.end_headers()
            try:
                handler.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        except Exception as _e_read:
            logger.error("attachment: read failed %s: %s", candidate, _e_read)
            try:
                handler._json({"error": "read failed"}, 500)
            except Exception:
                pass
        return

    elif path.startswith("/static/"):
        import mimetypes
        rel = path.lstrip("/")  # e.g. "static/robots/robot_ceo.svg" or "static/js/portal_login.js"
        sub = rel.replace("static/", "", 1)  # e.g. "robots/robot_ceo.svg" or "js/portal_login.js"

        # JS files live under app/server/static/; everything else under app/static/
        server_static_dir = os.path.join(os.path.dirname(__file__), "static")
        app_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")

        # Try app/server/static/ first (JS modules), then app/static/ (robots, config, etc.)
        file_path = None
        for base_dir in (server_static_dir, app_static_dir):
            candidate = os.path.normpath(os.path.join(base_dir, sub))
            if candidate.startswith(os.path.normpath(base_dir)) and os.path.isfile(candidate):
                file_path = candidate
                break

        if file_path:
            mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            with open(file_path, "rb") as f:
                data = f.read()
            handler.send_response(200)
            handler.send_header("Content-Type", mime)
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "public, max-age=3600")
            handler.end_headers()
            handler.wfile.write(data)
        else:
            handler.send_error(404)

    elif path.startswith("/workspace/"):
        # Serve files that an agent references in chat as
        #   http://host:port/workspace/<relative-path>
        # Resolve against the global workspaces tree (all agents). Supports:
        #   1. Direct relative path:  /workspace/agents/<aid>/workspace/foo.md
        #   2. Basename search:       /workspace/lessons_learned.md
        # Path-traversal protected: result must live under DATA_DIR/workspaces.
        import mimetypes as _mt
        from pathlib import Path as _P
        from .. import DEFAULT_DATA_DIR as _DDD2

        rel = path[len("/workspace/"):]
        rel_norm = rel.replace("\\", "/").lstrip("./").lstrip("/")
        if not rel_norm:
            handler.send_error(404)
            return

        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or _DDD2
        ws_root = os.path.normpath(str(_P(data_dir) / "workspaces"))

        candidate = ""
        # 1. Try direct path under workspaces/
        direct = os.path.normpath(os.path.join(ws_root, rel_norm))
        if (direct == ws_root or direct.startswith(ws_root + os.sep)) \
                and os.path.isfile(direct):
            candidate = direct

        # 2. Basename walk (bounded) if direct missed
        if not candidate:
            basename = os.path.basename(rel_norm)
            if basename and os.path.isdir(ws_root):
                MAX_ENTRIES = PROJECT_SCAN_MAX_FILES
                scanned = 0
                found = False
                for dirpath, dirnames, filenames in os.walk(ws_root):
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".")
                                   and d not in ("node_modules", "__pycache__",
                                                 "large_results", "memories")]
                    scanned += len(filenames)
                    if basename in filenames:
                        candidate = os.path.normpath(
                            os.path.join(dirpath, basename))
                        found = True
                        break
                    if scanned > MAX_ENTRIES:
                        break
                if not found:
                    candidate = ""

        # 3. Path-traversal check: resolved must live under workspaces/
        safe = (candidate == ws_root
                or candidate.startswith(ws_root + os.sep)) if candidate else False

        if not candidate or not safe or not os.path.isfile(candidate):
            logger.info("workspace: not found — rel=%r ws_root=%s",
                        rel_norm, ws_root)
            handler.send_error(404)
            return

        mime = _mt.guess_type(candidate)[0] or "application/octet-stream"
        # Text files: force utf-8 charset so Chinese content renders correctly
        if mime.startswith("text/"):
            mime = f"{mime}; charset=utf-8"
        elif mime in ("application/json", "application/javascript"):
            mime = f"{mime}; charset=utf-8"
        try:
            with open(candidate, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning("workspace read failed %s: %s", candidate, e)
            handler.send_error(500)
            return
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "private, max-age=60")
        handler.end_headers()
        handler.wfile.write(data)

    elif path.startswith("/api/portal/agent/") and path.endswith("/thinking"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if not agent:
            handler._json({"error": "Agent not found"}, 404)
            return
        stats = agent.active_thinking.get_stats() if agent.active_thinking else {"enabled": False}
        history = []
        if agent.active_thinking:
            history = [r.to_dict() for r in agent.active_thinking.history[-10:]]
        handler._json({"stats": stats, "history": history})

    elif path == "/api/portal/audio/events":
        # Audio events endpoint: TTS speak / STT listen events
        # get_audio_events not yet in tools.py; return empty for now
        try:
            from .tools import get_audio_events
            qs = parse_qs(urlparse(handler.path).query)
            since = float(qs.get("since", ["0"])[0])
            events = get_audio_events(since=int(since))
        except (ImportError, AttributeError):
            events = []
        handler._json({"events": events})

    elif path == "/api/portal/roles":
        # Return all role definitions (souls + robots mapping)
        roles_file = os.path.join(os.path.dirname(__file__),
                                  "static", "config", "roles.json")
        if os.path.isfile(roles_file):
            with open(roles_file, "r", encoding="utf-8") as f:
                handler._json(json.load(f))
        else:
            handler._json({"roles": []})

    # ---- Experience Library GET endpoints ----
    elif path == "/api/portal/experience/stats":
        from ..experience_library import get_experience_library
        lib = get_experience_library()
        handler._json(lib.get_stats())

    elif path.startswith("/api/portal/experience/list"):
        qs = parse_qs(urlparse(handler.path).query)
        role = qs.get("role", [""])[0]
        if not role:
            handler._json({"error": "role parameter required"}, 400)
            return
        from ..experience_library import get_experience_library
        lib = get_experience_library()
        exps = lib.get_all_experiences(role)
        handler._json({
            "role": role,
            "count": len(exps),
            "experiences": [e.to_dict() for e in exps[-100:]],
        })

    elif path == "/api/portal/experience/history":
        # Collect recent retro/learning history from all agents
        history = []
        for agent in hub.agents.values():
            si = getattr(agent, 'self_improvement', None)
            if si:
                for r in getattr(si, 'retrospective_history', [])[-10:]:
                    history.append({
                        "type": "retrospective",
                        "agent_name": r.get("agent_name", agent.name),
                        "summary": r.get("what_happened", "")[:100],
                        "new_count": len(r.get("new_experiences", [])),
                        "created_at": r.get("created_at", 0),
                    })
                for l in getattr(si, 'learning_history', [])[-10:]:
                    history.append({
                        "type": "active_learning",
                        "agent_name": l.get("agent_name", agent.name),
                        "summary": l.get("learning_goal", "")[:100],
                        "new_count": len(l.get("new_experiences", [])),
                        "created_at": l.get("created_at", 0),
                    })
        history.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        handler._json({"history": history[:30]})

    elif path == "/api/portal/experience/plans":
        # Return learning plan data grouped by lifecycle state so the UI can
        # render a real closed loop: goal → plan → completion → conversion.
        plans: list[dict] = []
        queued_total = 0
        running_total = 0
        completed_total = 0
        converted_total = 0  # completed plans that produced >=1 experience
        exp_produced = 0     # total experiences produced by completed plans

        for agent in hub.agents.values():
            si = getattr(agent, 'self_improvement', None)
            if not si:
                continue
            agent_role = getattr(agent, 'role', '') or ''
            agent_name = getattr(agent, 'name', '') or ''

            # ---- Queued tasks (haven't started) ----
            for q in list(getattr(si, '_learning_queue', []) or []):
                queued_total += 1
                plans.append({
                    "id": q.get("id", ""),
                    "state": "queued",
                    "agent_id": agent.id,
                    "agent_name": agent_name,
                    "role": agent_role,
                    "learning_goal": q.get("learning_goal", ""),
                    "knowledge_gap": q.get("knowledge_gap", ""),
                    "source_type": "",
                    "source_detail": "",
                    "key_findings": "",
                    "applicable_scenes": "",
                    "new_experiences": [],
                    "queued_at": q.get("queued_at", 0),
                    "started_at": 0,
                    "completed_at": 0,
                    "created_at": q.get("queued_at", 0),
                })

            # ---- Currently running task ----
            cur = getattr(si, '_current_learning', None)
            if cur:
                running_total += 1
                plans.append({
                    "id": cur.get("id", ""),
                    "state": "running",
                    "agent_id": agent.id,
                    "agent_name": agent_name,
                    "role": agent_role,
                    "learning_goal": cur.get("learning_goal", ""),
                    "knowledge_gap": cur.get("knowledge_gap", ""),
                    "source_type": "",
                    "source_detail": "",
                    "key_findings": "",
                    "applicable_scenes": "",
                    "new_experiences": [],
                    "queued_at": cur.get("queued_at", 0),
                    "started_at": cur.get("started_at", 0),
                    "completed_at": 0,
                    "created_at": cur.get("started_at") or cur.get("queued_at", 0),
                })

            # ---- Completed history ----
            for l in getattr(si, 'learning_history', []) or []:
                completed_total += 1
                new_exps = l.get("new_experiences", []) or []
                if new_exps:
                    converted_total += 1
                    exp_produced += len(new_exps)
                plans.append({
                    "id": l.get("id", ""),
                    "state": "completed",
                    "agent_id": agent.id,
                    "agent_name": l.get("agent_name", agent_name),
                    "role": l.get("role", agent_role),
                    "learning_goal": l.get("learning_goal", ""),
                    "knowledge_gap": "",
                    "source_type": l.get("source_type", ""),
                    "source_detail": l.get("source_detail", ""),
                    "key_findings": l.get("key_findings", ""),
                    "applicable_scenes": l.get("applicable_scenes", ""),
                    "new_experiences": new_exps,
                    "queued_at": 0,
                    "started_at": 0,
                    "completed_at": l.get("created_at", 0),
                    "created_at": l.get("created_at", 0),
                })

        # Newest first across all states
        plans.sort(key=lambda x: x.get("created_at", 0), reverse=True)

        total = queued_total + running_total + completed_total
        completion_rate = (completed_total / total) if total else 0.0
        conversion_rate = (converted_total / completed_total) if completed_total else 0.0

        handler._json({
            "plans": plans[:60],
            "summary": {
                "total": total,
                "queued": queued_total,
                "running": running_total,
                "completed": completed_total,
                "converted": converted_total,
                "experiences_produced": exp_produced,
                "completion_rate": round(completion_rate, 3),
                "conversion_rate": round(conversion_rate, 3),
            },
        })

    elif path == "/api/portal/experience/insights":
        # Return retrospective insights, latest first (latest overrides history)
        insights = []
        for agent in hub.agents.values():
            si = getattr(agent, 'self_improvement', None)
            if si:
                for r in getattr(si, 'retrospective_history', []):
                    insights.append({
                        "agent_name": r.get("agent_name", agent.name),
                        "role": r.get("role", getattr(agent, 'role', '')),
                        "what_happened": r.get("what_happened", ""),
                        "what_went_well": r.get("what_went_well", ""),
                        "what_went_wrong": r.get("what_went_wrong", ""),
                        "root_cause": r.get("root_cause", ""),
                        "improvement_plan": r.get("improvement_plan", ""),
                        "new_experiences": r.get("new_experiences", []),
                        "created_at": r.get("created_at", 0),
                    })
        # Sort newest first — latest overrides history
        insights.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        handler._json({"insights": insights[:30]})

    elif path.startswith("/api/portal/agent/") and path.endswith("/soul"):
        agent_id = path.split("/")[4]
        agent = hub.get_agent(agent_id)
        if not agent:
            handler._json({"error": "Agent not found"}, 404)
            return
        # Return the agent's SOUL.md content
        soul_content = getattr(agent, 'soul_md', '') or ''
        if not soul_content:
            # Load default from role template
            role = agent.role or 'general'
            soul_file = os.path.join(os.path.dirname(__file__),
                                     "static", "souls", f"soul_{role}.md")
            if os.path.isfile(soul_file):
                with open(soul_file, "r", encoding="utf-8") as f:
                    soul_content = f.read()
        robot_id = getattr(agent, 'robot_avatar', '') or ''
        handler._json({"soul_md": soul_content, "robot_avatar": robot_id,
                    "agent_id": agent_id, "role": agent.role})

    elif path == "/api/portal/knowledge":
        # List all knowledge entries
        entries = knowledge.list_entries()
        handler._json({"entries": entries})

    elif path == "/api/portal/knowledge/search":
        # Search knowledge entries
        qs = parse_qs(urlparse(handler.path).query)
        query = qs.get("q", [""])[0]
        if not query:
            handler._json({"entries": []})
            return
        entries = knowledge.search(query)
        handler._json({"entries": entries})

    # --- RAG Provider Registry (GET) ---
    elif path == "/api/portal/rag/providers":
        from ..rag_provider import get_rag_registry
        reg = get_rag_registry()
        handler._json({"providers": [p.to_dict() for p in reg.list_providers()]})

    # --- Orchestration topology (agents/projects/tasks/sub-agents graph) ---
    elif path == "/api/portal/orchestration":
        qs = parse_qs(urlparse(handler.path).query)
        project_filter = qs.get("project", [""])[0]
        try:
            graph = _build_orchestration_graph(hub, project_filter)
        except Exception as e:
            logger.warning("orchestration graph build failed: %s", e)
            graph = {"nodes": [], "edges": [], "error": str(e)}
        handler._json(graph)

    # --- Cross-node aggregated audit log ---
    elif path == "/api/portal/audit/aggregated":
        qs = parse_qs(urlparse(handler.path).query)
        limit = int((qs.get("limit", ["500"])[0]) or 500)
        action_filter = qs.get("action", [""])[0]
        actor_filter = qs.get("actor", [""])[0]
        node_filter = qs.get("node", [""])[0]
        auth_mgr = get_auth()
        entries = auth_mgr.get_audit_log(
            limit=max(1, min(limit, 5000)),
            action=action_filter, actor=actor_filter,
        )
        if node_filter:
            tag = f"[node:{node_filter}]"
            entries = [e for e in entries if tag in (e.get("detail") or "")]
        # Build per-node summary
        from collections import Counter as _Counter
        node_counts = _Counter()
        for e in entries:
            d = e.get("detail") or ""
            if d.startswith("[node:"):
                end = d.find("]")
                if end > 0:
                    node_counts[d[6:end]] += 1
            else:
                node_counts["local"] += 1
        handler._json({
            "entries": entries,
            "total": len(entries),
            "by_node": dict(node_counts),
        })

    # --- Skill packages (new skill registry, distinct from legacy SKILL.md skills) ---
    elif path == "/api/portal/skill-pkgs":
        try:
            reg = getattr(hub, "skill_registry", None)
            items = [i.to_dict() for i in (reg.list_all() if reg else [])]
        except Exception as _e:
            items = []
            logger.debug("skill-pkgs list failed: %s", _e)
        handler._json({"skills": items})

    elif path.startswith("/api/portal/skill-pkgs/") and not path.endswith("/agents"):
        sid = path.split("/")[-1]
        reg = getattr(hub, "skill_registry", None)
        if not reg:
            handler._json({"error": "skill registry unavailable"}, 503)
            return
        inst = reg.get(sid)
        if not inst:
            handler._json({"error": "skill not found"}, 404)
            return
        handler._json({"skill": inst.to_dict()})

    elif path.startswith("/api/portal/agent/") and path.endswith("/skill-pkgs"):
        agent_id = path.split("/")[4]
        reg = getattr(hub, "skill_registry", None)
        if not reg:
            handler._json({"skills": []})
            return
        items = [i.to_dict() for i in reg.list_for_agent(agent_id)]
        handler._json({"skills": items})

    elif path.startswith("/api/portal/i18n/"):
        from .. import i18n as _i18n
        locale = path.split("/")[-1]
        try:
            handler._json({"locale": locale, "table": _i18n.get_locale_table(locale)})
        except Exception as _e:
            handler._json({"error": str(_e)}, 500)

    else:
        # Return JSON 404 for /api/ routes so frontend can parse it
        if path.startswith("/api/"):
            handler._json({"error": f"Not found: {path}"}, status=404)
        else:
            handler.send_error(404)
