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
    _check_enabled()
    raise HTTPException(501, "not implemented (V2 delivers)")


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
