"""LLM Tier Routing API — 档位→provider/model 映射管理。

端点：
  GET    /api/admin/llm_tiers             列出所有档位映射 + 已配置的 provider/model
  GET    /api/admin/llm_tiers/catalog     获取标准档位定义（名称/中文标签/描述/建议）
  POST   /api/admin/llm_tiers/{tier}      upsert 单个档位映射
  DELETE /api/admin/llm_tiers/{tier}      删除映射
  POST   /api/admin/llm_tiers/autofill    触发智能预填（force=true 可覆盖已有）
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, Body

from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.llm_tiers")

router = APIRouter(prefix="/api/admin/llm_tiers", tags=["llm_tiers"])


def _require_admin(user: CurrentUser) -> None:
    if not getattr(user, "is_super_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("")
async def list_tiers(user: CurrentUser = Depends(get_current_user)):
    """列出所有档位映射。普通用户也可查看（只读）。"""
    from ...llm_tier_routing import (
        get_router, STANDARD_TIERS, TIER_LABELS_ZH, TIER_DESCRIPTIONS_ZH,
    )
    from ... import llm
    router_ = get_router()
    try:
        providers = llm.list_providers()
        models_by_provider = llm.list_available_models()
    except Exception:
        providers, models_by_provider = [], {}

    # Build provider_info with display names (id + human-readable name + kind)
    # so UI can show names instead of raw IDs.
    provider_info = []
    try:
        reg = llm.get_registry()
        for pid in providers:
            entry = reg.get(pid) if hasattr(reg, "get") else None
            if entry is not None:
                provider_info.append({
                    "id": entry.id,
                    "name": entry.name or entry.id,
                    "kind": getattr(entry, "kind", ""),
                })
            else:
                provider_info.append({"id": pid, "name": pid, "kind": ""})
    except Exception:
        provider_info = [{"id": p, "name": p, "kind": ""} for p in providers]

    tiers = []
    for tier in STANDARD_TIERS:
        entry = router_.get(tier)
        tiers.append({
            "tier": tier,
            "label_zh": TIER_LABELS_ZH.get(tier, tier),
            "description_zh": TIER_DESCRIPTIONS_ZH.get(tier, ""),
            "configured": entry is not None,
            "provider": entry.provider if entry else "",
            "model": entry.model if entry else "",
            "enabled": entry.enabled if entry else False,
            "fallback_tier": entry.fallback_tier if entry else "",
            "cost_hint": entry.cost_hint if entry else "medium",
            "note": entry.note if entry else "",
        })
    # 也包含管理员自定义档位（不在 STANDARD_TIERS 但有映射）
    for tier, entry in router_.all().items():
        if tier in STANDARD_TIERS:
            continue
        tiers.append({
            "tier": tier,
            "label_zh": tier,
            "description_zh": entry.note or "（自定义档位）",
            "configured": True,
            "provider": entry.provider,
            "model": entry.model,
            "enabled": entry.enabled,
            "fallback_tier": entry.fallback_tier,
            "cost_hint": entry.cost_hint,
            "note": entry.note,
            "custom": True,
        })
    return {
        "tiers": tiers,
        "available_providers": providers,
        "provider_info": provider_info,
        "available_models": models_by_provider,
    }


@router.get("/catalog")
async def tier_catalog(user: CurrentUser = Depends(get_current_user)):
    """获取标准档位目录（前端 UI 用）。"""
    from ...llm_tier_routing import STANDARD_TIERS, TIER_LABELS_ZH, TIER_DESCRIPTIONS_ZH
    return {
        "standard_tiers": [
            {"tier": t, "label_zh": TIER_LABELS_ZH.get(t, t),
             "description_zh": TIER_DESCRIPTIONS_ZH.get(t, "")}
            for t in STANDARD_TIERS
        ],
    }


@router.post("/{tier}")
async def upsert_tier(
    tier: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """Upsert 单个档位映射。"""
    _require_admin(user)
    from ...llm_tier_routing import LLMTierEntry, get_router
    if not tier:
        raise HTTPException(status_code=400, detail="tier required")
    entry = LLMTierEntry(
        tier=tier,
        provider=str(body.get("provider", "")).strip(),
        model=str(body.get("model", "")).strip(),
        fallback_tier=str(body.get("fallback_tier", "")).strip(),
        enabled=bool(body.get("enabled", True)),
        cost_hint=str(body.get("cost_hint", "medium")),
        note=str(body.get("note", "")),
    )
    if not entry.provider or not entry.model:
        raise HTTPException(status_code=400, detail="provider and model required")
    r = get_router()
    r.set(tier, entry)
    try:
        r.save()
    except Exception as e:
        logger.warning("save failed: %s", e)
    return {"ok": True, "tier": tier, "entry": entry.to_dict()}


@router.delete("/{tier}")
async def delete_tier(
    tier: str,
    user: CurrentUser = Depends(get_current_user),
):
    """删除档位映射。"""
    _require_admin(user)
    from ...llm_tier_routing import get_router
    r = get_router()
    ok = r.remove(tier)
    try:
        r.save()
    except Exception:
        pass
    return {"ok": ok, "tier": tier}


@router.post("/autofill")
async def autofill_tiers(
    body: dict = Body(default={}),
    user: CurrentUser = Depends(get_current_user),
):
    """智能预填默认映射。

    Body:
      force: bool — True 则覆盖已有映射（默认 False，只填未配置的档位）
    """
    _require_admin(user)
    from ...llm_tier_routing import get_router
    force = bool(body.get("force", False))
    r = get_router()
    added = r.autofill_defaults(force=force)
    return {"ok": True, "added": added, "force": force}
