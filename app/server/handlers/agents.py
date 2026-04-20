"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
agents — handler implementations for agent-management POST endpoints.

Extracted from portal_routes_post.py (Phase 2).  Handles:

    POST /api/portal/agent/create
    POST /api/portal/agent/{id}/chat
    POST /api/portal/chat-task/{id}/abort
    POST /api/portal/agent/{id}/save-file
    POST /api/portal/agent/{id}/save-session
    POST /api/portal/agent/{id}/load-session
    POST /api/portal/agent/{id}/save-engine
    POST /api/portal/agent/{id}/restore-engine
    POST /api/portal/agent/{id}/compact-memory
    POST /api/portal/agent/{id}/exec-src-tool
    POST /api/portal/agent/{id}/exec-src-command
    POST /api/portal/agent/{id}/wake
    POST /api/portal/agent/{id}/clear
    POST /api/portal/agent/{id}/model
    POST /api/portal/agent/{id}/learning-model
    POST /api/portal/agent/{id}/growth-task
    POST /api/portal/agent/{id}/profile
    POST /api/portal/agent/{id}/enhancement
    POST /api/portal/agent/{id}/tasks
    POST /api/portal/agent/{id}/prompt-packs
    POST /api/portal/agent/{id}/growth
    POST /api/portal/agent/{id}/persona
    POST /api/portal/agent/{id}/soul
    POST /api/portal/agent/{id}/thinking/enable
    POST /api/portal/agent/{id}/thinking/disable
    POST /api/portal/agent/{id}/thinking/trigger
    POST /api/portal/agent/{id}/thinking/history
    POST /api/portal/agent/workspace/authorize
    POST /api/portal/agent/workspace/revoke
    POST /api/portal/agent/workspace/list
    POST /api/portal/agent/{id}/self-improvement/enable
    POST /api/portal/agent/{id}/self-improvement/disable
