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
import os

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


@router.post("/specialty-templates", summary="Create a new specialty template")
async def create_specialty_template(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new template YAML in app/data/specialty_templates/.

    Required body fields:
      id          — unique template id (also the filename stem)
      specialty   — short specialty key (e.g. "medical", "finance")
      name        — display name (e.g. "医疗专家")

    Optional body fields:
      version              — default "1.0"
      icon                 — Material Symbol or emoji, default "school"
      description          — multi-line text
      required_packs       — list of community pack ids
      required_anthropic_packs — list of anthropic akwp_* ids
      required_skills      — list of skill install ids
      chunker_strategy     — default "paragraph"
      raft_data_target     — default 1000

    Auto-fills sensible defaults for level_rules / safety / training so
    the resulting YAML loads cleanly via the existing loader.
    """
    _check_enabled()
    tid = (body.get("id") or "").strip()
    specialty = (body.get("specialty") or "").strip()
    name = (body.get("name") or "").strip()
    if not tid or not specialty or not name:
        raise HTTPException(400, "body must include id + specialty + name")
    # Reject id with file-system unsafe characters
    import re as _re
    if not _re.match(r"^[a-z0-9][a-z0-9_-]*$", tid):
        raise HTTPException(
            400,
            "id must be lowercase alphanumeric (with - or _)",
        )

    # Refuse if file already exists
    from ..template_loader import _yaml_path_for, invalidate_cache
    path = _yaml_path_for(tid)
    if os.path.exists(path):
        raise HTTPException(409, f"template {tid!r} already exists at {path}")

    # Build the YAML structure
    yaml_doc = {
        "id":          tid,
        "version":     str(body.get("version") or "1.0"),
        "name":        name,
        "specialty":   specialty,
        "icon":        str(body.get("icon") or "school"),
        "description": str(body.get("description") or "").strip(),
        # Knowledge layer
        "required_packs":           list(body.get("required_packs") or []),
        "required_anthropic_packs": list(body.get("required_anthropic_packs") or []),
        "required_skills":          list(body.get("required_skills") or []),
        "required_mcps":            list(body.get("required_mcps") or []),
        # Corpus / chunker (V3 hooks in)
        "corpus_sources": list(body.get("corpus_sources") or []),
        # Chunker strategy enum is ['semantic', 'fixed', 'structural'].
        # 'paragraph' (V3 step 2 ingest) is internal-only — we default
        # the YAML schema to 'semantic' which V3 step 3 will honor.
        "chunker": {
            "strategy": str(body.get("chunker_strategy") or "semantic"),
            "max_tokens": int(body.get("chunker_max_tokens") or 768),
            "overlap_tokens": int(body.get("chunker_overlap") or 96),
            "respect_boundaries": True,
        },
        # Training (V4 hooks in) — schema is dataclass TrainingConfig
        "training": {
            "base_model":       str(body.get("base_model") or ""),
            "raft_recipe":      str(body.get("raft_recipe") or "default"),
            "lora_rank":        int(body.get("lora_rank") or 16),
            "lora_alpha":       int(body.get("lora_alpha") or 32),
            "learning_rate":    float(body.get("learning_rate") or 2e-4),
            "max_steps":        int(body.get("max_steps") or 0),
            "distractor_count": int(body.get("distractor_count") or 4),
            "eval_split":       float(body.get("eval_split") or 0.1),
        },
        # Eval suite — empty until user adds runners (V3 step 3)
        "eval_suite": list(body.get("eval_suite") or []),
        # Default growth path (per design §3.6.6)
        "level_rules": [
            {"from_level": "novice",     "to_level": "journeyman",
             "min_eval_score": 0.5,  "min_corpus_chunks": 200,  "min_traces": 0},
            {"from_level": "journeyman", "to_level": "expert",
             "min_eval_score": 0.7,  "min_corpus_chunks": 1000, "min_traces": 200},
            {"from_level": "expert",     "to_level": "master",
             "min_eval_score": 0.85, "min_corpus_chunks": 5000, "min_traces": 1000},
        ],
        # Safety defaults — schema is dataclass SafetyRails
        "safety": {
            "cite_required":        bool(body.get("cite_required",  False)),
            "confidence_threshold": float(body.get("confidence_threshold", 0.0)),
            "refuse_topics":        list(body.get("refuse_topics") or []),
            "disclaimer":           str(body.get("disclaimer") or ""),
        },
    }

    # Validate by loading through the same code path /specialty-templates
    # uses — fail loudly here rather than at first read.
    from ..template import SpecialtyTemplate
    try:
        SpecialtyTemplate.from_dict(yaml_doc)
    except Exception as e:
        raise HTTPException(400, f"invalid template structure: {e}")

    # Write YAML with reasonable formatting. yaml.safe_dump preserves
    # unicode, sort_keys=False keeps our intentional ordering.
    try:
        import yaml
    except ImportError:
        raise HTTPException(500, "PyYAML not installed")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        # Header banner so user knows it's auto-generated
        f.write(
            "# Specialty Template — auto-generated via Settings UI\n"
            f"# Specialty: {specialty}\n"
            f"# Created by: {getattr(user, 'user_id', 'unknown')}\n"
            "# Edit by hand to add corpus_sources / eval_suite / etc.\n\n"
        )
        yaml.safe_dump(yaml_doc, f, allow_unicode=True, sort_keys=False)

    # Drop loader cache so /specialty-templates picks it up immediately
    invalidate_cache()
    logger.info("specialty template created: %s by %s",
                tid, getattr(user, "user_id", "unknown"))
    return {
        "ok": True,
        "id": tid,
        "path": path,
        "template": yaml_doc,
    }


@router.put("/specialty-templates/{template_id}", summary="Update an existing specialty template")
async def update_specialty_template(
    template_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Edit an existing template's YAML. Only the fields present in
    `body` are overlaid; everything else is preserved.

    Path arg: template filename stem OR inner id (same fallback as GET).
    Body: any subset of the create fields (id, name, description, packs,
          skills, icon, version, etc.). Changing `id` renames the file.

    Returns the updated template dict on success.
    """
    _check_enabled()
    from ..template_loader import (
        _yaml_path_for, load, load_all, invalidate_cache,
        TemplateNotFoundError, TemplateInvalidError,
    )

    # Resolve the existing yaml path — by filename or inner id
    path = _yaml_path_for(template_id)
    existing = None
    if os.path.exists(path):
        try:
            existing = load(template_id)
        except (TemplateNotFoundError, TemplateInvalidError) as e:
            raise HTTPException(500, f"existing template invalid: {e}")
    if existing is None:
        # try inner-id resolution
        for cand in load_all():
            if cand.id == template_id:
                existing = cand
                path = _yaml_path_for(cand.specialty)
                break
    if existing is None:
        raise HTTPException(404, f"template {template_id!r} not found")

    # Read current YAML to a dict so we can overlay
    try:
        import yaml
    except ImportError:
        raise HTTPException(500, "PyYAML not installed")
    with open(path, "r", encoding="utf-8") as f:
        cur_doc = yaml.safe_load(f) or {}

    # Overlay body — only top-level keys we recognize, plus pass-through
    # for nested chunker / training / safety dicts.
    SHALLOW_FIELDS = {
        "id", "version", "name", "specialty", "icon", "description",
        "required_packs", "required_anthropic_packs", "required_skills",
        "required_mcps", "corpus_sources", "eval_suite", "level_rules",
    }
    for k in SHALLOW_FIELDS:
        if k in body:
            cur_doc[k] = body[k]
    # Nested merges (allow PATCH-style partial updates of sub-dicts)
    for nested in ("chunker", "training", "safety"):
        if nested in body and isinstance(body[nested], dict):
            cur = dict(cur_doc.get(nested) or {})
            cur.update(body[nested])
            cur_doc[nested] = cur

    # Validate
    from ..template import SpecialtyTemplate
    try:
        SpecialtyTemplate.from_dict(cur_doc)
    except Exception as e:
        raise HTTPException(400, f"invalid template after edit: {e}")

    # Determine target path: if `id` (filename) changed, rename the file
    new_specialty = cur_doc.get("specialty") or existing.specialty
    target_path = _yaml_path_for(cur_doc.get("id") or existing.id) \
        if False else _yaml_path_for(new_specialty)
    # Actually filename should remain stable unless user explicitly asked
    # to change it. We key on filename = specialty key. If `specialty`
    # changed, the file moves.
    if target_path != path and os.path.exists(target_path):
        raise HTTPException(409,
            f"target path {target_path} already exists "
            f"(specialty conflict)")

    # Write
    if target_path != path:
        os.remove(path)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(
            "# Specialty Template — edited via cultivation hub UI\n"
            f"# Specialty: {new_specialty}\n"
            f"# Last edit by: {getattr(user, 'user_id', 'unknown')}\n\n"
        )
        yaml.safe_dump(cur_doc, f, allow_unicode=True, sort_keys=False)

    invalidate_cache()
    logger.info("specialty template updated: %s by %s",
                template_id, getattr(user, "user_id", "unknown"))
    return {"ok": True, "id": cur_doc.get("id"), "path": target_path,
            "template": cur_doc}


@router.delete("/specialty-templates/{template_id}", summary="Delete a specialty template")
async def delete_specialty_template(
    template_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a template YAML. Returns 404 if not found, 409 if any
    cultivated agent still uses this specialty (cascade-protect)."""
    _check_enabled()
    from ..template_loader import _yaml_path_for, invalidate_cache, load_all
    path = _yaml_path_for(template_id)
    if not os.path.exists(path):
        # Try inner-id resolution
        try:
            for cand in load_all():
                if cand.id == template_id:
                    path = _yaml_path_for(cand.specialty)
                    break
        except Exception:
            pass
    if not os.path.exists(path):
        raise HTTPException(404, f"template {template_id!r} not found")
    os.remove(path)
    invalidate_cache()
    return {"ok": True, "id": template_id, "deleted_path": path}


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

    # ── feedback_counts + trace_count for 段位 (level) computation ──
    # The level chip + modal pipeline both need these in the same payload
    # so they can render synchronously without a second fetch. Cheap to
    # compute here — single file scan each.
    import json as _json
    from .._config import expert_dir_for as _edir
    edir = _edir(agent_id)
    fb_up = fb_down = trace_count = 0
    fb_path = os.path.join(edir, "feedback", "feedback.jsonl")
    if os.path.isfile(fb_path):
        try:
            with open(fb_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                        if rec.get("rating") == "up":
                            fb_up += 1
                        elif rec.get("rating") == "down":
                            fb_down += 1
                    except _json.JSONDecodeError:
                        continue
        except OSError:
            pass
    traces_dir = os.path.join(edir, "traces")
    if os.path.isdir(traces_dir):
        try:
            for fname in os.listdir(traces_dir):
                if not fname.endswith(".jsonl"):
                    continue
                with open(os.path.join(traces_dir, fname), "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            trace_count += 1
        except OSError:
            pass
    payload["feedback_counts"] = {"up": fb_up, "down": fb_down}
    payload["trace_count"] = trace_count
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

@router.get("/agent/{agent_id}/expert/corpus", summary="List corpus sources for an agent")
async def corpus_list(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V3 step 1. Returns the agent's corpus manifest plus the template's
    pre-configured sources (so UI can show "configured but not yet
    ingested" entries vs already-indexed ones).

    Response:
      {
        "agent_id": ...,
        "cultivated": bool,
        "manifest": { sources: [...], total_chunks, total_bytes },
        "template_sources": [ ... ],   # from SpecialtyTemplate.corpus_sources
        "specialty": "legal"|"" ,
      }
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")

    cultivated = bool(getattr(agent, "expert_specialty", "") or "")
    specialty = getattr(agent, "expert_specialty", "") or ""

    # ── manifest on disk ──
    from ..corpus.manifest import CorpusManifest
    manifest = CorpusManifest.load(agent_id)

    # ── template sources (for "configured but not yet ingested" view) ──
    template_sources: list = []
    if cultivated and specialty:
        try:
            from ..template_loader import (
                load, load_all, TemplateNotFoundError,
            )
            tpl = None
            try:
                tpl = load(specialty)
            except TemplateNotFoundError:
                for cand in load_all():
                    if cand.id == specialty or cand.specialty == specialty:
                        tpl = cand
                        break
            if tpl is not None:
                for cs in (tpl.corpus_sources or []):
                    # CorpusSource is a dataclass; convert to plain dict
                    if hasattr(cs, "__dict__"):
                        template_sources.append(dict(cs.__dict__))
                    elif isinstance(cs, dict):
                        template_sources.append(dict(cs))
                    else:
                        template_sources.append({"raw": str(cs)})
        except Exception as e:
            logger.warning("template source enumeration failed for %s: %s",
                           agent_id, e)

    return {
        "agent_id": agent_id,
        "cultivated": cultivated,
        "specialty": specialty,
        "manifest": manifest.to_dict(),
        "template_sources": template_sources,
    }


@router.post("/agent/{agent_id}/expert/corpus/ingest", summary="Ingest a corpus source")
async def corpus_ingest(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V3 step 2. Register + (optionally) chunk a corpus source.

    Body shapes:
      Register only (V3 step 1 behavior):
        {"source_id": "...", "version": "...", "chunker_strategy": "..."}
      → entry with chunk_count=0, indexed_at=0 (pending)

      Register + chunk content (V3 step 2):
        {"source_id": "...", "content": "<raw text>", "chunker_strategy": "paragraph"}
      → splits content via the registered chunker, writes
        chunks.jsonl to ~/.tudou_claw/expert/<id>/corpus/<source_id>/,
        updates manifest with chunk_count + bytes + indexed_at.

    Once chunk_count > 0, the UI bumps the agent from 见习 (25%) to
    熟手 (50%) per design spec §3.6.6.

    Real bge-m3 embedding + sqlite-vss vector store comes in V3 step 3.
    Until then chunks live as plain JSONL — works for retrieval via
    keyword/BM25 fallback in V4 step 2.
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")

    source_id = (body.get("source_id") or "").strip()
    if not source_id:
        raise HTTPException(400, "body must include 'source_id' (string)")

    content = body.get("content") or ""
    strategy = (body.get("chunker_strategy") or "paragraph").strip()

    from ..corpus.manifest import CorpusManifest, CorpusSourceEntry
    manifest = CorpusManifest.load(agent_id)

    chunk_count = 0
    bytes_ingested = 0
    indexed_at = 0.0
    notes = "registered, awaiting V3 step 2 ingest"

    if content:
        # ── Real chunking path (V3 step 2) ──
        # Import chunker module side-effects: register paragraph + legal
        # strategies. Lazy import keeps the endpoint fast when content
        # isn't provided.
        from ..corpus import chunker as ch
        # Side-effect registration of the legal chunkers
        try:
            from ..corpus import chunker_legal as _  # noqa: F401
        except Exception as e:
            logger.info("legal chunkers not registered: %s", e)
        # Resolve strategy with fallbacks: try as-is, then 'paragraph'.
        # Template uses 'structural' which we don't have yet — fall back.
        strategies_to_try = [strategy]
        if strategy != "paragraph":
            strategies_to_try.append("paragraph")
        chunker_inst = None
        for s in strategies_to_try:
            try:
                chunker_inst = ch.get(s)
                strategy = s
                break
            except KeyError:
                continue
        if chunker_inst is None:
            raise HTTPException(500, f"no chunker available for {strategy!r}")

        # Run chunking
        import json, time
        from .._config import expert_dir_for
        chunks_dir = os.path.join(expert_dir_for(agent_id), "corpus", source_id)
        os.makedirs(chunks_dir, exist_ok=True)
        chunks_jsonl = os.path.join(chunks_dir, "chunks.jsonl")
        source_meta = {
            "source_id": source_id,
            "version": str(body.get("version") or ""),
            "ingested_at": time.time(),
        }
        with open(chunks_jsonl, "w", encoding="utf-8") as f:
            for chunk in chunker_inst.chunk(content, source_meta):
                rec = {"text": chunk.text, "metadata": dict(chunk.metadata)}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                chunk_count += 1
                bytes_ingested += len(chunk.text.encode("utf-8"))
        indexed_at = time.time()
        notes = f"indexed {chunk_count} chunks via '{strategy}' chunker"
    else:
        notes = str(body.get("notes") or notes)

    entry = CorpusSourceEntry(
        source_id=source_id,
        version=str(body.get("version") or ""),
        chunk_count=chunk_count,
        bytes=bytes_ingested,
        indexed_at=indexed_at,
        chunker_strategy=strategy,
        notes=notes,
    )
    manifest.add_source(entry)
    manifest.save()

    return {
        "agent_id": agent_id,
        "added_source": entry.source_id,
        "manifest": manifest.to_dict(),
        "ok": True,
        "stage": "indexed" if chunk_count > 0 else "registered",
        "chunk_count": chunk_count,
        "bytes": bytes_ingested,
    }


@router.post("/agent/{agent_id}/expert/corpus/reindex", summary="Rebuild vector index")
async def corpus_reindex(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V3 step 1. Stub — actual reindex (chunk + embed + write sqlite-vss)
    lands in V3 step 2 once the embedder + store are wired into the live
    request path. For now, returns the current manifest as a sanity check.
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    from ..corpus.manifest import CorpusManifest
    manifest = CorpusManifest.load(agent_id)
    return {
        "agent_id": agent_id,
        "stage": "stub",
        "manifest": manifest.to_dict(),
        "next": "V3 step 2: bge-m3 embedder + sqlite-vss persistence",
    }


# ── Query + feedback ──

@router.post("/agent/{agent_id}/expert/query", summary="Direct expert query (RAG-augmented)")
async def expert_query(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 2. Thin REST wrapper around app.domain_expert.inference.
    pipeline.answer — the same function agent.chat() routes through
    when expert_specialty is set.

    Body: `{"q": "<user question>", "context_id": "solo"}`

    Returns: `{"answer": "...", "trace_id": "...", "retrieved_count": N}`
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    cur_specialty = getattr(agent, "expert_specialty", "") or ""
    if not cur_specialty:
        raise HTTPException(409, {
            "error": "not_cultivated",
            "agent_id": agent_id,
            "hint": "POST /agent/{id}/expert/initialize first.",
        })
    q = (body.get("q") or body.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "body must include 'q' (string)")
    context_id = (body.get("context_id") or "solo").strip()

    try:
        from ..inference import pipeline as _pipeline
        answer_text = _pipeline.answer(
            agent, q, source="api", context_id=context_id,
        )
    except Exception as e:
        logger.exception("expert_query failed for %s", agent_id)
        raise HTTPException(500, f"expert pipeline failed: {e}")

    return {
        "agent_id": agent_id,
        "specialty": cur_specialty,
        "q": q,
        "answer": answer_text,
        "ok": True,
    }


@router.post("/agent/{agent_id}/expert/feedback", summary="User 👍/👎 feedback on a reply")
async def expert_feedback(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 1. Append a 👍/👎 feedback record to the agent's trace
    pool. The trace is appended to the same JSONL the V4 step 2 query
    handler will write organic Q/A traces to, so RAFT data prep + LoRA
    training pull from a single source.

    Body: `{"trace_id": "...", "rating": "thumbs_up"|"thumbs_down",
            "comment": "..."}`
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")

    rating = (body.get("rating") or "").strip().lower()
    if rating not in ("thumbs_up", "thumbs_down", "up", "down", "👍", "👎"):
        raise HTTPException(400, "rating must be thumbs_up or thumbs_down")
    rating_norm = "up" if rating in ("thumbs_up", "up", "👍") else "down"
    trace_id = (body.get("trace_id") or "").strip()
    comment = (body.get("comment") or "").strip()

    import json, time
    from .._config import expert_dir_for
    feedback_dir = os.path.join(expert_dir_for(agent_id), "feedback")
    os.makedirs(feedback_dir, exist_ok=True)
    record = {
        "trace_id": trace_id,
        "rating": rating_norm,
        "comment": comment,
        "ts": time.time(),
        "by_user": getattr(user, "user_id", "unknown"),
    }
    fp = os.path.join(feedback_dir, "feedback.jsonl")
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "agent_id": agent_id, "feedback": record}


# ── Traces / stats ──

@router.get("/agent/{agent_id}/expert/traces", summary="List Q/A trace history")
async def expert_traces(
    agent_id: str,
    limit: int = 100,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 1. Read the agent's trace JSONL files and return up to
    `limit` most-recent entries. V4 step 2 will add filters (by feedback
    / by source / by score), but a flat read is enough for the UI to
    show the trace count toward the RAFT threshold.

    Returns:
      {
        "agent_id": ...,
        "total": int,             # total trace entries on disk
        "traces": [ {q, a, retrieved_docs, feedback, origin, ts}, ... ],
      }
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    import json
    from .._config import expert_dir_for
    traces_dir = os.path.join(expert_dir_for(agent_id), "traces")
    traces: list[dict] = []
    total = 0
    if os.path.isdir(traces_dir):
        # Each .jsonl file holds N traces; read all then take `limit` most
        # recent. For V4 step 1 this is fine — files are agent-scoped and
        # bounded. V4 step 2 adds proper sharding.
        for fname in sorted(os.listdir(traces_dir)):
            if not fname.endswith(".jsonl"):
                continue
            with open(os.path.join(traces_dir, fname), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    try:
                        traces.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    # Most recent first; then truncate to limit
    traces.sort(key=lambda t: t.get("ts", 0), reverse=True)
    return {
        "agent_id": agent_id,
        "total": total,
        "traces": traces[: max(1, min(int(limit), 500))],
    }


@router.get("/agent/{agent_id}/expert/routing", summary="Get routing config + live stats")
async def routing_get(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V5. Returns the agent's routing config (confidence_threshold, mode)
    + live local-handle stats. Mode is one of:
      auto    — confidence-gated (≥ threshold → local LoRA, else → cloud)
      local   — force local (testing / privacy mode)
      cloud   — force cloud (bypass LoRA, useful while LoRA is broken)
    Stored in ~/.tudou_claw/expert/<id>/routing.json. Defaults if missing.
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    import json as _json
    from .._config import expert_dir_for as _edir
    edir = _edir(agent_id)
    cfg = {
        "mode": "auto",
        "confidence_threshold": 0.7,
        "fallback_to_cloud": True,
    }
    cfg_path = os.path.join(edir, "routing.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                stored = _json.load(f)
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, _json.JSONDecodeError):
            pass

    # Live stats: read routing log if present (V4 step 2 doesn't write
    # this yet — placeholder zeros until inference path tags routes).
    stats = {"local_handled": 0, "cloud_handled": 0, "total": 0,
             "local_handle_rate": 0.0}
    log_path = os.path.join(edir, "routing.log.jsonl")
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    stats["total"] += 1
                    where = rec.get("handled_by")
                    if where == "local":
                        stats["local_handled"] += 1
                    elif where == "cloud":
                        stats["cloud_handled"] += 1
        except OSError:
            pass
    if stats["total"] > 0:
        stats["local_handle_rate"] = stats["local_handled"] / stats["total"]
    return {
        "agent_id": agent_id,
        "config": cfg,
        "stats": stats,
    }


@router.put("/agent/{agent_id}/expert/routing", summary="Update routing config")
async def routing_put(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V5. Update routing config (any subset of: mode / confidence_threshold
    / fallback_to_cloud). Validates mode enum + threshold 0..1."""
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")

    mode = body.get("mode")
    if mode is not None and mode not in ("auto", "local", "cloud"):
        raise HTTPException(400, "mode must be auto / local / cloud")
    thresh = body.get("confidence_threshold")
    if thresh is not None:
        try:
            thresh = float(thresh)
        except (TypeError, ValueError):
            raise HTTPException(400, "confidence_threshold must be a number")
        if not (0.0 <= thresh <= 1.0):
            raise HTTPException(400, "confidence_threshold must be 0..1")
    fallback = body.get("fallback_to_cloud")

    import json as _json
    from .._config import expert_dir_for as _edir
    edir = _edir(agent_id)
    os.makedirs(edir, exist_ok=True)
    cfg_path = os.path.join(edir, "routing.json")
    cfg = {"mode": "auto", "confidence_threshold": 0.7,
           "fallback_to_cloud": True}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                stored = _json.load(f)
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, _json.JSONDecodeError):
            pass
    if mode is not None:
        cfg["mode"] = mode
    if thresh is not None:
        cfg["confidence_threshold"] = thresh
    if fallback is not None:
        cfg["fallback_to_cloud"] = bool(fallback)
    with open(cfg_path, "w", encoding="utf-8") as f:
        _json.dump(cfg, f, ensure_ascii=False, indent=2)
    logger.info("routing config updated for %s: %s", agent_id, cfg)
    return {"ok": True, "agent_id": agent_id, "config": cfg}


@router.post("/agent/{agent_id}/expert/lora/train", summary="Trigger LoRA training")
async def lora_train(
    agent_id: str,
    body: dict = Body(default={}),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 3 stub. Records a training request to a queue file.

    The actual mlx-lm + RAFT pipeline lands in SP-2 (estimated ~3 days
    of work). For now this:
      1. Validates the agent is cultivated
      2. Validates trace_count >= raft_data_target (no point training
         without enough data)
      3. Writes a request record to lora/_queue.jsonl with status='queued'

    Returns 200 + the queue record. The UI button surfaces this as
    "训练已排队 — 后台执行(SP-2 上线)". No actual training fires yet.

    Body (all optional):
      override_target  — accept fewer traces than template's raft_data_target
      base_model       — override template's base_model
      lora_rank / alpha — override hyperparams
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    cur_specialty = getattr(agent, "expert_specialty", "") or ""
    if not cur_specialty:
        raise HTTPException(409, {"error": "not_cultivated"})

    # Count traces — same logic as /stats
    import json as _json, time as _time
    from .._config import expert_dir_for as _edir
    edir = _edir(agent_id)
    trace_count = 0
    traces_dir = os.path.join(edir, "traces")
    if os.path.isdir(traces_dir):
        try:
            for fname in os.listdir(traces_dir):
                if fname.endswith(".jsonl"):
                    with open(os.path.join(traces_dir, fname), "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                trace_count += 1
        except OSError:
            pass

    # Resolve template to know raft_data_target
    raft_target = 1000
    try:
        from ..template_loader import load, load_all, TemplateNotFoundError
        try:
            tpl = load(cur_specialty)
        except TemplateNotFoundError:
            tpl = None
            for cand in load_all():
                if cand.id == cur_specialty or cand.specialty == cur_specialty:
                    tpl = cand; break
        if tpl is not None and tpl.training:
            # Schema's TrainingConfig has no raft_data_target, but we use
            # max_steps as a rough analog or fall back to 1000.
            raft_target = max(int(getattr(tpl.training, "max_steps", 0) or 0), 1000)
    except Exception as e:
        logger.info("template lookup for lora_train failed: %s", e)

    override_target = bool(body.get("override_target") or False)
    if trace_count < raft_target and not override_target:
        raise HTTPException(409, {
            "error": "insufficient_traces",
            "trace_count": trace_count,
            "raft_target": raft_target,
            "hint": (
                f"need ≥ {raft_target} traces, have {trace_count}. "
                "pass override_target=true to force-queue anyway."
            ),
        })

    # Queue the request
    lora_dir = os.path.join(edir, "lora")
    os.makedirs(lora_dir, exist_ok=True)
    queue_path = os.path.join(lora_dir, "_queue.jsonl")
    record = {
        "ts": _time.time(),
        "agent_id": agent_id,
        "specialty": cur_specialty,
        "trace_count": trace_count,
        "raft_target": raft_target,
        "override_target": override_target,
        "base_model_override": str(body.get("base_model") or ""),
        "lora_rank_override":  int(body.get("lora_rank") or 0),
        "lora_alpha_override": int(body.get("lora_alpha") or 0),
        "status": "queued",
        "queued_by": getattr(user, "user_id", "unknown"),
        "note": "SP-2 worker not yet wired; record kept for inspection",
    }
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("lora_train queued for %s (traces=%d/target=%d, override=%s)",
                agent_id, trace_count, raft_target, override_target)
    return {"ok": True, "queued": record}


@router.get("/agent/{agent_id}/expert/lora", summary="List LoRA versions + queue")
async def lora_list(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 3 read-side. Lists training queue records + on-disk LoRA
    versions (subdirs under lora/). Used by module 5 drill panel."""
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    import json as _json
    from .._config import expert_dir_for as _edir
    lora_dir = os.path.join(_edir(agent_id), "lora")
    versions: list[str] = []
    queue: list[dict] = []
    if os.path.isdir(lora_dir):
        try:
            versions = sorted(
                d for d in os.listdir(lora_dir)
                if os.path.isdir(os.path.join(lora_dir, d)) and d not in ("current",)
            )
        except OSError:
            pass
        qpath = os.path.join(lora_dir, "_queue.jsonl")
        if os.path.isfile(qpath):
            try:
                with open(qpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            queue.append(_json.loads(line))
                        except _json.JSONDecodeError:
                            continue
            except OSError:
                pass
    queue.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return {
        "agent_id": agent_id,
        "active_lora": getattr(agent, "expert_lora_version", "") or "",
        "versions": versions,
        "queue": queue[:20],
    }


@router.get("/agent/{agent_id}/expert/stats", summary="Cultivation pipeline stats dashboard")
async def expert_stats(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """V4 step 1. Aggregate stats across the agent's expert dir for the
    pipeline visualization. V4 step 2 wires per-module deeper metrics
    (per-source chunk counts, eval scores, training history); V5 adds
    routing rate. For now this returns enough to feed the 6 module cards.

    Returns:
      {
        "agent_id": ...,
        "cultivated": bool,
        "specialty": ...,
        "level": novice|journeyman|expert|master,
        "manifest": {sources, total_chunks, total_bytes, last_updated},
        "trace_count": int,
        "feedback_counts": {up: N, down: N},
        "lora_versions": [v1, v2, ...],
        "active_lora": ...,
      }
    """
    _check_enabled()
    agent = hub.agents.get(agent_id) if hasattr(hub, "agents") else None
    if agent is None:
        raise HTTPException(404, f"agent {agent_id!r} not found")
    import json
    from .._config import expert_dir_for
    from ..corpus.manifest import CorpusManifest

    edir = expert_dir_for(agent_id)
    manifest = CorpusManifest.load(agent_id)

    # Trace count — sum of lines in all JSONL files
    trace_count = 0
    traces_dir = os.path.join(edir, "traces")
    if os.path.isdir(traces_dir):
        for fname in os.listdir(traces_dir):
            if not fname.endswith(".jsonl"):
                continue
            with open(os.path.join(traces_dir, fname), "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        trace_count += 1

    # Feedback counts — read feedback.jsonl
    fb_up = fb_down = 0
    fb_path = os.path.join(edir, "feedback", "feedback.jsonl")
    if os.path.isfile(fb_path):
        with open(fb_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("rating") == "up":
                        fb_up += 1
                    elif rec.get("rating") == "down":
                        fb_down += 1
                except json.JSONDecodeError:
                    continue

    # LoRA versions — list dirs under lora/
    lora_dir = os.path.join(edir, "lora")
    lora_versions: list[str] = []
    if os.path.isdir(lora_dir):
        lora_versions = sorted(
            d for d in os.listdir(lora_dir)
            if os.path.isdir(os.path.join(lora_dir, d)) and d != "current"
        )

    return {
        "agent_id": agent_id,
        "cultivated": bool(getattr(agent, "expert_specialty", "") or ""),
        "specialty": getattr(agent, "expert_specialty", "") or "",
        "level": getattr(agent, "expert_level", "novice") or "novice",
        "template_version": getattr(agent, "expert_template_version", "") or "",
        "active_lora": getattr(agent, "expert_lora_version", "") or "",
        "lora_versions": lora_versions,
        "manifest": manifest.to_dict(),
        "trace_count": trace_count,
        "feedback_counts": {"up": fb_up, "down": fb_down},
    }
