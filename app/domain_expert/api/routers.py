"""Expert API surface. Phase 0: all 501 stubs.

Real implementations land via vertical slices V1-V5 (see
docs/superpowers/plans/2026-05-10-INDEX.md).

All endpoints are under /api/portal — sub-routes either:
    /api/portal/specialty-templates           (catalog browsing)
    /api/portal/agent/{agent_id}/expert/*     (per-agent operations)

When TUDOU_EXPERT_DISABLED=1, every endpoint here returns 503.
"""
from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException, Body

# Reuse existing auth + hub deps from the main API package
from app.api.deps.auth import CurrentUser, get_current_user
from app.api.deps.hub import get_hub
from .. import _config

logger = logging.getLogger("tudouclaw.api.expert")
router = APIRouter(prefix="/api/portal", tags=["expert"])


def _check_enabled():
    """Gate all endpoints behind the feature flag."""
    if _config.is_disabled():
        raise HTTPException(503, "expert module disabled (TUDOU_EXPERT_DISABLED=1)")


# ── Specialty templates (catalog) ──
# V1 implementation (2026-05-10): replaces Phase 0's 501 stubs by wiring
# the Track D template loader into REST endpoints. Browse-only — no
# mutation here; bundle apply (POST initialize) lands in V2.

@router.get("/specialty-templates", summary="List available specialty templates")
async def list_specialty_templates(
    user: CurrentUser = Depends(get_current_user),
):
    """Return all shipped specialty templates as a lightweight summary list.

    Each entry has id / name / specialty / version / icon / description so
    the UI 养成 tab can render the template picker without follow-up
    fetches. For full schema (eval_suite, chunker, level_rules, etc.) call
    GET /specialty-templates/{template_id}.
    """
    _check_enabled()
    try:
        from ..template_loader import load_all
        templates = load_all()
    except Exception as e:
        logger.exception("template_loader.load_all failed")
        raise HTTPException(500, f"template registry unavailable: {e}")
    return {
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "specialty": t.specialty,
                "version": t.version,
                "icon": t.icon,
                "description": t.description,
                "required_packs_count": len(t.required_packs)
                                        + len(t.required_anthropic_packs),
                "required_skills_count": len(t.required_skills),
                "level_count": len(t.level_rules),
            }
            for t in templates
        ],
        "total": len(templates),
    }