"""
from __future__ import annotations

import json
import logging
import os
import re
import traceback
from pathlib import Path

from ...agent import (AgentProfile, get_chat_task_manager)
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth,
               actor_name: str, user_role: str) -> bool:
    """Return *True* if *path* was handled by this module, *False* otherwise."""

    # ── Agent creation ──
    if path == "/api/portal/agent/create":
        return _handle_create(handler, hub, body, auth, actor_name, user_role)

    # ── Chat ──
    if path.startswith("/api/portal/agent/") and path.endswith("/chat"):
        return _handle_chat(handler, path, hub, body, auth, actor_name, user_role)

    # ── Abort chat task ──
    if "/chat-task/" in path and path.endswith("/abort"):
        return _handle_chat_task_abort(handler, path, hub, body, auth, actor_name, user_role)

    # ── Save file to agent workspace ──
    if path.startswith("/api/portal/agent/") and path.endswith("/save-file"):
        return _handle_save_file(handler, path, hub, body, auth, actor_name, user_role)

    # ── Session save / load ──
    if path.startswith("/api/portal/agent/") and path.endswith("/save-session"):
        return _handle_save_session(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/load-session"):
        return _handle_load_session(handler, path, hub, body, auth, actor_name, user_role)

    # ── Memory engine persistence ──
    if path.startswith("/api/portal/agent/") and path.endswith("/save-engine"):
        return _handle_save_engine(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/restore-engine"):
        return _handle_restore_engine(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/compact-memory"):
        return _handle_compact_memory(handler, path, hub, body, auth, actor_name, user_role)

    # ── SRC integration ──
    if path.startswith("/api/portal/agent/") and path.endswith("/exec-src-tool"):
        return _handle_exec_src_tool(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/exec-src-command"):
        return _handle_exec_src_command(handler, path, hub, body, auth, actor_name, user_role)

    # ── Wake agent ──
    if path.startswith("/api/portal/agent/") and path.endswith("/wake"):
        return _handle_wake(handler, path, hub, body, auth, actor_name, user_role)

    # ── Clear agent ──
    if path.startswith("/api/portal/agent/") and path.endswith("/clear"):
        return _handle_clear(handler, path, hub, body, auth, actor_name, user_role)

    # ── Model switching ──
    if path.startswith("/api/portal/agent/") and path.endswith("/model"):
        return _handle_model(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/learning-model"):
        return _handle_learning_model(handler, path, hub, body, auth, actor_name, user_role)

    # ── Growth task ──
    if path.startswith("/api/portal/agent/") and path.endswith("/growth-task"):
        return _handle_growth_task(handler, path, hub, body, auth, actor_name, user_role)

    # ── Profile update ──
    if path.startswith("/api/portal/agent/") and "/profile" in path:
        return _handle_profile(handler, path, hub, body, auth, actor_name, user_role)

    # ── Enhancement module ──
    if re.match(r"^/api/portal/agent/([^/]+)/enhancement$", path):
        return _handle_enhancement(handler, path, hub, body, auth, actor_name, user_role)

    # ── Agent tasks CRUD ──
    if path.startswith("/api/portal/agent/") and path.endswith("/tasks"):
        return _handle_tasks(handler, path, hub, body, auth, actor_name, user_role)

    # ── Prompt packs / skill management ──
    if path.startswith("/api/portal/agent/") and path.endswith("/prompt-packs"):
        return _handle_prompt_packs(handler, path, hub, body, auth, actor_name, user_role)

    # ── Role growth path ──
    if path.startswith("/api/portal/agent/") and path.endswith("/growth"):
        return _handle_growth(handler, path, hub, body, auth, actor_name, user_role)

    # ── Persona ──
    if path.startswith("/api/portal/agent/") and path.endswith("/persona"):
        return _handle_persona(handler, path, hub, body, auth, actor_name, user_role)

    # ── Soul ──
    if path.startswith("/api/portal/agent/") and path.endswith("/soul"):
        return _handle_soul(handler, path, hub, body, auth, actor_name, user_role)

    # ── Active thinking ──
    if path.startswith("/api/portal/agent/") and path.endswith("/thinking/enable"):
        return _handle_thinking_enable(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/thinking/disable"):
        return _handle_thinking_disable(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/thinking/trigger"):
        return _handle_thinking_trigger(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/thinking/history"):
        return _handle_thinking_history(handler, path, hub, body, auth, actor_name, user_role)

    # ── Workspace authorization ──
    if path == "/api/portal/agent/workspace/authorize":
        return _handle_workspace_authorize(handler, hub, body, auth, actor_name, user_role)

    if path == "/api/portal/agent/workspace/revoke":
        return _handle_workspace_revoke(handler, hub, body, auth, actor_name, user_role)

    if path == "/api/portal/agent/workspace/list":
        return _handle_workspace_list(handler, hub, body, auth, actor_name, user_role)

    # ── Self-improvement ──
    if path.startswith("/api/portal/agent/") and path.endswith("/self-improvement/enable"):
        return _handle_self_improvement_enable(handler, path, hub, body, auth, actor_name, user_role)

    if path.startswith("/api/portal/agent/") and path.endswith("/self-improvement/disable"):
        return _handle_self_improvement_disable(handler, path, hub, body, auth, actor_name, user_role)

    return False


# ------------------------------------------------------------------
# Handler implementations
# ------------------------------------------------------------------

def _handle_create(handler, hub, body, auth, actor_name, user_role) -> bool:
    target_node = body.get("node_id", "local")
    logger.info("CREATE_AGENT request: name=%s role=%s target_node=%s actor=%s",
                body.get("name"), body.get("role"), target_node, actor_name)
    if target_node and target_node != "local" and target_node != hub.node_id:
        # Create agent on remote node
        node = hub.remote_nodes.get(target_node)
        logger.info("CREATE_AGENT remote: node_id=%s found=%s url=%s",
                    target_node, node is not None, node.url if node else "N/A")
        if node and node.url:
            try:
                headers = {"Content-Type": "application/json"}
                if node.secret:
                    headers["X-Claw-Secret"] = node.secret
                import requests as http_req
                # Forward create request to remote node (as local create)
                remote_body = dict(body)
                remote_body["node_id"] = "local"  # Tell remote to create locally
                target_url = f"{node.url}/api/portal/agent/create"
                logger.info("CREATE_AGENT -> POST %s body=%s has_secret=%s",
                            target_url,
                            {k: v for k, v in remote_body.items() if k != "system_prompt"},
                            bool(node.secret))
                resp = http_req.post(target_url,
                    headers=headers, json=remote_body, timeout=15)
                logger.info("CREATE_AGENT <- status=%s length=%s",
                            resp.status_code, len(resp.text))
                if resp.status_code != 200:
                    err_msg = resp.text[:500]
                    logger.error("CREATE_AGENT REMOTE FAIL: status=%s body=%s",
                                 resp.status_code, err_msg)
                    handler._json({"error": f"Remote node returned {resp.status_code}: {err_msg}"}, resp.status_code)
                    return True
                data = resp.json()
                logger.info("CREATE_AGENT remote OK: agent_id=%s", data.get("id", "?"))
                hub.refresh_node(target_node)
                auth.audit("create_remote_agent", actor=actor_name,
                           role=user_role, target=target_node,
                           ip=get_client_ip(handler))
                handler._json(data)
            except Exception as e:
                logger.exception("CREATE_AGENT remote EXCEPTION: %s", e)
                handler._json({"error": f"Remote create failed: {e}"}, 500)
        else:
            logger.error("CREATE_AGENT node not found: %s available=%s",
                         target_node, list(hub.remote_nodes.keys()))
            handler._json({"error": "Node not found"}, 404)
        return True

    # Local agent creation
    logger.info("CREATE_AGENT local: name=%s role=%s model=%s provider=%s priority=%s",
                body.get("name"), body.get("role"), body.get("model"), body.get("provider"),
                body.get("priority_level", 3))
    try:
        agent = hub.create_agent(
            name=body.get("name", ""),
            role=body.get("role", "general"),
            model=body.get("model", ""),
            provider=body.get("provider", ""),
            working_dir=body.get("working_dir", ""),
            system_prompt=body.get("system_prompt", ""),
            priority_level=int(body.get("priority_level", 3)),
            role_title=body.get("role_title", ""),
        )
    except ValueError as e:
        handler._json({"error": str(e)}, 400)
        return True
    if agent and body.get("profile"):
        prof = body["profile"]
        # Preserve previously-set fields we don't get from the form
        existing = agent.profile
        agent.profile = AgentProfile(
            agent_class=prof.get("agent_class", "") or existing.agent_class,
            memory_mode=prof.get("memory_mode", "") or existing.memory_mode,
            rag_mode=prof.get("rag_mode", "") or existing.rag_mode,
            rag_provider_id=prof.get("rag_provider_id", "") or existing.rag_provider_id,
            rag_collection_ids=prof.get("rag_collection_ids", []) or list(existing.rag_collection_ids),
            personality=prof.get("personality", "") or existing.personality,
            communication_style=prof.get("communication_style", "") or existing.communication_style,
            expertise=prof.get("expertise", []) or list(existing.expertise),
            skills=prof.get("skills", []) or list(existing.skills),
            language=prof.get("language", "auto") or existing.language,
            custom_instructions=prof.get("custom_instructions", "") or existing.custom_instructions,
            max_context_messages=int(prof.get("max_context_messages", existing.max_context_messages) or existing.max_context_messages),
            temperature=float(prof.get("temperature", existing.temperature) or existing.temperature),
            exec_policy=prof.get("exec_policy", "") or existing.exec_policy,
            allowed_tools=list(existing.allowed_tools),
            denied_tools=list(existing.denied_tools),
            auto_approve_tools=list(existing.auto_approve_tools),
            mcp_servers=list(existing.mcp_servers),
        )
        hub._save_agents()  # re-save with profile
    # Set robot avatar if specified
    if agent and body.get("robot_avatar"):
        agent.robot_avatar = body["robot_avatar"]
        hub._save_agents()
    # Apply persona template if user selected one -- this overrides
    # personality/communication_style/expertise/skills/temperature +
    # system_prompt with the persona's curated values.
    if agent and body.get("persona_id"):
        try:
            from ...persona import apply_persona_to_agent
            apply_persona_to_agent(agent, body["persona_id"])
            hub._save_agents()
            logger.info("CREATE_AGENT persona applied: agent=%s persona=%s",
                        agent.id, body["persona_id"])
        except Exception as e:
            logger.warning("persona apply failed: %s", e)
    logger.info("CREATE_AGENT local OK: id=%s name=%s",
                agent.id if agent else "NONE", agent.name if agent else "")
    auth.audit("create_agent", actor=actor_name, role=user_role,
               target=agent.id if agent else "unknown",
               ip=get_client_ip(handler))
    handler._json(agent.to_dict() if agent else {})
    return True


def _handle_chat(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        # Try proxy to remote node
        data = hub.proxy_remote_agent_post(agent_id, "/chat", body)
        if data:
            handler._json(data)
        else:
            handler._json({"error": "Agent not found (local or remote)"}, 404)
        return True
    user_msg = body.get("message", "").strip()
    if not user_msg:
        handler._json({"error": "Empty message"}, 400)
        return True

    auth.audit("chat", actor=actor_name, role=user_role, target=agent_id,
               ip=get_client_ip(handler))

    # Submit as background task -- return task ID immediately
    # Tag message with source="admin" to indicate it came from portal UI
    task = agent.chat_async(user_msg, source="admin")
    handler._json({"task_id": task.id, "status": task.status.value})
    return True


def _handle_chat_task_abort(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    # POST /api/portal/chat-task/{task_id}/abort
    parts = path.rstrip("/").split("/")
    task_id = parts[4]
    mgr = get_chat_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        handler._json({"error": "Task not found"}, 404)
        return True
    task.abort()
    auth.audit("abort_task", actor=actor_name, role=user_role,
               target=task_id, ip=get_client_ip(handler))
    handler._json({"ok": True, "status": task.status.value})
    return True


def _handle_save_file(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    # POST /api/portal/agent/{agent_id}/save-file
    # Save content to a file in the agent's working directory
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    filename = body.get("filename", "").strip()
    content = body.get("content", "")
    if not filename:
        handler._json({"error": "Filename is required"}, 400)
        return True
    # Sanitize filename: prevent directory traversal
    filename = filename.replace("\\", "/")
    if ".." in filename or filename.startswith("/"):
        handler._json({"error": "Invalid filename"}, 400)
        return True
    # Never fall back to os.getcwd() -- that's the server-process CWD
    # (often the code package directory). Use the agent's private
    # workspace instead so runtime files stay out of the code tree.
    base_dir = agent.working_dir or str(agent._effective_working_dir())
    file_path = os.path.join(base_dir, filename)
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        auth.audit("save_file", actor=actor_name, role=user_role,
                   target=agent_id, detail=filename,
                   ip=get_client_ip(handler))
        handler._json({"ok": True, "path": file_path, "size": len(content)})
    except Exception as e:
        handler._json({"error": f"Failed to save file: {e}"}, 500)
    return True


def _handle_save_session(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    saved = hub.save_agent_session(agent_id)
    if saved:
        handler._json({"ok": True, "path": saved})
    else:
        handler._json({"error": "Agent not found or save failed"}, 404)
    return True


def _handle_load_session(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    ok = hub.load_agent_session(agent_id)
    handler._json({"ok": ok})
    return True


def _handle_save_engine(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    saved = hub.save_engine_session(agent_id)
    handler._json({"ok": bool(saved), "path": saved})
    return True


def _handle_restore_engine(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    ok = hub.restore_engine_session(agent_id)
    handler._json({"ok": ok})
    return True


def _handle_compact_memory(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    ok = hub.compact_agent_memory(agent_id)
    handler._json({"ok": ok})
    return True


def _handle_exec_src_tool(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    tool_name = body.get("tool", "")
    payload = body.get("payload", "")
    handler._json(hub.execute_src_tool(agent_id, tool_name, payload))
    return True


def _handle_exec_src_command(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    cmd_name = body.get("command", "")
    prompt = body.get("prompt", "")
    handler._json(hub.execute_src_command(agent_id, cmd_name, prompt))
    return True


def _handle_wake(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    # POST /api/portal/agent/{agent_id}/wake -- wake up agent, scan all projects
    # for tasks assigned to it that are not yet completed, spawn background execution.
    agent_id = path.split("/")[4]
    max_tasks = int(body.get("max_tasks", 5) or 5)
    result = hub.wake_up_agent(agent_id, max_tasks=max_tasks)
    auth.audit("wake_agent", actor=actor_name, role=user_role,
               target=agent_id, ip=get_client_ip(handler))
    handler._json(result)
    return True


def _handle_clear(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if agent:
        agent.clear()
        auth.audit("clear_agent", actor=actor_name, role=user_role,
                   target=agent_id, ip=get_client_ip(handler))
        handler._json({"ok": True})
    else:
        handler._json({"error": "Agent not found"}, 404)
    return True


def _handle_model(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if agent:
        agent.provider = body.get("provider", "")
        agent.model = body.get("model", "")
        hub._save_agents()
        auth.audit("switch_model", actor=actor_name, role=user_role,
                   target=agent_id,
                   detail=f"{agent.provider or 'default'}/{agent.model or 'default'}",
                   ip=get_client_ip(handler))
        handler._json({"ok": True, "provider": agent.provider, "model": agent.model})
    else:
        # Try remote agent
        node = hub.find_agent_node(agent_id)
        if node:
            hub.proxy_update_model(agent_id, node, body.get("provider", ""), body.get("model", ""))
            handler._json({"ok": True})
        else:
            handler._json({"error": "Agent not found"}, 404)
    return True


def _handle_learning_model(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    # POST /api/portal/agent/{agent_id}/learning-model
    # Body: {provider, model} -- sets the cheap/local LLM used for self-growth tasks.
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    agent.learning_provider = (body.get("provider", "") or "").strip()
    agent.learning_model = (body.get("model", "") or "").strip()
    hub._save_agents()
    auth.audit("set_learning_model", actor=actor_name, role=user_role,
               target=agent_id,
               detail=f"{agent.learning_provider or 'default'}/{agent.learning_model or 'default'}",
               ip=get_client_ip(handler))
    handler._json({
        "ok": True,
        "learning_provider": agent.learning_provider,
        "learning_model": agent.learning_model,
    })
    return True


def _handle_growth_task(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    # POST /api/portal/agent/{agent_id}/growth-task
    # Body: {learning_goal, knowledge_gap, title?}
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    try:
        task = agent.enqueue_growth_task(
            learning_goal=(body.get("learning_goal", "") or "").strip(),
            knowledge_gap=(body.get("knowledge_gap", "") or "").strip(),
            title=(body.get("title", "") or "").strip(),
        )
        hub._save_agents()
        auth.audit("enqueue_growth_task", actor=actor_name, role=user_role,
                   target=agent_id, detail=task.title,
                   ip=get_client_ip(handler))
        handler._json({"ok": True, "task": task.to_dict()})
    except Exception as e:
        handler._json({"error": str(e)}, 500)
    return True


def _handle_profile(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if agent:
        try:
            # Update core fields if provided
            if "name" in body and body["name"].strip():
                agent.name = body["name"].strip()
            if "role" in body:
                agent.role = body["role"]
            if "working_dir" in body:
                # Normalize working_dir so the agent's jail root is always
                # an absolute, user-intended path.
                _raw_wd = (body.get("working_dir") or "").strip()
                if not _raw_wd:
                    # Empty -> fall back to default private workspace
                    try:
                        _ws = agent._ensure_workspace_layout()
                        agent.working_dir = str(_ws)
                    except Exception:
                        agent.working_dir = ""
                else:
                    try:
                        _p = os.path.expanduser(_raw_wd)
                        if not os.path.isabs(_p):
                            # Relative path -> append under the agent's
                            # default private workspace base.
                            try:
                                _base = agent._ensure_workspace_layout()
                            except Exception:
                                _base = None
                            if _base is None:
                                handler._json({
                                    "error": (
                                        "cannot resolve relative working_dir: "
                                        "default workspace unavailable"
                                    )
                                }, 400)
                                return True
                            _resolved = (Path(str(_base)) / _p).resolve()
                            try:
                                _resolved.mkdir(parents=True, exist_ok=True)
                            except OSError:
                                pass
                            agent.working_dir = str(_resolved)
                        else:
                            agent.working_dir = os.path.abspath(_p)
                    except Exception as _wd_err:
                        handler._json({"error": f"invalid working_dir: {_wd_err}"}, 400)
                        return True
            if "provider" in body:
                agent.provider = body["provider"]
            if "model" in body:
                agent.model = body["model"]
            # -- learning / multimodal dedicated LLM slots --
            if "learning_provider" in body:
                agent.learning_provider = str(body.get("learning_provider") or "")
            if "learning_model" in body:
                agent.learning_model = str(body.get("learning_model") or "")
            if "multimodal_provider" in body:
                agent.multimodal_provider = str(body.get("multimodal_provider") or "")
            if "multimodal_model" in body:
                agent.multimodal_model = str(body.get("multimodal_model") or "")
            # -- extra_llms: arbitrary N LLM slots --
            if "extra_llms" in body:
                raw_slots = body.get("extra_llms") or []
                if not isinstance(raw_slots, list):
                    raw_slots = []
                cleaned: list[dict] = []
                for s in raw_slots:
                    if not isinstance(s, dict):
                        continue
                    label = str(s.get("label") or "").strip()
                    if not label:
                        continue
                    cleaned.append({
                        "label": label,
                        "provider": str(s.get("provider") or "").strip(),
                        "model": str(s.get("model") or "").strip(),
                        "purpose": str(s.get("purpose") or "").strip(),
                        "note": str(s.get("note") or "").strip(),
                    })
                agent.extra_llms = cleaned
            # -- auto_route heuristic routing --
            if "auto_route" in body:
                raw_ar = body.get("auto_route") or {}
                if not isinstance(raw_ar, dict):
                    raw_ar = {}
                try:
                    _threshold = int(raw_ar.get("complex_threshold_chars", 2000) or 2000)
                except (TypeError, ValueError):
                    _threshold = 2000
                agent.auto_route = {
                    "enabled": bool(raw_ar.get("enabled")),
                    "default": str(raw_ar.get("default") or "").strip(),
                    "complex": str(raw_ar.get("complex") or "").strip(),
                    "multimodal": str(raw_ar.get("multimodal") or "").strip(),
                    "complex_threshold_chars": max(1, _threshold),
                }
            if "robot_avatar" in body:
                agent.robot_avatar = body["robot_avatar"]
            agent.profile = AgentProfile(
                agent_class=body.get("agent_class", agent.profile.agent_class),
                memory_mode=body.get("memory_mode", agent.profile.memory_mode),
                rag_mode=body.get("rag_mode", agent.profile.rag_mode),
                rag_provider_id=body.get("rag_provider_id", agent.profile.rag_provider_id),
                rag_collection_ids=body.get("rag_collection_ids", agent.profile.rag_collection_ids),
                personality=body.get("personality", agent.profile.personality),
                communication_style=body.get("communication_style", agent.profile.communication_style),
                expertise=body.get("expertise", agent.profile.expertise),
                skills=body.get("skills", agent.profile.skills),
                language=body.get("language", agent.profile.language),
                max_context_messages=body.get("max_context_messages", agent.profile.max_context_messages),
                allowed_tools=body.get("allowed_tools", agent.profile.allowed_tools),
                denied_tools=body.get("denied_tools", agent.profile.denied_tools),
                auto_approve_tools=body.get("auto_approve_tools", agent.profile.auto_approve_tools),
                temperature=body.get("temperature", agent.profile.temperature),
                custom_instructions=body.get("custom_instructions", agent.profile.custom_instructions),
                exec_policy=body.get("exec_policy", agent.profile.exec_policy),
                exec_blacklist=body.get("exec_blacklist", agent.profile.exec_blacklist),
                exec_whitelist=body.get("exec_whitelist", agent.profile.exec_whitelist),
                # RolePresetV2 — 由前端传入或保留旧值（避免被 profile 重建时清空）
                role_preset_id=body.get("role_preset_id", getattr(agent.profile, "role_preset_id", "")),
                role_preset_version=body.get("role_preset_version", getattr(agent.profile, "role_preset_version", 1)),
                llm_tier=body.get("llm_tier", getattr(agent.profile, "llm_tier", "")),
                sop_template_id=body.get("sop_template_id", getattr(agent.profile, "sop_template_id", "")),
                quality_rules=body.get("quality_rules", getattr(agent.profile, "quality_rules", [])),
                output_contract=body.get("output_contract", getattr(agent.profile, "output_contract", {})),
                input_contract=body.get("input_contract", getattr(agent.profile, "input_contract", {})),
                kpi_definitions=body.get("kpi_definitions", getattr(agent.profile, "kpi_definitions", [])),
            )
            hub._save_agents()
            auth.audit("update_agent_profile", actor=actor_name, role=user_role,
                       target=agent_id, ip=get_client_ip(handler))
            handler._json({"ok": True})
        except Exception as e:
            traceback.print_exc()
            handler._json({"error": f"Failed to update profile: {e}"}, 500)
    else:
        handler._json({"error": "Agent not found"}, 404)
    return True


def _handle_enhancement(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    aid = re.match(r"^/api/portal/agent/([^/]+)/enhancement$", path).group(1)
    agent = hub.get_agent(aid)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    action = body.get("action", "enable")
    if action == "enable":
        # Accept either single domain or list of up to 8 domains
        domains = body.get("domains")
        if domains is None:
            domains = body.get("domain", "general")
        if isinstance(domains, list):
            domains = domains[:8]
        stats = agent.enable_enhancement(domains)
        hub._save_agents()
        label = "+".join(domains) if isinstance(domains, list) else str(domains)
        auth.audit("enable_enhancement", actor=actor_name, role=user_role,
                   target=f"{aid}/{label}", ip=get_client_ip(handler))
        handler._json({"ok": True, "stats": stats})
    elif action == "disable":
        agent.disable_enhancement()
        hub._save_agents()
        auth.audit("disable_enhancement", actor=actor_name, role=user_role,
                   target=aid, ip=get_client_ip(handler))
        handler._json({"ok": True})
    elif action == "add_knowledge":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        entry = agent.enhancer.knowledge.add(
            title=body.get("title", ""),
            content=body.get("content", ""),
            category=body.get("category", "general"),
            tags=body.get("tags", []),
            priority=body.get("priority", 0),
            source=actor_name,
        )
        hub._save_agents()
        handler._json(entry.to_dict())
    elif action == "remove_knowledge":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        ok = agent.enhancer.knowledge.remove(body.get("entry_id", ""))
        hub._save_agents()
        handler._json({"ok": ok})
    elif action == "add_reasoning_pattern":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        pattern = agent.enhancer.reasoning.add_pattern(
            name=body.get("name", ""),
            description=body.get("description", ""),
            trigger_keywords=body.get("trigger_keywords", []),
            steps=body.get("steps", []),
            reflection_prompt=body.get("reflection_prompt", ""),
        )
        hub._save_agents()
        handler._json(pattern.to_dict())
    elif action == "add_memory":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        node = agent.enhancer.memory.add(
            title=body.get("title", ""),
            content=body.get("content", ""),
            kind=body.get("kind", "observation"),
            tags=body.get("tags", []),
            importance=body.get("importance", 0.5),
        )
        hub._save_agents()
        handler._json(node.to_dict())
    elif action == "feedback":
        # Learn from user feedback
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        node = agent.enhancer.learn_from_interaction(
            user_message=body.get("user_message", ""),
            agent_response=body.get("agent_response", ""),
            outcome=body.get("outcome", "success"),
            feedback=body.get("feedback", ""),
        )
        hub._save_agents()
        handler._json({"ok": True, "learned": node.to_dict() if node else None})
    elif action == "remove_reasoning_pattern":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        pid = body.get("pattern_id", "")
        ok = pid in agent.enhancer.reasoning.patterns and agent.enhancer.reasoning.patterns.pop(pid, None) is not None
        hub._save_agents()
        handler._json({"ok": ok})
    elif action == "remove_memory":
        if not agent.enhancer:
            handler._json({"error": "Enhancement not enabled"}, 400)
            return True
        nid = body.get("node_id", "")
        ok = nid in agent.enhancer.memory.nodes and agent.enhancer.memory.nodes.pop(nid, None) is not None
        hub._save_agents()
        handler._json({"ok": ok})
    else:
        handler._json({"error": f"Unknown action: {action}"}, 400)
    return True


def _handle_tasks(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        # Try proxy to remote node
        data = hub.proxy_remote_agent_post(agent_id, "/tasks", body)
        if data:
            handler._json(data)
        else:
            handler._json({"error": "Agent not found (local or remote)"}, 404)
        return True
    action = body.get("action", "create")
    if action == "create":
        task = agent.add_task(
            title=body.get("title", ""),
            description=body.get("description", ""),
            priority=body.get("priority", 0),
            parent_id=body.get("parent_id", ""),
            assigned_by=actor_name,
            source=body.get("source", "admin"),
            source_agent_id=body.get("source_agent_id", ""),
            deadline=body.get("deadline", 0.0),
            tags=body.get("tags", []),
        )
        logger.info("TASK API create: agent=%s task=%s source=%s deadline=%s",
                    agent_id, task.id, task.source, task.deadline_str or "none")
        handler._json(task.to_dict())
    elif action == "update":
        task_id = body.get("task_id", "")
        updates = {}
        for k in ("title", "description", "status", "priority", "result", "tags", "deadline"):
            if k in body:
                updates[k] = body[k]
        task = agent.update_task(task_id, **updates)
        if task:
            handler._json(task.to_dict())
        else:
            handler._json({"error": "Task not found"}, 404)
    elif action == "delete":
        ok = agent.remove_task(body.get("task_id", ""))
        handler._json({"ok": ok})
    else:
        handler._json({"error": f"Unknown action: {action}"}, 400)
    return True


def _handle_prompt_packs(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    action = body.get("action", "")
    from ...core.prompt_enhancer import get_prompt_pack_registry, PromptPack
    registry = get_prompt_pack_registry()
    if action == "bind":
        skill_id = body.get("skill_id", "")
        if skill_id and skill_id not in agent.bound_prompt_packs:
            agent.bound_prompt_packs.append(skill_id)
            hub._save_agents()
        handler._json({"ok": True, "bound_prompt_packs": agent.bound_prompt_packs})
    elif action == "unbind":
        skill_id = body.get("skill_id", "")
        if skill_id in agent.bound_prompt_packs:
            agent.bound_prompt_packs.remove(skill_id)
            hub._save_agents()
        handler._json({"ok": True, "bound_prompt_packs": agent.bound_prompt_packs})
    elif action == "discover":
        scan_dirs = body.get("scan_dirs", [])
        # Auto-add agent working dir skill paths
        if agent.working_dir:
            for sub in [".claw/skills", ".claude/skills", "skills"]:
                d = os.path.join(agent.working_dir, sub)
                if os.path.isdir(d) and d not in scan_dirs:
                    scan_dirs.append(d)
        # Also add global default paths
        home = os.path.expanduser("~")
        for d in [os.path.join(home, ".tudou_claw", "skills"),
                  os.path.join(os.getcwd(), "skills"),
                  os.path.join(os.getcwd(), ".claw", "skills")]:
            if os.path.isdir(d) and d not in scan_dirs:
                scan_dirs.append(d)
        new_count = registry.discover(scan_dirs if scan_dirs else None)
        handler._json({"ok": True, "new_skills": new_count,
                    "total": len(registry.store.get_active()),
                    "scan_dirs": registry.store._scan_dirs})
    elif action == "import_from_catalog":
        skill_ids = body.get("skill_ids", [])
        catalog_path = Path(__file__).resolve().parent.parent.parent / "data" / "community_skills.json"
        try:
            with open(catalog_path, 'r', encoding='utf-8') as f:
                catalog = json.load(f)
            imported_count = 0
            for skill_id in skill_ids:
                skill_entry = None
                for skill in catalog.get("skills", []):
                    if skill.get("id") == skill_id:
                        skill_entry = skill
                        break
                if skill_entry:
                    record = PromptPack(
                        skill_id=skill_entry.get("id", ""),
                        name=skill_entry.get("name", ""),
                        description=skill_entry.get("description", ""),
                        category=skill_entry.get("category", "general"),
                        tags=skill_entry.get("tags", []),
                        content="",
                        origin="catalog"
                    )
                    registry.store.add_skill(record)
                    imported_count += 1
                    if skill_id not in agent.bound_prompt_packs:
                        agent.bound_prompt_packs.append(skill_id)
            hub._save_agents()
            handler._json({"ok": True, "imported": imported_count,
                        "bound_prompt_packs": agent.bound_prompt_packs})
        except Exception as e:
            handler._json({"error": str(e)}, 500)
    elif action == "import_local":
        local_path = body.get("path", "")
        if not os.path.isdir(local_path):
            handler._json({"error": "Invalid path or directory not found"}, 400)
            return True
        if local_path not in registry.store._scan_dirs:
            registry.store._scan_dirs.append(local_path)
        new_count = registry.discover([local_path])
        handler._json({"ok": True, "new_skills": new_count, "scan_path": local_path})
    else:
        handler._json({"error": f"Unknown action: {action}"}, 400)
    return True


def _handle_growth(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    action = body.get("action", "")
    if action == "init":
        gp = agent.ensure_growth_path()
        hub._save_agents()
        handler._json({"ok": True,
                    "growth_path": gp.to_dict() if gp else None})
    elif action == "complete_objective":
        objective_id = body.get("objective_id", "")
        gp = agent.ensure_growth_path()
        if gp and objective_id:
            ok = gp.mark_objective_completed(objective_id)
            advanced = gp.try_advance()
            hub._save_agents()
            handler._json({"ok": ok, "advanced": advanced,
                        "summary": gp.get_summary()})
        else:
            handler._json({"error": "No growth path or missing objective_id"}, 400)
    elif action == "trigger_learning":
        gp = agent.ensure_growth_path()
        if gp:
            obj = gp.get_next_objectives(limit=1)
            if obj:
                from ...core.role_growth_path import build_learning_task_prompt
                prompt = build_learning_task_prompt(obj[0], gp.role_name)
                handler._json({"ok": True,
                            "objective": obj[0].to_dict(),
                            "learning_prompt": prompt})
            else:
                handler._json({"ok": False,
                            "message": "All objectives in current stage completed"})
        else:
            handler._json({"error": "No growth path for this role"}, 400)
    elif action == "advance":
        gp = agent.ensure_growth_path()
        if gp:
            advanced = gp.try_advance()
            hub._save_agents()
            handler._json({"ok": advanced, "summary": gp.get_summary()})
        else:
            handler._json({"error": "No growth path"}, 400)
    else:
        handler._json({"error": f"Unknown action: {action}"}, 400)
    return True


def _handle_persona(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    persona_id = body.get("persona_id", "")
    if not persona_id:
        handler._json({"error": "persona_id required"}, 400)
    else:
        ok = hub.apply_persona(agent_id, persona_id)
        if ok:
            auth.audit("apply_persona", actor=actor_name,
                       role=user_role, target=agent_id,
                       detail=persona_id, ip=get_client_ip(handler))
            handler._json({"ok": True})
        else:
            handler._json({"error": "Agent or persona not found"}, 404)
    return True


def _handle_soul(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    soul_md = body.get("soul_md", "")
    robot_avatar = body.get("robot_avatar", "")
    if soul_md is not None:
        agent.soul_md = soul_md
        # Also update the system_prompt from SOUL.md content
        agent.system_prompt = soul_md
    if robot_avatar is not None:
        agent.robot_avatar = robot_avatar
    # Rebuild system prompt immediately
    if agent.messages and agent.messages[0].get("role") == "system":
        agent.messages[0]["content"] = agent._build_system_prompt()
    hub._save_agents()
    handler._json({"ok": True, "agent_id": agent_id})
    return True


def _handle_thinking_enable(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    config = body or {}
    stats = agent.enable_active_thinking(**config)
    hub._save_agents()
    handler._json({"ok": True, "stats": stats})
    return True


def _handle_thinking_disable(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    agent.disable_active_thinking()
    hub._save_agents()
    handler._json({"ok": True})
    return True


def _handle_thinking_trigger(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    trigger = body.get("trigger", "manual")
    context = body.get("context", "")
    result = agent.trigger_thinking(trigger=trigger, context=context)
    hub._save_agents()
    handler._json({"ok": True, "result": result})
    return True


def _handle_thinking_history(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    history = []
    if agent.active_thinking:
        history = [r.to_dict() for r in agent.active_thinking.history[-20:]]
    stats = agent.active_thinking.get_stats() if agent.active_thinking else None
    handler._json({"history": history, "stats": stats})
    return True


def _handle_workspace_authorize(handler, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = body.get("agent_id", "")
    target_agent_id = body.get("target_agent_id", "")  # whose workspace to authorize
    if not agent_id or not target_agent_id:
        handler._json({"error": "agent_id and target_agent_id required"}, 400)
        return True
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": f"Agent {agent_id} not found"}, 404)
        return True
    if target_agent_id not in agent.authorized_workspaces:
        agent.authorized_workspaces.append(target_agent_id)
        hub._save_agents()
        auth.audit("authorize_workspace", actor=actor_name,
                   role=user_role, target=agent_id,
                   detail=f"authorized:{target_agent_id}",
                   ip=get_client_ip(handler))
    handler._json({"ok": True, "authorized_workspaces": agent.authorized_workspaces})
    return True


def _handle_workspace_revoke(handler, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = body.get("agent_id", "")
    target_agent_id = body.get("target_agent_id", "")
    if not agent_id or not target_agent_id:
        handler._json({"error": "agent_id and target_agent_id required"}, 400)
        return True
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": f"Agent {agent_id} not found"}, 404)
        return True
    if target_agent_id in agent.authorized_workspaces:
        agent.authorized_workspaces.remove(target_agent_id)
        hub._save_agents()
        auth.audit("revoke_workspace", actor=actor_name,
                   role=user_role, target=agent_id,
                   detail=f"revoked:{target_agent_id}",
                   ip=get_client_ip(handler))
    handler._json({"ok": True, "authorized_workspaces": agent.authorized_workspaces})
    return True


def _handle_workspace_list(handler, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = body.get("agent_id", "")
    if not agent_id:
        handler._json({"error": "agent_id required"}, 400)
        return True
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": f"Agent {agent_id} not found"}, 404)
        return True
    handler._json({
        "agent_id": agent_id,
        "own_workspace": agent.working_dir,
        "shared_workspace": agent.shared_workspace,
        "authorized_workspaces": agent.authorized_workspaces,
    })
    return True


def _handle_self_improvement_enable(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    import_exp = body.get("import_experience", True)
    import_limit = body.get("import_limit", 50)
    result = agent.enable_self_improvement(
        import_experience=import_exp, import_limit=import_limit)
    hub._save_agents()
    handler._json({"ok": True, **result})
    return True


def _handle_self_improvement_disable(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    agent_id = path.split("/")[4]
    agent = hub.get_agent(agent_id)
    if not agent:
        handler._json({"error": "Agent not found"}, 404)
        return True
    agent.disable_self_improvement()
    hub._save_agents()
    handler._json({"ok": True})
    return True
