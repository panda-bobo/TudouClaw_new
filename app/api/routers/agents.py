"""Agent management router — CRUD, model, profile, enhancement, thinking."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.agents")

router = APIRouter(prefix="/api/portal", tags=["agents"])


def _get_agent_or_404(hub, agent_id: str):
    agent = hub.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


# ---------------------------------------------------------------------------
# Agent listing
# ---------------------------------------------------------------------------

@router.get("/agents")
async def list_agents(
    include_subagents: bool = Query(False),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all agents visible to current user."""
    agents_raw = hub.list_agents() if hasattr(hub, "list_agents") else []
    agents_list = [a.to_dict() if hasattr(a, "to_dict") else a for a in agents_raw]

    if not include_subagents:
        agents_list = [a for a in agents_list if not a.get("parent_id")]

    # Attach local node_id
    for a in agents_list:
        if a.get("location") == "local" and not a.get("node_id"):
            a["node_id"] = hub.node_id

    return {"agents": agents_list}


# ---------------------------------------------------------------------------
# Single agent endpoints
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/events")
async def get_agent_events(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    return {"events": [e.to_dict() for e in agent.events[-500:]]}


@router.get("/agent/{agent_id}/tasks")
async def get_agent_tasks(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    tasks = agent.tasks if hasattr(agent, "tasks") else []
    return {"tasks": [t.to_dict() if hasattr(t, "to_dict") else t for t in tasks]}


@router.post("/agent/{agent_id}/tasks")
async def manage_agent_tasks(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Agent task CRUD — matches legacy handlers/agents.py _handle_tasks."""
    agent = hub.get_agent(agent_id)
    if not agent:
        # Try proxy to remote node
        data = hub.proxy_remote_agent_post(agent_id, "/tasks", body) if hasattr(hub, "proxy_remote_agent_post") else None
        if data:
            return data
        raise HTTPException(status_code=404, detail="Agent not found (local or remote)")

    action = body.get("action", "create")
    actor_name = getattr(user, "username", "") or getattr(user, "user_id", "")

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
        return task.to_dict()
    elif action == "update":
        task_id = body.get("task_id", "")
        updates = {}
        for k in ("title", "description", "status", "priority", "result", "tags", "deadline"):
            if k in body:
                updates[k] = body[k]
        task = agent.update_task(task_id, **updates)
        if task:
            return task.to_dict()
        raise HTTPException(404, "Task not found")
    elif action == "delete":
        ok = agent.remove_task(body.get("task_id", ""))
        return {"ok": ok}
    else:
        raise HTTPException(400, f"Unknown action: {action}")


@router.get("/agent/{agent_id}/runtime-stats")
async def get_runtime_stats(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    stats = agent.get_runtime_stats() if hasattr(agent, "get_runtime_stats") else {}
    return stats


@router.get("/agent/{agent_id}/cost")
async def get_agent_cost(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    cost = agent.get_cost_analytics() if hasattr(agent, "get_cost_analytics") else {}
    return cost


@router.get("/agent/{agent_id}/history")
async def get_agent_history(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    md = hub.get_agent_history(agent_id)
    return {"markdown": md}


@router.get("/agent/{agent_id}/growth")
async def get_agent_growth(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    growth = agent.get_growth_metrics() if hasattr(agent, "get_growth_metrics") else {}
    return growth


@router.post("/agent/{agent_id}/growth")
async def manage_agent_growth(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Manage agent growth path — matches legacy handlers/agents.py _handle_growth."""
    agent = _get_agent_or_404(hub, agent_id)
    action = body.get("action", "")

    if action == "init":
        gp = agent.ensure_growth_path()
        hub._save_agents()
        return {"ok": True, "growth_path": gp.to_dict() if gp else None}
    elif action == "complete_objective":
        objective_id = body.get("objective_id", "")
        gp = agent.ensure_growth_path()
        if gp and objective_id:
            ok = gp.mark_objective_completed(objective_id)
            advanced = gp.try_advance()
            hub._save_agents()
            return {"ok": ok, "advanced": advanced, "summary": gp.get_summary()}
        raise HTTPException(400, "No growth path or missing objective_id")
    elif action == "trigger_learning":
        gp = agent.ensure_growth_path()
        if gp:
            obj = gp.get_next_objectives(limit=1)
            if obj:
                from ...core.role_growth_path import build_learning_task_prompt
                prompt = build_learning_task_prompt(obj[0], gp.role_name)
                return {"ok": True, "objective": obj[0].to_dict(), "learning_prompt": prompt}
            return {"ok": False, "message": "All objectives in current stage completed"}
        raise HTTPException(400, "No growth path for this role")
    elif action == "advance":
        gp = agent.ensure_growth_path()
        if gp:
            advanced = gp.try_advance()
            hub._save_agents()
            return {"ok": advanced, "summary": gp.get_summary()}
        raise HTTPException(400, "No growth path")
    else:
        raise HTTPException(400, f"Unknown action: {action}")


@router.get("/agent/{agent_id}/soul")
async def get_agent_soul(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    soul = agent.get_soul() if hasattr(agent, "get_soul") else ""
    robot_avatar = getattr(agent, "robot_avatar", "")
    return {"soul": soul, "robot_avatar": robot_avatar}


@router.post("/agent/{agent_id}/soul")
async def update_agent_soul(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update agent soul/system prompt — matches legacy handlers/agents.py _handle_soul."""
    agent = _get_agent_or_404(hub, agent_id)
    soul_md = body.get("soul_md", "")
    robot_avatar = body.get("robot_avatar", "")
    if soul_md is not None:
        agent.soul_md = soul_md
        agent.system_prompt = soul_md
    if robot_avatar is not None:
        agent.robot_avatar = robot_avatar
    # Rebuild system prompt immediately
    if agent.messages and agent.messages[0].get("role") == "system":
        agent.messages[0]["content"] = agent._build_system_prompt()
    hub._save_agents()
    return {"ok": True, "agent_id": agent_id}


@router.get("/supervisor/status")
async def supervisor_status(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Return process isolation supervisor status (worker health, uptime)."""
    result = hub.supervisor.get_status()
    # Phase 2: include UID manager and shared file router status
    try:
        from ...isolation.uid_manager import get_uid_manager
        result["uid_manager"] = get_uid_manager(hub._data_dir).get_status()
    except Exception:
        result["uid_manager"] = {"error": "not initialized"}
    try:
        from ...isolation.shared_file_router import get_shared_file_router
        result["shared_file_router"] = get_shared_file_router(hub._data_dir).get_status()
    except Exception:
        result["shared_file_router"] = {"error": "not initialized"}
    return result


@router.get("/supervisor/audit")
async def supervisor_audit(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
    agent_id: str = "",
    limit: int = 50,
):
    """Return shared file operation audit trail."""
    try:
        from ...isolation.shared_file_router import get_shared_file_router
        router = get_shared_file_router(hub._data_dir)
        return {"audit": router.get_audit(last_n=limit, agent_id=agent_id)}
    except Exception as e:
        return {"audit": [], "error": str(e)}


@router.get("/departments")
async def list_departments(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Return the default department list plus any custom departments
    that currently have agents assigned to them, with agent counts."""
    try:
        from ...agent import DEFAULT_DEPARTMENTS
    except Exception:
        DEFAULT_DEPARTMENTS = []
    counts: dict[str, int] = {}
    unassigned = 0
    try:
        for a in hub.agents.values():
            dep = (getattr(a, "department", "") or "").strip()
            if not dep:
                unassigned += 1
                continue
            counts[dep] = counts.get(dep, 0) + 1
    except Exception:
        pass
    # Merge: defaults first (even with count 0), then any custom ones
    departments = []
    seen: set = set()
    for name in DEFAULT_DEPARTMENTS:
        departments.append({"name": name, "count": counts.get(name, 0), "is_default": True})
        seen.add(name)
    for name, cnt in counts.items():
        if name in seen:
            continue
        departments.append({"name": name, "count": cnt, "is_default": False})
    return {
        "departments": departments,
        "defaults": list(DEFAULT_DEPARTMENTS),
        "unassigned": unassigned,
    }


@router.post("/agent/{agent_id}/department")
async def update_agent_department(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Set an agent's department affiliation. Empty string = unassigned."""
    agent = hub.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    new_dep = (body.get("department") or "").strip()
    agent.department = new_dep
    try:
        hub._save_agents()
    except Exception:
        pass
    return {"ok": True, "department": agent.department}


@router.post("/agent/{agent_id}/model")
async def update_agent_model(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = hub.get_agent(agent_id)
    if agent:
        agent.provider = body.get("provider", "")
        agent.model = body.get("model", "")
        hub._save_agents()
        return {"ok": True, "provider": agent.provider, "model": agent.model}
    else:
        # Try remote agent
        node = hub.find_agent_node(agent_id) if hasattr(hub, "find_agent_node") else None
        if node:
            hub.proxy_update_model(agent_id, node, body.get("provider", ""), body.get("model", ""))
            return {"ok": True}
        raise HTTPException(status_code=404, detail="Agent not found")


@router.post("/agent/{agent_id}/profile")
async def update_agent_profile(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update agent profile — matches legacy handlers/agents.py _handle_profile."""
    agent = _get_agent_or_404(hub, agent_id)
    try:
        from ...agent import AgentProfile
        # Update core fields if provided
        if "name" in body and body["name"].strip():
            agent.name = body["name"].strip()
        if "role" in body:
            agent.role = body["role"]
        if "working_dir" in body:
            _raw_wd = (body.get("working_dir") or "").strip()
            if not _raw_wd:
                try:
                    _ws = agent._ensure_workspace_layout()
                    agent.working_dir = str(_ws)
                except Exception:
                    agent.working_dir = ""
            else:
                _p = os.path.expanduser(_raw_wd)
                if not os.path.isabs(_p):
                    try:
                        _base = agent._ensure_workspace_layout()
                    except Exception:
                        _base = None
                    if _base is None:
                        raise HTTPException(400, "cannot resolve relative working_dir: default workspace unavailable")
                    from pathlib import Path as _Path
                    _resolved = (_Path(str(_base)) / _p).resolve()
                    try:
                        _resolved.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                    agent.working_dir = str(_resolved)
                else:
                    agent.working_dir = os.path.abspath(_p)
        if "provider" in body:
            agent.provider = body["provider"]
        if "model" in body:
            agent.model = body["model"]
        if "learning_provider" in body:
            agent.learning_provider = str(body.get("learning_provider") or "")
        if "learning_model" in body:
            agent.learning_model = str(body.get("learning_model") or "")
        if "multimodal_provider" in body:
            agent.multimodal_provider = str(body.get("multimodal_provider") or "")
        if "multimodal_model" in body:
            agent.multimodal_model = str(body.get("multimodal_model") or "")
        if "multimodal_supports_tools" in body:
            agent.multimodal_supports_tools = bool(body.get("multimodal_supports_tools"))
        if "coding_provider" in body:
            agent.coding_provider = str(body.get("coding_provider") or "")
        if "coding_model" in body:
            agent.coding_model = str(body.get("coding_model") or "")
        if "extra_llms" in body:
            raw_slots = body.get("extra_llms") or []
            if not isinstance(raw_slots, list):
                raw_slots = []
            cleaned = []
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
        )
        hub._save_agents()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {e}")


@router.post("/agent/{agent_id}/chat")
async def send_chat(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Submit a chat message — returns task_id for SSE streaming."""
    import base64 as _b64
    import os as _os
    import time as _time

    agent = _get_agent_or_404(hub, agent_id)
    user_msg = body.get("message", "").strip()
    attachments = body.get("attachments") or []

    # ── Handle attachments: build multimodal content + save files ──
    _MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB
    saved_refs: list[str] = []
    multimodal_parts: list[dict] = []
    if isinstance(attachments, list) and attachments:
        for att in attachments[:10]:
            if not isinstance(att, dict):
                continue
            raw_name = str(att.get("name") or "attachment.bin")
            safe_name = "".join(
                c for c in raw_name if c.isalnum() or c in "._-"
            ) or "attachment.bin"
            data_b64 = att.get("data_base64") or ""
            mime_type = str(att.get("mime") or "application/octet-stream")
            if not data_b64:
                continue
            try:
                data_bytes = _b64.b64decode(data_b64)
            except Exception:
                continue
            if len(data_bytes) > _MAX_UPLOAD:
                continue
            # Build multimodal content
            if mime_type.startswith("image/"):
                multimodal_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{data_b64}"},
                })
                # Save image to disk (best-effort)
                try:
                    base_dir = agent.working_dir or str(agent._effective_working_dir())
                    att_dir = _os.path.join(base_dir, "attachments")
                    _os.makedirs(att_dir, exist_ok=True)
                    fname = f"{int(_time.time() * 1000)}_{safe_name}"
                    fpath = _os.path.join(att_dir, fname)
                    with open(fpath, "wb") as _f:
                        _f.write(data_bytes)
                    saved_refs.append(fname)
                except Exception:
                    pass
            else:
                # Save to disk first so we can extract text
                fpath = None
                try:
                    base_dir = agent.working_dir or str(agent._effective_working_dir())
                    att_dir = _os.path.join(base_dir, "attachments")
                    _os.makedirs(att_dir, exist_ok=True)
                    fname = f"{int(_time.time() * 1000)}_{safe_name}"
                    fpath = _os.path.join(att_dir, fname)
                    with open(fpath, "wb") as _f:
                        _f.write(data_bytes)
                    saved_refs.append(fname)
                except Exception:
                    pass
                # Try to extract text content for the LLM
                extracted = ""
                if fpath:
                    try:
                        from app.utils.file_parser import extract_file_text
                        extracted = extract_file_text(fpath, mime_type)
                    except Exception:
                        pass
                if extracted:
                    multimodal_parts.append({
                        "type": "text",
                        "text": f"[File: {safe_name}]\n{extracted}",
                    })
                else:
                    multimodal_parts.append({
                        "type": "text",
                        "text": f"[Attached file: {safe_name} ({mime_type})]",
                    })
                continue

    if not user_msg and not saved_refs and not multimodal_parts:
        raise HTTPException(400, "Empty message")

    # Build chat content: multimodal list or plain text
    if multimodal_parts:
        content_parts: list[dict] = []
        if user_msg:
            content_parts.append({"type": "text", "text": user_msg})
        elif saved_refs:
            content_parts.append({"type": "text", "text": "请查看以下附件:"})
        content_parts.extend(multimodal_parts)
        chat_content = content_parts  # list = multimodal
    else:
        chat_content = user_msg
        if saved_refs:
            suffix = "\n" + " ".join(f"📎{r}" for r in saved_refs)
            chat_content = (chat_content + suffix) if chat_content else suffix.lstrip()

    # Route through supervisor (handles both isolated and in-process)
    task = hub.supervisor.chat_async(agent.id, chat_content, source="admin")
    return {
        "task_id": task.id,
        "status": task.status.value,
        "attachments_saved": saved_refs,
    }


@router.post("/agent/{agent_id}/wake")
async def wake_agent(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Wake agent — scan projects for assigned tasks and execute them."""
    max_tasks = int(body.get("max_tasks", 5) or 5)
    result = hub.wake_up_agent(agent_id, max_tasks=max_tasks)
    return result


@router.post("/agent/{agent_id}/clear")
async def clear_agent(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    if hasattr(agent, "clear"):
        agent.clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Enhancement & Thinking
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/enhancement")
async def manage_enhancement(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Manage agent enhancement — matches legacy handlers/agents.py _handle_enhancement."""
    agent = _get_agent_or_404(hub, agent_id)
    action = body.get("action", "enable")

    if action == "enable":
        domains = body.get("domains")
        if domains is None:
            domains = body.get("domain", "general")
        if isinstance(domains, list):
            domains = domains[:8]
        stats = agent.enable_enhancement(domains)
        hub._save_agents()
        return {"ok": True, "stats": stats}
    elif action == "disable":
        agent.disable_enhancement()
        hub._save_agents()
        return {"ok": True}
    elif action == "add_knowledge":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        entry = agent.enhancer.knowledge.add(
            title=body.get("title", ""),
            content=body.get("content", ""),
            category=body.get("category", "general"),
            tags=body.get("tags", []),
            priority=body.get("priority", 0),
            source=getattr(user, "username", "") or getattr(user, "user_id", ""),
        )
        hub._save_agents()
        return entry.to_dict()
    elif action == "remove_knowledge":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        ok = agent.enhancer.knowledge.remove(body.get("entry_id", ""))
        hub._save_agents()
        return {"ok": ok}
    elif action == "add_reasoning_pattern":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        pattern = agent.enhancer.reasoning.add_pattern(
            name=body.get("name", ""),
            description=body.get("description", ""),
            trigger_keywords=body.get("trigger_keywords", []),
            steps=body.get("steps", []),
            reflection_prompt=body.get("reflection_prompt", ""),
        )
        hub._save_agents()
        return pattern.to_dict()
    elif action == "add_memory":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        node = agent.enhancer.memory.add(
            title=body.get("title", ""),
            content=body.get("content", ""),
            kind=body.get("kind", "observation"),
            tags=body.get("tags", []),
            importance=body.get("importance", 0.5),
        )
        hub._save_agents()
        return node.to_dict()
    elif action == "feedback":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        node = agent.enhancer.learn_from_interaction(
            user_message=body.get("user_message", ""),
            agent_response=body.get("agent_response", ""),
            outcome=body.get("outcome", "success"),
            feedback=body.get("feedback", ""),
        )
        hub._save_agents()
        return {"ok": True, "learned": node.to_dict() if node else None}
    elif action == "remove_reasoning_pattern":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        pid = body.get("pattern_id", "")
        ok = pid in agent.enhancer.reasoning.patterns and agent.enhancer.reasoning.patterns.pop(pid, None) is not None
        hub._save_agents()
        return {"ok": ok}
    elif action == "remove_memory":
        if not agent.enhancer:
            raise HTTPException(400, "Enhancement not enabled")
        nid = body.get("node_id", "")
        ok = nid in agent.enhancer.memory.nodes and agent.enhancer.memory.nodes.pop(nid, None) is not None
        hub._save_agents()
        return {"ok": ok}
    else:
        raise HTTPException(400, f"Unknown action: {action}")


@router.get("/agent/{agent_id}/thinking")
async def get_thinking(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    agent = _get_agent_or_404(hub, agent_id)
    stats = agent.active_thinking.get_stats() if agent.active_thinking else {"enabled": False}
    history = []
    if agent.active_thinking:
        history = [r.to_dict() for r in agent.active_thinking.history[-10:]]
    return {"stats": stats, "history": history}


@router.post("/agent/{agent_id}/thinking/enable")
async def enable_thinking(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Enable active thinking — matches legacy handlers/agents.py."""
    agent = _get_agent_or_404(hub, agent_id)
    config = body or {}
    stats = agent.enable_active_thinking(**config)
    hub._save_agents()
    return {"ok": True, "stats": stats}


@router.post("/agent/{agent_id}/thinking/disable")
async def disable_thinking(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Disable active thinking — matches legacy handlers/agents.py."""
    agent = _get_agent_or_404(hub, agent_id)
    agent.disable_active_thinking()
    hub._save_agents()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Agent creation & deletion
# ---------------------------------------------------------------------------

@router.post("/agent/create")
async def create_agent(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new agent."""
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
            department=body.get("department", "") or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Apply profile if provided
    if agent and body.get("profile"):
        from ...agent import AgentProfile
        prof = body["profile"]
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
        hub._save_agents()

    if agent and body.get("robot_avatar"):
        agent.robot_avatar = body["robot_avatar"]
        hub._save_agents()

    if agent and body.get("persona_id"):
        try:
            from ...persona import apply_persona_to_agent
            apply_persona_to_agent(agent, body["persona_id"])
            hub._save_agents()
        except Exception as e:
            logger.warning("persona apply failed: %s", e)

    return agent.to_dict() if agent else {}


@router.delete("/agent/{agent_id}")
async def delete_agent(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete an agent permanently."""
    ok = hub.remove_agent(agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# File & session persistence
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/save-file")
async def save_file(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Save a file to the agent's working directory."""
    agent = _get_agent_or_404(hub, agent_id)
    filename = (body.get("filename", "") or "").strip()
    content = body.get("content", "")
    if not filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    filename = filename.replace("\\", "/")
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    base_dir = agent.working_dir or str(agent._effective_working_dir())
    file_path = os.path.join(base_dir, filename)
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": file_path, "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")


@router.post("/agent/{agent_id}/save-session")
async def save_session(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Save agent session to disk."""
    saved = hub.save_agent_session(agent_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Agent not found or save failed")
    return {"ok": True, "path": saved}


@router.post("/agent/{agent_id}/load-session")
async def load_session(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Load agent session from disk."""
    ok = hub.load_agent_session(agent_id)
    return {"ok": ok}


@router.post("/agent/{agent_id}/save-engine")
async def save_engine(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Save memory engine session."""
    saved = hub.save_engine_session(agent_id)
    return {"ok": bool(saved), "path": saved}


@router.post("/agent/{agent_id}/restore-engine")
async def restore_engine(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Restore memory engine session."""
    ok = hub.restore_engine_session(agent_id)
    return {"ok": ok}


@router.post("/agent/{agent_id}/compact-memory")
async def compact_memory(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Compact agent memory."""
    ok = hub.compact_agent_memory(agent_id)
    return {"ok": ok}


# ---------------------------------------------------------------------------
# SRC integration
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/exec-src-tool")
async def exec_src_tool(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Execute a SRC tool on behalf of an agent."""
    _get_agent_or_404(hub, agent_id)
    tool_name = body.get("tool", "")
    payload = body.get("payload", "")
    try:
        result = hub.execute_src_tool(agent_id, tool_name, payload)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/{agent_id}/exec-src-command")
async def exec_src_command(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Execute a SRC command on behalf of an agent."""
    _get_agent_or_404(hub, agent_id)
    cmd_name = body.get("command", "")
    prompt = body.get("prompt", "")
    try:
        result = hub.execute_src_command(agent_id, cmd_name, prompt)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Learning model & growth tasks
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/learning-model")
async def set_learning_model(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Set the learning model (cheap/local LLM for self-growth tasks)."""
    agent = _get_agent_or_404(hub, agent_id)
    agent.learning_provider = (body.get("provider", "") or "").strip()
    agent.learning_model = (body.get("model", "") or "").strip()
    hub._save_agents()
    return {
        "ok": True,
        "learning_provider": agent.learning_provider,
        "learning_model": agent.learning_model,
    }


@router.post("/agent/{agent_id}/growth-task")
async def add_growth_task(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Enqueue a growth task for the agent."""
    agent = _get_agent_or_404(hub, agent_id)
    try:
        task = agent.enqueue_growth_task(
            learning_goal=(body.get("learning_goal", "") or "").strip(),
            knowledge_gap=(body.get("knowledge_gap", "") or "").strip(),
            title=(body.get("title", "") or "").strip(),
        )
        hub._save_agents()
        return {"ok": True, "task": task.to_dict()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Prompt packs
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/prompt-packs")
async def manage_prompt_packs(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Manage prompt packs: bind, unbind, discover, import."""
    agent = _get_agent_or_404(hub, agent_id)
    action = body.get("action", "")

    from ...core.prompt_enhancer import get_prompt_pack_registry
    registry = get_prompt_pack_registry()

    if action == "bind":
        skill_id = body.get("skill_id", "")
        if skill_id and skill_id not in agent.bound_prompt_packs:
            agent.bound_prompt_packs.append(skill_id)
            hub._save_agents()
        return {"ok": True, "bound_prompt_packs": agent.bound_prompt_packs}
    elif action == "unbind":
        skill_id = body.get("skill_id", "")
        if skill_id in agent.bound_prompt_packs:
            agent.bound_prompt_packs.remove(skill_id)
            hub._save_agents()
        return {"ok": True, "bound_prompt_packs": agent.bound_prompt_packs}
    elif action == "discover":
        scan_dirs = body.get("scan_dirs", [])
        if agent.working_dir:
            for sub in [".claw/skills", ".claude/skills", "skills"]:
                d = os.path.join(agent.working_dir, sub)
                if os.path.isdir(d) and d not in scan_dirs:
                    scan_dirs.append(d)
        home = os.path.expanduser("~")
        for d in [os.path.join(home, ".tudou_claw", "skills"),
                  os.path.join(os.getcwd(), "skills"),
                  os.path.join(os.getcwd(), ".claw", "skills")]:
            if os.path.isdir(d) and d not in scan_dirs:
                scan_dirs.append(d)
        new_count = registry.discover(scan_dirs if scan_dirs else None)
        return {
            "ok": True,
            "new_skills": new_count,
            "total": len(registry.store.get_active()),
            "scan_dirs": registry.store._scan_dirs,
        }
    elif action == "import_from_catalog":
        import json as _json
        from pathlib import Path as _Path
        from ...core.prompt_enhancer import PromptPack
        skill_ids = body.get("skill_ids", [])
        catalog_path = _Path(__file__).resolve().parent.parent.parent / "data" / "community_skills.json"
        try:
            with open(catalog_path, 'r', encoding='utf-8') as f:
                catalog = _json.load(f)
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
            return {"ok": True, "imported": imported_count, "bound_prompt_packs": agent.bound_prompt_packs}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "import_local":
        local_path = body.get("path", "")
        if not os.path.isdir(local_path):
            raise HTTPException(status_code=400, detail="Invalid path or directory not found")
        if local_path not in registry.store._scan_dirs:
            registry.store._scan_dirs.append(local_path)
        new_count = registry.discover([local_path])
        return {"ok": True, "new_skills": new_count, "scan_path": local_path}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


@router.get("/agent/{agent_id}/prompt-packs")
async def get_prompt_packs(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List prompt packs bound to the agent."""
    agent = _get_agent_or_404(hub, agent_id)
    from ...core.prompt_enhancer import get_prompt_pack_registry
    registry = get_prompt_pack_registry()
    bound = []
    for sid in agent.bound_prompt_packs:
        rec = registry.store.get(sid)
        if rec:
            bound.append(rec.to_dict())
    return {
        "bound_skills": bound,
        "bound_prompt_packs": agent.bound_prompt_packs,
        "registry_stats": registry.store.get_stats(),
    }


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/persona")
async def apply_persona(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Apply a persona template to the agent."""
    persona_id = body.get("persona_id", "")
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id required")
    ok = hub.apply_persona(agent_id, persona_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent or persona not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Thinking — trigger & history
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/thinking/trigger")
async def trigger_thinking(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Trigger an active-thinking cycle."""
    agent = _get_agent_or_404(hub, agent_id)
    trigger = body.get("trigger", "manual")
    context = body.get("context", "")
    try:
        result = agent.trigger_thinking(trigger=trigger, context=context)
        hub._save_agents()
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/{agent_id}/thinking/history")
async def get_thinking_history(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get thinking history (POST variant for filtered queries)."""
    agent = _get_agent_or_404(hub, agent_id)
    history = []
    if agent.active_thinking:
        history = [r.to_dict() for r in agent.active_thinking.history[-20:]]
    stats = agent.active_thinking.get_stats() if agent.active_thinking else None
    return {"history": history, "stats": stats}


# ---------------------------------------------------------------------------
# Workspace authorization
# ---------------------------------------------------------------------------

@router.post("/agent/workspace/authorize")
async def workspace_authorize(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Authorize an agent to access another agent's workspace."""
    agent_id = body.get("agent_id", "")
    target_agent_id = body.get("target_agent_id", "")
    if not agent_id or not target_agent_id:
        raise HTTPException(status_code=400, detail="agent_id and target_agent_id required")
    agent = hub.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    if target_agent_id not in agent.authorized_workspaces:
        agent.authorized_workspaces.append(target_agent_id)
        hub._save_agents()
    return {"ok": True, "authorized_workspaces": agent.authorized_workspaces}


@router.post("/agent/workspace/revoke")
async def workspace_revoke(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Revoke workspace access between agents."""
    agent_id = body.get("agent_id", "")
    target_agent_id = body.get("target_agent_id", "")
    if not agent_id or not target_agent_id:
        raise HTTPException(status_code=400, detail="agent_id and target_agent_id required")
    agent = hub.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    if target_agent_id in agent.authorized_workspaces:
        agent.authorized_workspaces.remove(target_agent_id)
        hub._save_agents()
    return {"ok": True, "authorized_workspaces": agent.authorized_workspaces}


@router.post("/agent/workspace/list")
async def workspace_list(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List workspaces for an agent."""
    agent_id = body.get("agent_id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id required")
    agent = hub.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return {
        "agent_id": agent_id,
        "own_workspace": agent.working_dir,
        "shared_workspace": getattr(agent, "shared_workspace", ""),
        "authorized_workspaces": agent.authorized_workspaces,
    }


# ---------------------------------------------------------------------------
# Self-improvement
# ---------------------------------------------------------------------------

@router.post("/agent/{agent_id}/self-improvement/enable")
async def enable_self_improvement(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Enable self-improvement for the agent."""
    agent = _get_agent_or_404(hub, agent_id)
    import_exp = body.get("import_experience", True)
    import_limit = body.get("import_limit", 50)
    try:
        result = agent.enable_self_improvement(
            import_experience=import_exp, import_limit=import_limit)
        hub._save_agents()
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/{agent_id}/self-improvement/disable")
async def disable_self_improvement(
    agent_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Disable self-improvement for the agent."""
    agent = _get_agent_or_404(hub, agent_id)
    if hasattr(agent, "disable_self_improvement"):
        agent.disable_self_improvement()
    hub._save_agents()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pending tasks & skill packages (GET)
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/pending-tasks")
async def get_pending_tasks(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List pending tasks across all projects for this agent."""
    _get_agent_or_404(hub, agent_id)
    items = hub.list_agent_pending_tasks(agent_id)
    return {"pending": items, "count": len(items)}


@router.get("/agent/{agent_id}/skill-pkgs")
async def get_skill_pkgs(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List skill packages granted to this agent."""
    _get_agent_or_404(hub, agent_id)
    reg = getattr(hub, "skill_registry", None)
    if not reg:
        return {"skills": []}
    items = [i.to_dict() for i in reg.list_for_agent(agent_id)]
    return {"skills": items}


# ---------------------------------------------------------------------------
# Agent files (persistent artifact store)
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/files")
async def get_agent_files(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List persistent files produced by the agent."""
    agent = _get_agent_or_404(hub, agent_id)
    shadow = getattr(agent, "_shadow", None)
    if shadow is None:
        try:
            from ...agent_state.shadow import install_into_agent
            shadow = install_into_agent(agent)
        except Exception:
            shadow = None
    if shadow is None:
        return {"files": [], "count": 0, "shadow": False, "turns": [], "orphans": []}
    try:
        rescanned = shadow.rescan_deliverable_dir()
    except Exception:
        rescanned = 0
    try:
        idx = shadow.compute_file_index_from_events()
    except Exception:
        idx = {"turns": [], "orphans": [], "total_assistant_turns": 0}
    files = shadow.list_all_file_refs()
    return {
        "files": files,
        "count": len(files),
        "rescanned": rescanned,
        "shadow": True,
        "turns": idx.get("turns", []),
        "orphans": idx.get("orphans", []),
        "total_assistant_turns": idx.get("total_assistant_turns", 0),
    }


# ---------------------------------------------------------------------------
# Execution analyses
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/analyses")
async def get_agent_analyses(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get recent execution analyses."""
    agent = _get_agent_or_404(hub, agent_id)
    if agent._execution_analyzer:
        analyses = agent._execution_analyzer.get_recent_analyses(20)
        return {"analyses": [a.to_dict() for a in analyses]}
    return {"analyses": []}


# ---------------------------------------------------------------------------
# Unified agent task view (project + standalone)
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/all-tasks")
async def get_agent_all_tasks(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get all tasks for an agent: project tasks + standalone tasks."""
    _get_agent_or_404(hub, agent_id)
    proj_tasks = []
    try:
        proj_tasks = hub.list_agent_pending_tasks(agent_id)
    except Exception:
        pass
    standalone = []
    reg = getattr(hub, "standalone_task_registry", None)
    if reg is not None:
        standalone = [t.to_dict() for t in reg.list(assignee=agent_id)]
    return {
        "project_tasks": proj_tasks,
        "standalone_tasks": standalone,
        "counts": {
            "project": len(proj_tasks),
            "standalone": len(standalone),
            "total": len(proj_tasks) + len(standalone),
        },
    }


# ---------------------------------------------------------------------------
# Enhancement info (GET)
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/enhancement")
async def get_agent_enhancement(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get enhancement module info for an agent."""
    agent = _get_agent_or_404(hub, agent_id)
    info = agent.get_enhancement_info() if hasattr(agent, "get_enhancement_info") else None
    if info:
        kl = info.get("knowledge_entries", [])
        rl = info.get("reasoning_patterns", [])
        ml = info.get("memory_nodes", [])
        tl = info.get("tool_chains", [])
        info["knowledge_list"] = kl
        info["reasoning_list"] = rl
        info["memory_list"] = ml
        info["tool_chain_list"] = tl
        info["knowledge_entries"] = len(kl) if isinstance(kl, list) else kl
        info["reasoning_patterns"] = len(rl) if isinstance(rl, list) else rl
        info["memory_nodes"] = len(ml) if isinstance(ml, list) else ml
        info["tool_chains"] = len(tl) if isinstance(tl, list) else tl
    try:
        from ...core.execution_analyzer import list_enhancement_presets
        presets = list_enhancement_presets()
    except (ImportError, Exception):
        presets = []
    return {"enhancement": info, "presets": presets}


# ---------------------------------------------------------------------------
# Execution plans
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/plans")
async def get_agent_plans(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get execution plans for an agent."""
    agent = _get_agent_or_404(hub, agent_id)
    current = agent.get_current_plan() if hasattr(agent, "get_current_plan") else None
    plans = agent.get_execution_plans(limit=10) if hasattr(agent, "get_execution_plans") else []
    return {"current_plan": current, "plans": plans}


# ---------------------------------------------------------------------------
# Engine info & Transcript (migrated from old portal_routes_get)
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/engine")
async def get_agent_engine(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get agent engine state summary."""
    return hub.get_agent_engine_info(agent_id) or {}


@router.get("/agent/{agent_id}/transcript")
async def get_agent_transcript(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get agent full transcript replay."""
    return {"transcript": hub.get_agent_transcript(agent_id)}


# ---------------------------------------------------------------------------
# Memory stats — L1 / L2 / L3 counts + recent entries
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/memory-stats")
async def get_agent_memory_stats(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Return L1/L2/L3 memory counts and recent L2/L3 entries for one agent."""
    agent = _get_agent_or_404(hub, agent_id)
    return _build_memory_stats(agent)


@router.get("/agents/memory-stats")
async def get_all_agents_memory_stats(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Return L1/L2/L3 counts for every agent (batch, for card rendering)."""
    result = {}
    for aid, agent in hub.agents.items():
        if getattr(agent, "parent_id", None):
            continue
        try:
            mm = _get_mm()
            l1 = len([m for m in (agent.messages or []) if m.get("role") != "system"])
            l2 = mm.count_episodic(aid) if mm else 0
            l3 = mm.count_facts(aid) if mm else 0
        except Exception:
            l1, l2, l3 = 0, 0, 0
        result[aid] = {"l1": l1, "l2": l2, "l3": l3}
    return result


def _get_mm():
    """Lazy-get global MemoryManager, return None if unavailable."""
    try:
        from ...core.memory import get_memory_manager
        return get_memory_manager()
    except Exception:
        return None


def _build_memory_stats(agent) -> dict:
    """Build detailed memory stats dict for a single agent."""
    mm = _get_mm()
    aid = agent.id

    # L1: non-system messages in current context window
    msgs = agent.messages or []
    l1_count = len([m for m in msgs if m.get("role") != "system"])

    # L2 & L3 counts
    l2_count = mm.count_episodic(aid) if mm else 0
    l3_count = mm.count_facts(aid) if mm else 0

    # Recent L2 episodic entries (last 10)
    l2_entries = []
    if mm:
        try:
            for ep in mm.get_recent_episodic(aid, limit=10):
                l2_entries.append({
                    "id": ep.id,
                    "summary": ep.summary[:500] if ep.summary else "",
                    "keywords": ep.keywords,
                    "turn_start": ep.turn_start,
                    "turn_end": ep.turn_end,
                    "message_count": ep.message_count,
                    "compression_level": getattr(ep, "compression_level", 0),
                    "created_at": ep.created_at,
                })
        except Exception:
            pass

    # Recent L3 semantic facts (last 20)
    l3_entries = []
    if mm:
        try:
            for fact in mm.get_recent_facts(aid, limit=20):
                l3_entries.append({
                    "id": fact.id,
                    "category": fact.category,
                    "content": fact.content[:500] if fact.content else "",
                    "confidence": getattr(fact, "confidence", 0),
                    "source": getattr(fact, "source", ""),
                    "created_at": fact.created_at,
                    "updated_at": getattr(fact, "updated_at", ""),
                })
        except Exception:
            pass

    # L3 by category breakdown
    l3_by_category = {}
    if mm:
        try:
            for cat in ("intent", "reasoning", "outcome", "rule", "reflection"):
                facts = mm.get_recent_facts(aid, limit=1, category=cat)
                # Use count query if available
                row = mm._conn.execute(
                    "SELECT COUNT(*) as cnt FROM memory_semantic WHERE agent_id=? AND category=?",
                    (aid, cat),
                ).fetchone()
                l3_by_category[cat] = row["cnt"] if row else 0
        except Exception:
            pass

    return {
        "l1": l1_count,
        "l2": l2_count,
        "l3": l3_count,
        "l3_by_category": l3_by_category,
        "l2_entries": l2_entries,
        "l3_entries": l3_entries,
    }


# ---------------------------------------------------------------------------
# Evolution goals & achievement
# ---------------------------------------------------------------------------

@router.get("/agent/{agent_id}/achievement")
async def get_agent_achievement(
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get agent's evolution goals and achievement rates."""
    agent = _get_agent_or_404(hub, agent_id)

    goals = getattr(agent, 'evolution_goals', []) or []
    engine = getattr(agent, 'self_improvement', None)

    achievement = {}
    history = []
    if engine:
        for g in goals:
            gid = g.get("id", "")
            if gid:
                achievement[gid] = engine.get_achievement_rate(gid)
        history = (engine.quality_history or [])[-20:]

    return {
        "goals": goals,
        "achievement_rates": achievement,
        "overall_rate": engine.get_achievement_rate() if engine else 0,
        "history": history,
    }


@router.post("/agent/{agent_id}/goals")
async def update_agent_goals(
    agent_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update agent's evolution goals."""
    agent = _get_agent_or_404(hub, agent_id)

    goals = body.get("goals", [])
    if not isinstance(goals, list):
        raise HTTPException(400, "goals must be a list")

    # Ensure each goal has an id
    import uuid
    for g in goals:
        if not g.get("id"):
            g["id"] = uuid.uuid4().hex[:8]
        if "target_score" not in g:
            g["target_score"] = 80
        if "current_score" not in g:
            g["current_score"] = 0

    agent.evolution_goals = goals
    # Persist
    try:
        hub.save_agent_session(agent_id)
    except Exception:
        pass

    return {"ok": True, "goals": agent.evolution_goals}
