"""Skills and prompt packs router — skill packages, prompt packs, skill store."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.skills")

router = APIRouter(prefix="/api/portal", tags=["skills"])


def _get_skill_or_404(hub, skill_id: str):
    """Get skill package or raise 404."""
    try:
        skill = hub.get_skill_package(skill_id) if hasattr(hub, "get_skill_package") else None
        if not skill:
            raise HTTPException(status_code=404, detail=f"Skill package '{skill_id}' not found")
        return skill
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Skill packages
# ---------------------------------------------------------------------------

@router.get("/skill-pkgs")
async def list_skill_packages(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all installed skill packages."""
    try:
        skills = hub.list_skill_packages() if hasattr(hub, "list_skill_packages") else []
        skills_list = [s.to_dict() if hasattr(s, "to_dict") else s for s in skills]
        return {"skill_packages": skills_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skill-pkgs/{skill_id}")
async def get_skill_package(
    skill_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get skill package detail."""
    try:
        skill = _get_skill_or_404(hub, skill_id)
        data = skill.to_dict() if hasattr(skill, "to_dict") else skill
        return data if isinstance(data, dict) else {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill-pkgs/install")
async def install_skill_package(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Install a skill package."""
    try:
        skill_id = body.get("skill_id", "")
        if not skill_id:
            raise HTTPException(400, "Missing skill_id")

        if hasattr(hub, "install_skill_package"):
            result = hub.install_skill_package(skill_id, body)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill-pkgs/{skill_id}/uninstall")
async def uninstall_skill_package(
    skill_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Uninstall a skill package."""
    try:
        reg = getattr(hub, "skill_registry", None)
        if not reg:
            raise HTTPException(503, "Skill registry unavailable")
        ok = reg.uninstall(skill_id)
        return {"ok": ok}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill-pkgs/{skill_id}/invoke")
async def invoke_skill_package(
    skill_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Invoke a skill package.

    Parity with legacy portal_routes_post: skill-not-found and invalid
    inputs surface as 400 (user error), not 500. Only genuinely
    unexpected exceptions become 500.
    """
    agent_id = body.get("agent_id", "")
    inputs = body.get("inputs", {}) or {}
    reg = getattr(hub, "skill_registry", None)
    if not reg:
        raise HTTPException(503, "Skill registry unavailable")
    try:
        result = reg.invoke(skill_id, agent_id, inputs)
    except HTTPException:
        raise
    except Exception as e:
        # Legacy returned 400 for any reg.invoke() error — matches the
        # semantic "client asked for something invalid" better than 500.
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "result": result}


@router.get("/skill-pkgs/{skill_id}/agents")
async def get_skill_agents(
    skill_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List agents that have this skill package granted."""
    try:
        reg = getattr(hub, "skill_registry", None)
        if not reg:
            return {"agents": []}
        inst = reg.get(skill_id)
        if not inst:
            raise HTTPException(404, "Skill not found")
        agent_ids = getattr(inst, "granted_agents", []) or []
        agents = []
        for aid in agent_ids:
            a = hub.get_agent(aid) if hasattr(hub, "get_agent") else None
            agents.append({
                "agent_id": aid,
                "agent_name": a.name if a else aid,
            })
        return {"agents": agents, "skill_id": skill_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Skill granting and revocation
# ---------------------------------------------------------------------------

@router.post("/skill-pkgs/{skill_id}/grant")
async def grant_skill_to_agents(
    skill_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Grant skill package to agents."""
    try:
        skill = _get_skill_or_404(hub, skill_id)
        agent_ids = body.get("agent_ids", [])

        if hasattr(skill, "grant_to_agents"):
            result = skill.grant_to_agents(agent_ids)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill-pkgs/{skill_id}/revoke")
async def revoke_skill_from_agents(
    skill_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Revoke skill package from agents."""
    try:
        skill = _get_skill_or_404(hub, skill_id)
        agent_ids = body.get("agent_ids", [])

        if hasattr(skill, "revoke_from_agents"):
            result = skill.revoke_from_agents(agent_ids)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Prompt packs
# ---------------------------------------------------------------------------

@router.get("/prompt-packs")
async def list_prompt_packs(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all prompt packs — matches legacy portal_routes_get."""
    try:
        from ...core.prompt_enhancer import get_prompt_pack_registry
        registry = get_prompt_pack_registry()
        skills = [s.to_dict() for s in registry.store.get_active()]
        return {"skills": skills, "stats": registry.store.get_stats()}
    except (ImportError, Exception) as e:
        return {"skills": [], "stats": {}}


@router.post("/prompt-packs")
async def manage_prompt_packs(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create, update, delete, or browse catalog of prompt packs."""
    try:
        action = body.get("action", "create")

        if action == "catalog":
            # -----------------------------------------------------------
            # Browse community_skills.json catalog (matches old server)
            # -----------------------------------------------------------
            import json as _json
            from pathlib import Path as _Path
            catalog_path = _Path(__file__).resolve().parent.parent.parent / "data" / "community_skills.json"
            try:
                with open(catalog_path, "r", encoding="utf-8") as f:
                    catalog = _json.load(f)
            except FileNotFoundError:
                return {"skills": [], "categories": [], "total": 0}

            category_filter = body.get("category", "")
            search_query = body.get("search", "").lower()
            page = body.get("page", 1)
            per_page = body.get("per_page", 20)

            skills = catalog.get("skills", [])
            if category_filter:
                skills = [s for s in skills if s.get("category") == category_filter]
            if search_query:
                skills = [
                    s for s in skills
                    if search_query in s.get("name", "").lower()
                    or search_query in s.get("description", "").lower()
                ]

            total = len(skills)
            start = (page - 1) * per_page
            paginated = skills[start : start + per_page]

            result = [
                {
                    "id": s.get("id", ""),
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                    "icon": s.get("icon", ""),
                    "category": s.get("category", ""),
                }
                for s in paginated
            ]
            return {
                "skills": result,
                "categories": catalog.get("categories", []),
                "total": total,
                "page": page,
                "per_page": per_page,
            }

        elif action == "create":
            pack = hub.create_prompt_pack(body) if hasattr(hub, "create_prompt_pack") else {}
            return {"ok": True, "prompt_pack": pack}
        elif action == "update":
            pack = hub.update_prompt_pack(body.get("pack_id"), body) if hasattr(hub, "update_prompt_pack") else {}
            return {"ok": True, "prompt_pack": pack}
        elif action == "delete":
            hub.delete_prompt_pack(body.get("pack_id")) if hasattr(hub, "delete_prompt_pack") else None
            return {"ok": True}
        else:
            raise HTTPException(400, f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Skill store
# ---------------------------------------------------------------------------

@router.get("/skill-store")
async def get_skill_store_catalog(
    source: str = Query("", description="Filter by source"),
    tag: str = Query("", description="Filter by tag"),
    q: str = Query("", description="Search query"),
    all: str = Query("0", description="Include disallowed"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get skill store catalog — matches legacy portal_routes_get."""
    try:
        store = getattr(hub, "skill_store", None)
        if store is None:
            return {"entries": [], "installed": {}, "annotations": [], "stats": {}}

        include_all = all in ("1", "true", "yes")
        entries = store.list_catalog(
            source_filter=source or "",
            tag=tag or "",
            query=q or "",
            include_disallowed=include_all,
        )

        # Pull installed-skill metadata for UI
        installed_map: dict = {}
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

        return {
            "entries": [e.to_dict() for e in entries],
            "installed": installed_map,
            "annotations": store.list_annotations(),
            "stats": store.stats(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skill-store")
async def manage_skill_store(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Unified skill store management endpoint.

    Mirrors the old BaseHTTPRequestHandler at /api/portal/skill-store.
    Supported actions: search, browse, rescan, install, uninstall,
    grant, revoke, annotate, clear_annotation, set_allowed_sources,
    import, import_bulk, import_from_url, scan_url, cleanup_scan.
    """
    store = getattr(hub, "skill_store", None)
    if store is None:
        raise HTTPException(503, "skill store not initialized")

    action = body.get("action", "search")

    try:
        if action == "search":
            if hasattr(hub, "search_skill_store"):
                return {"results": hub.search_skill_store(body.get("query", ""))}
            return {"results": []}

        if action == "browse":
            if hasattr(hub, "browse_skill_store"):
                return {"results": hub.browse_skill_store(body.get("category", ""))}
            return {"results": []}

        if action == "rescan":
            n = store.scan()
            return {"ok": True, "count": n, "stats": store.stats()}

        if action == "install":
            entry_id = body.get("entry_id", "")
            who = body.get("installed_by", "portal")
            result = store.install_entry(entry_id, installed_by=who)
            store.scan()  # refresh installed flags
            return {"ok": True, "result": result}

        if action == "uninstall":
            entry_id = body.get("entry_id", "")
            ok = store.uninstall_entry(entry_id)
            store.scan()
            return {"ok": ok}

        if action == "grant":
            installed_id = body.get("installed_id", "")
            agent_id = body.get("agent_id", "")
            agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
            wdir = ""
            sync_result = None
            if agent is not None:
                wdir = getattr(agent, "working_directory", "") or ""
                if installed_id not in getattr(agent, "granted_skills", []):
                    try:
                        agent.granted_skills.append(installed_id)
                        hub._save_agents()
                    except Exception as e:
                        logger.warning("grant skill %s to agent %s: %s", installed_id, agent_id, e)
                # Sync skill package to agent workspace
                try:
                    inst = store._registry.get(installed_id) if hasattr(store, "_registry") else None
                    if inst is not None and hasattr(agent, "sync_skill_to_workspace"):
                        sync_result = agent.sync_skill_to_workspace(inst)
                        hub._save_agents()
                except Exception as e:
                    logger.debug("sync skill %s workspace %s: %s", installed_id, agent_id, e)
            ok = store.grant(installed_id, agent_id, agent_working_dir=wdir)
            resp = {"ok": ok}
            if sync_result:
                resp["sync"] = sync_result
            return resp

        if action == "revoke":
            installed_id = body.get("installed_id", "")
            agent_id = body.get("agent_id", "")
            agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
            wdir = getattr(agent, "working_directory", "") if agent else ""
            if agent is not None and installed_id in getattr(agent, "granted_skills", []):
                try:
                    agent.granted_skills.remove(installed_id)
                    hub._save_agents()
                except Exception as e:
                    logger.warning("revoke skill %s from agent %s: %s", installed_id, agent_id, e)
            # Remove skill from agent workspace
            if agent is not None and hasattr(agent, "remove_skill_from_workspace"):
                try:
                    inst = store._registry.get(installed_id) if hasattr(store, "_registry") else None
                    skill_name = ""
                    if inst:
                        skill_name = getattr(getattr(inst, "manifest", None), "name", "") or getattr(inst, "id", "")
                    if skill_name:
                        agent.remove_skill_from_workspace(skill_name)
                        hub._save_agents()
                except Exception:
                    pass
            ok = store.revoke(installed_id, agent_id, agent_working_dir=wdir or "")
            return {"ok": ok}

        if action == "annotate":
            skill_id = body.get("skill_id", "")
            text = body.get("text", "")
            author = body.get("author", "portal")
            ann = store.annotate(skill_id, text, author=author)
            return {"ok": True, "annotation": ann}

        if action == "clear_annotation":
            skill_id = body.get("skill_id", "")
            ok = store.clear_annotation(skill_id)
            return {"ok": ok}

        if action == "set_allowed_sources":
            sources = body.get("sources", []) or []
            store.set_allowed_sources(sources)
            return {"ok": True, "allowed": store.allowed_sources()}

        if action == "import":
            from ...skill_store import import_agent_skill as _import_one
            src_path = (body.get("src_path") or "").strip()
            tier = body.get("tier", "community")
            auto_install = body.get("auto_install", True)
            if not src_path:
                raise HTTPException(400, "src_path is required")
            catalog_dir = store.catalog_dirs[-1] if store.catalog_dirs else ""
            if not catalog_dir:
                raise HTTPException(500, "no catalog dir configured")
            result = _import_one(src_path, catalog_dir, source_tier=tier)
            if auto_install and result.get("ok"):
                store.scan()
                eid = result.get("entry_id", "")
                if eid:
                    try:
                        store.install_entry(eid, installed_by="portal")
                        store.scan()
                    except Exception:
                        pass
            return result

        if action == "import_bulk":
            from ...skill_store import import_anthropic_skills_bulk as _import_bulk
            src_root = (body.get("src_root") or body.get("directory") or "").strip()
            auto_install = body.get("auto_install", True)
            if not src_root:
                raise HTTPException(400, "src_root is required")
            catalog_dir = store.catalog_dirs[-1] if store.catalog_dirs else ""
            if not catalog_dir:
                raise HTTPException(500, "no catalog dir configured")
            results = _import_bulk(src_root, catalog_dir)
            if auto_install:
                store.scan()
                for r in (results if isinstance(results, list) else []):
                    eid = r.get("entry_id", "")
                    if eid and r.get("ok"):
                        try:
                            store.install_entry(eid, installed_by="portal")
                        except Exception:
                            pass
                store.scan()
            return {"ok": True, "results": results}

        if action == "import_from_url":
            from ...skill_store import scan_remote_url, import_from_scan_result
            url = (body.get("url") or "").strip()
            auto_install = body.get("auto_install", True)
            if not url:
                raise HTTPException(400, "url is required")
            catalog_dir = store.catalog_dirs[-1] if store.catalog_dirs else ""
            scan_data = scan_remote_url(url)
            results = import_from_scan_result(scan_data, catalog_dir)
            if auto_install:
                store.scan()
                for r in (results if isinstance(results, list) else []):
                    eid = r.get("entry_id", "")
                    if eid and r.get("ok"):
                        try:
                            store.install_entry(eid, installed_by="portal")
                        except Exception:
                            pass
                store.scan()
            return {"ok": True, "results": results}

        if action == "scan_url":
            from ...skill_store import scan_remote_url
            url = (body.get("url") or "").strip()
            if not url:
                raise HTTPException(400, "url is required")
            data = scan_remote_url(url)
            return data

        if action == "cleanup_scan":
            from ...skill_store import cleanup_scan_temp
            temp_dir = body.get("temp_dir", "")
            cleanup_scan_temp(temp_dir)
            return {"ok": True}

        raise HTTPException(400, f"Unknown skill-store action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("skill-store action=%s error", action)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Pending skills (SkillForge drafts awaiting admin approval)
# ---------------------------------------------------------------------------

@router.get("/pending-skills")
async def list_pending_skills(
    status: str = Query("", description="Filter by status: draft, exported, approved, rejected"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List skill drafts generated by SkillForge, pending admin review."""
    try:
        from ...skills._skill_forge import get_skill_forge
        forge = get_skill_forge()
        drafts = forge.list_drafts()

        if status:
            drafts = [d for d in drafts if d.status == status]

        return {
            "drafts": [d.to_dict() for d in drafts],
            "total": len(drafts),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pending-skills/{draft_id}")
async def get_pending_skill(
    draft_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get detail of a single skill draft including code files and parsed manifest."""
    try:
        from ...skills._skill_forge import get_skill_forge
        forge = get_skill_forge()
        drafts = {d.id: d for d in forge.list_drafts()}
        draft = drafts.get(draft_id)
        if not draft:
            raise HTTPException(404, f"Draft not found: {draft_id}")
        result = draft.to_dict()
        # Add parsed manifest for easier rendering
        try:
            import yaml as _yaml
            if _yaml and draft.manifest_yaml:
                result["manifest_parsed"] = _yaml.safe_load(draft.manifest_yaml)
        except Exception:
            pass
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pending-skills/{draft_id}/approve")
async def approve_pending_skill(
    draft_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Approve a skill draft and import it into the skill store.

    forge.approve_draft() auto-exports the package. This endpoint
    then rescans the skill store catalog to pick it up.
    """
    try:
        from ...skills._skill_forge import get_skill_forge
        forge = get_skill_forge()
        result = forge.approve_draft(draft_id)
        if "error" in result:
            raise HTTPException(404, result["error"])

        # Rescan skill store catalog to pick up the exported package
        import_result = {}
        export_dir = result.get("export_dir", "")
        try:
            store = getattr(hub, "skill_store", None)
            if store is not None:
                store.scan()  # rescan all catalog_dirs including pending_skills
                import_result = {"imported": True, "export_dir": export_dir}
            else:
                import_result = {"imported": False, "reason": "skill_store not available"}
        except Exception as ie:
            logger.warning("Skill store rescan after approval failed: %s", ie)
            import_result = {"imported": False, "error": str(ie)}

        return {
            "ok": True,
            "draft_id": draft_id,
            "status": "approved",
            "import": import_result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pending-skills/import")
async def import_skill_as_draft(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Import a skill directory (created by an agent) as a SkillForge draft.

    Body: { "path": "/abs/path/to/skill_dir" }
    or:   { "agent_id": "xxx", "dir_name": "pptx_skill" }
    """
    import os
    import time as _time
    import yaml as _yaml
    from ...skills._skill_forge import get_skill_forge, SkillDraft

    forge = get_skill_forge()

    # Resolve the skill directory path
    skill_dir = body.get("path", "").strip()
    if not skill_dir:
        agent_id = body.get("agent_id", "").strip()
        dir_name = body.get("dir_name", "").strip()
        if agent_id and dir_name:
            # Look in agent workspace
            agent = hub.get_agent(agent_id) if agent_id else None
            if agent and hasattr(agent, "workspace_dir"):
                skill_dir = os.path.join(str(agent.workspace_dir), dir_name)
            else:
                # Fallback: search common workspace paths
                from ... import DEFAULT_DATA_DIR
                candidates = [
                    os.path.join(DEFAULT_DATA_DIR, "workspaces", "agents", agent_id, "workspace", dir_name),
                ]
                for c in candidates:
                    if os.path.isdir(c):
                        skill_dir = c
                        break
        if not skill_dir:
            raise HTTPException(400, "Provide 'path' or 'agent_id'+'dir_name'")

    if not os.path.isdir(skill_dir):
        raise HTTPException(404, f"Directory not found: {skill_dir}")

    # Read skill files
    manifest_yaml = ""
    skill_md = ""
    code_files = {}
    name = os.path.basename(skill_dir)
    description = ""
    runtime = "markdown"
    triggers = []

    manifest_path = os.path.join(skill_dir, "manifest.yaml")
    if os.path.isfile(manifest_path):
        manifest_yaml = open(manifest_path, "r", encoding="utf-8").read()
        try:
            m = _yaml.safe_load(manifest_yaml) or {}
            name = m.get("name", name)
            desc = m.get("description", "")
            description = desc if isinstance(desc, str) else (desc.get("zh-CN") or desc.get("en") or str(desc))
            runtime = m.get("runtime", "markdown")
            triggers = m.get("triggers", [])
        except Exception:
            pass

    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    if os.path.isfile(skill_md_path):
        skill_md = open(skill_md_path, "r", encoding="utf-8").read()

    # Collect code files (*.py)
    for fn in os.listdir(skill_dir):
        fp = os.path.join(skill_dir, fn)
        if os.path.isfile(fp) and fn.endswith(".py"):
            try:
                code_files[fn] = open(fp, "r", encoding="utf-8").read()
            except Exception:
                pass

    if not manifest_yaml and not skill_md:
        raise HTTPException(400, "No manifest.yaml or SKILL.md found in directory")

    # Check for duplicate: same name + same version = reject
    import_version = ""
    if manifest_yaml:
        try:
            _mv = _yaml.safe_load(manifest_yaml) or {}
            import_version = _mv.get("version", "")
        except Exception:
            pass
    for existing in forge._drafts.values():
        if existing.name == name and existing.status in ("draft", "exported", "approved"):
            existing_version = ""
            if existing.manifest_yaml:
                try:
                    em = _yaml.safe_load(existing.manifest_yaml) or {}
                    existing_version = em.get("version", "")
                except Exception:
                    pass
            if existing_version == import_version:
                raise HTTPException(
                    409,
                    f"技能 '{name}' v{import_version} 已存在"
                    f"（ID: {existing.id}, 状态: {existing.status}）。"
                    f"请修改 version 后重新导入。"
                )

    # Create a SkillDraft
    draft_id = f"SF-{_time.strftime('%Y%m%d')}-IMP-{os.urandom(3).hex()}"
    draft = SkillDraft(
        id=draft_id,
        name=name,
        description=description,
        source_experiences=[],
        role=body.get("role", ""),
        scene_pattern="",
        triggers=triggers if isinstance(triggers, list) else [triggers],
        manifest_yaml=manifest_yaml,
        skill_md=skill_md,
        confidence=0.9,
        created_at=_time.time(),
        status="exported",
        runtime=runtime,
        code_files=code_files,
    )

    # Add to forge and save
    forge._drafts[draft_id] = draft
    forge._save_drafts()

    logger.info("Imported skill draft from directory: %s → %s", skill_dir, draft_id)
    return {
        "ok": True,
        "draft_id": draft_id,
        "name": name,
        "runtime": runtime,
        "code_files": list(code_files.keys()),
        "source_dir": skill_dir,
    }


@router.post("/pending-skills/{draft_id}/reject")
async def reject_pending_skill(
    draft_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Reject a skill draft."""
    try:
        from ...skills._skill_forge import get_skill_forge
        forge = get_skill_forge()
        result = forge.reject_draft(draft_id)
        if "error" in result:
            raise HTTPException(404, result["error"])
        return {"ok": True, "draft_id": draft_id, "status": "rejected"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
