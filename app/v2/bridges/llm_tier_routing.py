"""
V2 tier resolution — thin adapter over the repo's LLMTierRouter.

Historical context: V2 originally shipped its own
``ProviderEntry.tier_models`` mechanism. TudouClaw_new already ships a
richer router (``app.llm_tier_routing.LLMTierRouter``) that supports:

    * fallback_tier chains
    * per-tier cost_hint / enabled / note
    * dedicated persistence (``llm_tiers.json``)

Rather than maintain two routing tables, V2 delegates to it. Anyone
configuring tier → provider/model mappings for V2 agents does so
through the same UI / REST / JSON file the rest of the platform uses.

Public API (unchanged by design):

    resolve_tier(tier)  → (provider_id, model)
    known_tiers()       → list[str]
"""
from __future__ import annotations

import logging
import os
from typing import Tuple


logger = logging.getLogger("tudouclaw.v2.tier_routing")


KNOWN_TIERS: list[str] = [
    "default",
    "reasoning_strong",
    "coding_strong",
    "writing_strong",
    "fast_cheap",
    "multimodal",
    "vision",
    "domain_specific",
]


def resolve_tier(tier: str) -> Tuple[str, str]:
    """Return ``(provider_id, model)`` for a tier, or ``("", "")``
    to fall through to V1's configured default.

    Delegation order:
        1. Env escape hatch: ``TUDOU_LLM_TIER_<TIER>="provider:model"``
        2. The repo-wide ``LLMTierRouter`` (reads ``llm_tiers.json``).
        3. Back-compat: ``ProviderEntry.tier_models`` on a registered provider.
        4. Fall-through ``("", "")``.

    Never raises.
    """
    key = (tier or "").strip()
    if not key:
        return ("", "")

    # 1. Main router (LLMTierRouter from app.llm_tier_routing).
    try:
        from app import llm_tier_routing as _router_mod
        router = _router_mod.get_router()
        provider, model = router.resolve(key)
        if provider and model:
            return (provider, model)
    except Exception as e:  # noqa: BLE001
        logger.debug("LLMTierRouter resolve failed for %r: %s", key, e)

    # 2. Legacy provider-level ``tier_models`` (pre-router configs).
    try:
        from app import llm as _llm
        picker = getattr(_llm.get_registry(), "pick_for_tier", None)
        if callable(picker):
            picked = picker(key)
            if picked is not None:
                entry, model = picked
                return (entry.id, model)
    except Exception:
        pass

    # 3. Env escape hatch (headless CI / tests). Lowest priority so a
    #    real UI-configured binding always wins over a stray env var.
    env_val = os.environ.get(
        "TUDOU_LLM_TIER_" + key.upper(), ""
    ).strip()
    if env_val and ":" in env_val:
        p, _, m = env_val.partition(":")
        return (p.strip(), m.strip())

    return ("", "")


def known_tiers() -> list[str]:
    """Return tier names we know about, plus any custom ones declared
    via the main router or provider-level tier_models dicts."""
    out = set(KNOWN_TIERS)
    try:
        from app import llm_tier_routing as _router_mod
        router = _router_mod.get_router()
        mapping = getattr(router, "_map", None) or {}
        for t in mapping.keys():
            if t:
                out.add(t)
    except Exception:
        pass
    try:
        from app import llm as _llm
        for p in _llm.get_registry().list(include_disabled=False):
            for t in (getattr(p, "tier_models", {}) or {}).keys():
                if t:
                    out.add(t)
    except Exception:
        pass
    return sorted(out)


__all__ = ["resolve_tier", "known_tiers", "KNOWN_TIERS"]
