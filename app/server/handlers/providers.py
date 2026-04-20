"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
LLM provider management POST handlers.

Extracted from portal_routes_post.py — handles:
  - POST /api/portal/providers           (add provider)
  - POST /api/portal/providers/{id}/update  (update provider)
  - POST /api/portal/providers/{id}/detect  (detect models for provider)
  - POST /api/portal/providers/detect-all   (detect all models)
"""
import logging

from ...llm import get_registry
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


def try_handle(handler, path: str, hub, body: dict, auth,
               actor_name: str, user_role: str) -> bool:
    """Handle authenticated provider-management endpoints.

    Returns True if the path was handled, False otherwise.
    """

    # ---- POST /api/portal/providers  (add) ----
    if path == "/api/portal/providers":
        reg = get_registry()
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
        auth.audit("add_provider", actor=actor_name, role=user_role,
                   target=p.id, ip=get_client_ip(handler))
        handler._json(p.to_dict(mask_key=True))
        return True

    # ---- POST /api/portal/providers/{id}/update ----
    if path.startswith("/api/portal/providers/") and path.endswith("/update"):
        provider_id = path.split("/")[4]
        reg = get_registry()
        kwargs = {}
        for k in ("name", "kind", "base_url", "api_key", "enabled", "manual_models"):
            if k in body:
                # Don't overwrite api_key with mask
                if k == "api_key" and body[k] == "********":
                    continue
                kwargs[k] = body[k]
        p = reg.update(provider_id, **kwargs)
        if p:
            # ── Concurrency & scheduling fields ──
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
            # The edit UI shows union of manual_models + models_cache as
            # editable tags. So what the user sends back IS the full model
            # list they want.  Sync models_cache accordingly.
            if "manual_models" in body:
                wanted = list(body.get("manual_models") or [])
                p.manual_models = wanted
                p.models_cache = list(set(wanted))
                changed = True
            if changed:
                reg._save()
            auth.audit("update_provider", actor=actor_name, role=user_role,
                       target=provider_id, ip=get_client_ip(handler))
            handler._json(p.to_dict(mask_key=True))
        else:
            handler._json({"error": "Provider not found"}, 404)
        return True

    # ---- POST /api/portal/providers/{id}/detect ----
    if path.startswith("/api/portal/providers/") and path.endswith("/detect"):
        provider_id = path.split("/")[4]
        reg = get_registry()
        models = reg.detect_models(provider_id)
        handler._json({"provider_id": provider_id, "models": models})
        return True

    # ---- POST /api/portal/providers/detect-all ----
    if path == "/api/portal/providers/detect-all":
        reg = get_registry()
        all_models = reg.detect_all_models()
        handler._json({"models": all_models})
        return True

    return False