@router.get("/specialty-templates/{template_id}", summary="Get one template detail")
async def get_specialty_template(
    template_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Return full SpecialtyTemplate as dict — used by V2 to display the
    bundle preview before user confirms cultivation."""
    _check_enabled()
    try:
        from ..template_loader import (
            load, load_all, TemplateNotFoundError, TemplateInvalidError,
        )
    except ImportError as e:
        raise HTTPException(500, f"template_loader unavailable: {e}")
    # Resolve template_id by trying:
    #   1. Direct filename match (e.g. "legal" → legal.yaml)
    #   2. Inner id match (e.g. "legal-expert" → scan load_all() for t.id)
    # The picker UI sends t.id (e.g. "legal-expert") but the loader keys
    # by filename — without this fallback every preview returned 404.
    try:
        tpl = load(template_id)
    except TemplateNotFoundError:
        try:
            for cand in load_all():
                if cand.id == template_id:
                    tpl = cand
                    break
            else:
                raise HTTPException(404, f"template {template_id!r} not found")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"template lookup by id failed: {e}")
    except TemplateInvalidError as e:
        raise HTTPException(500, f"template {template_id!r} invalid: {e}")
    except Exception as e:
        raise HTTPException(500, f"template load failed: {e}")
    return tpl.to_dict()


# ── Per-agent expert state ──

@router.get("/agent/{agent_id}/expert", summary="Get expert status for an agent")
async def get_expert_status(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V1 implementation. Returns agent's expert state:
      - specialty / level / template_version / lora_version / initialized_at
        (from agent dataclass fields persisted in agents.data JSON)
      - profile (from ~/.tudou_claw/expert/<id>/config.json if cultivated)
      - `cultivated`: bool — whether this agent is currently a専家 agent

    Returns 404 if agent doesn't exist. Returns 200 with `cultivated=False`
    and empty profile if agent exists but hasn't been initialized as expert.
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    cultivated = bool(getattr(agent, "expert_specialty", "") or "")
    payload = {
        "agent_id": agent_id,
        "agent_name": getattr(agent, "name", ""),
        "cultivated": cultivated,
        "expert_specialty": getattr(agent, "expert_specialty", "") or "",
        "expert_template_version": getattr(agent, "expert_template_version", "") or "",
        "expert_level": getattr(agent, "expert_level", "novice") or "novice",
        "expert_lora_version": getattr(agent, "expert_lora_version", "") or "",
        "expert_initialized_at": float(
            getattr(agent, "expert_initialized_at", 0.0) or 0.0
        ),
        "profile": None,
    }
    if cultivated:
        # Try to load the on-disk ExpertProfile (richer than agent fields)
        try:
            from ..profile import ExpertProfile
            p = ExpertProfile.load(agent_id)
            if p is not None:
                payload["profile"] = p.to_dict()
        except Exception as e:
            logger.warning("ExpertProfile.load(%s) failed: %s", agent_id, e)
    return payload


@router.post("/agent/{agent_id}/expert/initialize", summary="Start expert cultivation")
async def initialize_expert(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V2 implementation. Apply a specialty template to an agent — the
    "Start Cultivation" button.

    Body: `{"template_id": "<template_id>", "force": false}`

    What it does (delegating to Track D's apply_bundle):
      1. Look up the agent (404 if missing).
      2. Refuse if agent is already cultivated AND force=false (returns 409
         with current specialty so client can prompt user to delete first).
      3. Load the SpecialtyTemplate (404 if id unknown). Tries filename
         match first ("legal" → legal.yaml), falls back to inner id match
         ("legal-expert" → load_all + scan).
      4. Wire callbacks against the live PromptPackRegistry + skill
         registry so apply_bundle can detect "pack X not in registry".
      5. Call apply_bundle → stamps agent.expert_* fields + appends
         packs/skills/mcps, persists via hub._save_agents.
      6. Write ExpertProfile snapshot to ~/.tudou_claw/expert/<id>/config.json.
      7. Return the BundleApplyResult so UI can show what was applied vs
         skipped vs missing.
    """
    _check_enabled()

    # ── Step 1: agent ──
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")

    template_id = (body.get("template_id") or "").strip()
    if not template_id:
        raise HTTPException(400, "body must include 'template_id' (string)")
    force = bool(body.get("force") or False)

    # ── Step 2: refuse re-cultivation unless force ──
    cur_specialty = getattr(agent, "expert_specialty", "") or ""
    if cur_specialty and not force:
        raise HTTPException(
            409,
            {
                "error": "already_cultivated",
                "agent_id": agent_id,
                "current_specialty": cur_specialty,
                "current_template_version":
                    getattr(agent, "expert_template_version", ""),
                "hint": "DELETE /agent/{id}/expert first, or pass force=true.",
            },
        )

    # ── Step 3: template (filename or inner-id) ──
    try:
        from ..template_loader import (
            load, load_all, TemplateNotFoundError, TemplateInvalidError,
        )
    except ImportError as e:
        raise HTTPException(500, f"template_loader unavailable: {e}")
    template = None
    try:
        template = load(template_id)
    except TemplateNotFoundError:
        try:
            for cand in load_all():
                if cand.id == template_id:
                    template = cand
                    break
        except Exception as e:
            raise HTTPException(500, f"template lookup by id failed: {e}")
        if template is None:
            raise HTTPException(404, f"template {template_id!r} not found")
    except TemplateInvalidError as e:
        raise HTTPException(500, f"template {template_id!r} invalid: {e}")

    # ── Step 4: wire callbacks ──
    pack_exists_cb = None
    anthropic_pack_exists_cb = None
    skill_exists_cb = None
    skill_grant_cb = None

    # Prompt packs — registry is shared with the existing PromptPack store
    try:
        from app.skills.prompt_enhancer import get_prompt_pack_registry
        pp_reg = get_prompt_pack_registry()
        store = getattr(pp_reg, "store", None)
        if store is not None:
            pack_exists_cb = lambda pid: store.get(pid) is not None
            # Anthropic packs (akwp_*) live in the same store after import,
            # so the same callback works.
            anthropic_pack_exists_cb = pack_exists_cb
    except Exception as e:
        logger.info("PromptPackRegistry unavailable, packs treated as present: %s", e)

    # Skills — hub.skill_registry (if present)
    skill_registry = getattr(hub, "skill_registry", None)
    if skill_registry is not None:
        if hasattr(skill_registry, "has"):
            skill_exists_cb = lambda sid: skill_registry.has(sid)
        elif hasattr(skill_registry, "get"):
            skill_exists_cb = lambda sid: skill_registry.get(sid) is not None
        if hasattr(skill_registry, "grant"):
            def _grant(aid, sid):
                try:
                    skill_registry.grant(sid, aid)
                except TypeError:
                    skill_registry.grant(aid, sid)
            skill_grant_cb = _grant
    if skill_grant_cb is None:
        # Fallback: append directly to agent.granted_skills.
        def _grant_fallback(aid, sid):
            cur = list(getattr(agent, "granted_skills", []) or [])
            if sid not in cur:
                cur.append(sid)
                agent.granted_skills = cur
        skill_grant_cb = _grant_fallback

    # ── Step 5: apply ──
    from ..bundle_apply import apply_bundle
    save_called = {"n": 0}
    def _save():
        save_called["n"] += 1
        try:
            hub._save_agents()
        except Exception as e:
            logger.warning("hub._save_agents failed: %s", e)

    result = apply_bundle(
        template, agent,
        save_callback=_save,
        skill_grant_callback=skill_grant_cb,
        pack_exists_callback=pack_exists_cb,
        anthropic_pack_exists_callback=anthropic_pack_exists_cb,
        skill_exists_callback=skill_exists_cb,
    )

    # ── Step 6: write ExpertProfile snapshot ──
    try:
        from ..profile import ExpertProfile
        profile = ExpertProfile(
            agent_id=agent_id,
            specialty=template.specialty,
            template_id=template.id,
            template_version=template.version,
            level=getattr(agent, "expert_level", "novice") or "novice",
            active_lora_version=getattr(agent, "expert_lora_version", "") or "",
            initialized_at=float(
                getattr(agent, "expert_initialized_at", 0.0) or 0.0
            ),
        )
        profile.save()
    except Exception as e:
        logger.warning("ExpertProfile.save failed for %s: %s", agent_id, e)

    # ── Step 7: return ──
    from dataclasses import asdict
    out = asdict(result)
    out["expert_level_after"] = getattr(agent, "expert_level", "novice")
    out["save_called"] = save_called["n"] > 0
    out["is_complete"] = result.is_complete()
    out["summary"] = result.summary()
    out["ok"] = True
    return out


@router.delete("/agent/{agent_id}/expert", summary="Disable / delete expert state")
async def delete_expert(
    agent_id: str,
    keep_data: bool = True,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V1 implementation. Two modes:
      - `?keep_data=true` (default): clear `agent.expert_specialty` etc.
        but preserve ~/.tudou_claw/expert/<id>/ on disk. Re-initialize
        later to resume cultivation from the same data.
      - `?keep_data=false`: also remove the entire expert data tree.
        Destructive — corpus, traces, LoRA snapshots all gone.

    Returns 404 if agent doesn't exist. Returns 200 with summary of
    what was cleared.
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    was_cultivated = bool(getattr(agent, "expert_specialty", "") or "")
    cleared_fields = {}
    if was_cultivated:
        cleared_fields = {
            "expert_specialty": agent.expert_specialty,
            "expert_template_version": agent.expert_template_version,
            "expert_level": agent.expert_level,
            "expert_lora_version": agent.expert_lora_version,
            "expert_initialized_at": agent.expert_initialized_at,
        }
        agent.expert_specialty = ""
        agent.expert_template_version = ""
        agent.expert_level = "novice"
        agent.expert_lora_version = ""
        agent.expert_initialized_at = 0.0
        try:
            hub._save_agents()
        except Exception as e:
            logger.warning("hub._save_agents failed during delete_expert: %s", e)

    data_removed = False
    if not keep_data:
        from ..  import _config
        import shutil
        d = _config.expert_dir_for(agent_id)
        try:
            if shutil.rmtree.__module__ and __import__("os").path.isdir(d):
                shutil.rmtree(d)
                data_removed = True
        except Exception as e:
            logger.warning("expert dir removal failed for %s: %s", agent_id, e)

    return {
        "ok": True,
        "agent_id": agent_id,
        "was_cultivated": was_cultivated,
        "cleared_fields": cleared_fields,
        "data_removed": data_removed,
        "data_path_kept": (not data_removed and was_cultivated),
    }


# ── Corpus / RAG ──

@router.post("/agent/{agent_id}/expert/corpus/ingest", summary="Ingest corpus source")
async def corpus_ingest(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


@router.get("/agent/{agent_id}/expert/corpus", summary="List ingested corpus sources")
async def corpus_list(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


@router.post("/agent/{agent_id}/expert/corpus/reindex", summary="Rebuild vector index")
async def corpus_reindex(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


# ── Query + feedback ──

@router.post("/agent/{agent_id}/expert/query", summary="Direct expert query (RAG-augmented)")
async def expert_query(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V4 delivers)")


@router.post("/agent/{agent_id}/expert/feedback", summary="User 👍/👎 feedback on a reply")
async def expert_feedback(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")


# ── Traces / stats ──

@router.get("/agent/{agent_id}/expert/traces", summary="List Q/A trace history")
async def expert_traces(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")


@router.get("/agent/{agent_id}/expert/stats", summary="Cultivation pipeline stats dashboard")
async def expert_stats(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")
