"""LLM Provider management router — providers, models, detection."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.providers")

router = APIRouter(prefix="/api/portal", tags=["providers"])


def _get_registry():
    from ...llm import get_registry
    return get_registry()


# ---------------------------------------------------------------------------
# Provider listing — matches legacy portal_routes_get
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all registered LLM providers."""
    try:
        reg = _get_registry()
        providers = reg.list(include_disabled=True)
        return {"providers": [p.to_dict(mask_key=True) for p in providers]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Register a new provider — matches legacy handlers/providers.py
# ---------------------------------------------------------------------------

@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Remove an LLM provider. Parity with legacy ``DELETE /api/portal/providers/{id}``."""
    reg = _get_registry()
    ok = reg.remove(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"provider {provider_id!r} not found")
    try:
        from ...auth import get_auth
        get_auth().audit("delete_provider", actor=user.user_id,
                         role=user.role, target=provider_id)
    except Exception:
        pass
    return {"ok": True, "provider_id": provider_id}


@router.post("/providers")
async def register_provider(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Register a new LLM provider."""
    try:
        reg = _get_registry()
        p = reg.add(
            name=body.get("name", ""),
            kind=body.get("kind", "openai"),
            base_url=body.get("base_url", ""),
            api_key=body.get("api_key", ""),
            enabled=body.get("enabled", True),
            manual_models=body.get("manual_models"),
            scope=body.get("scope", "local"),
            max_concurrent=max(1, int(body.get("max_concurrent", 1))),
            schedule_strategy=body.get("schedule_strategy", "serial"),
            rate_limit_rpm=max(0, int(body.get("rate_limit_rpm", 0))),
        )
        # Set models_cache from manual_models
        if body.get("manual_models"):
            p.models_cache = list(body.get("manual_models", []))
            reg._save()
        return p.to_dict(mask_key=True)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Provider models
# ---------------------------------------------------------------------------

@router.get("/providers/{provider_id}/models")
async def get_provider_models(
    provider_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get available models from a provider."""
    try:
        reg = _get_registry()
        models = reg.detect_models(provider_id)
        return {"provider_id": provider_id, "models": models}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Provider update — matches legacy handlers/providers.py
# ---------------------------------------------------------------------------

@router.post("/providers/{provider_id}/update")
async def update_provider(
    provider_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update provider configuration."""
    try:
        reg = _get_registry()
        kwargs = {}
        for k in ("name", "kind", "base_url", "api_key", "enabled", "manual_models"):
            if k in body:
                # Don't overwrite api_key with mask
                if k == "api_key" and body[k] == "********":
                    continue
                kwargs[k] = body[k]
        p = reg.update(provider_id, **kwargs)
        if not p:
            raise HTTPException(status_code=404, detail="Provider not found")

        # Concurrency & scheduling fields
        changed = False
        if "max_concurrent" in body:
            p.max_concurrent = max(1, int(body["max_concurrent"]))
            changed = True
        if "model_concurrency" in body:
            mc = body["model_concurrency"]
            p.model_concurrency = {k: int(v) for k, v in (mc or {}).items() if int(v) > 0}
            changed = True
        if "schedule_strategy" in body:
            p.schedule_strategy = body["schedule_strategy"]
            changed = True
        if "rate_limit_rpm" in body:
            p.rate_limit_rpm = max(0, int(body["rate_limit_rpm"]))
            changed = True
        if "scope" in body:
            p.scope = body["scope"]
            changed = True
        if "priority" in body:
            p.priority = int(body["priority"])
            changed = True
        if "cost_per_1k_tokens" in body:
            p.cost_per_1k_tokens = float(body["cost_per_1k_tokens"])
            changed = True
        if "context_length" in body:
            p.context_length = max(0, int(body["context_length"]))
            changed = True
        # Sync models_cache from manual_models
        if "manual_models" in body:
            wanted = list(body.get("manual_models") or [])
            p.manual_models = wanted
            p.models_cache = list(set(wanted))
            changed = True
        if changed:
            reg._save()

        return p.to_dict(mask_key=True)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Model detection — matches legacy handlers/providers.py
# ---------------------------------------------------------------------------

@router.post("/providers/{provider_id}/detect")
async def detect_provider_models(
    provider_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Detect available models from a provider."""
    try:
        reg = _get_registry()
        models = reg.detect_models(provider_id)
        return {"provider_id": provider_id, "models": models}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/providers/detect-all")
async def detect_all_models(
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Detect models from all providers."""
    try:
        reg = _get_registry()
        all_models = reg.detect_all_models()
        return {"models": all_models}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
