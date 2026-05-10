"""TTS provider management + on-demand speech synthesis.

Endpoints under /api/portal/tts:
  GET    /providers          — list all configured providers (api_key masked)
  POST   /providers          — add a provider
  PATCH  /providers/{id}     — update fields
  DELETE /providers/{id}     — remove a provider
  GET    /active             — current active provider id (or empty string)
  POST   /active             — set active provider id (body: {provider_id})
  POST   /synthesize         — synthesize text → audio/mpeg bytes
                               (body: {text, voice?, lang?, speed?, provider_id?})
  POST   /test               — same as /synthesize but accepts an inline
                               provider config (no save) for the "Test"
                               button in the admin UI.

The frontend reads /active on page load: if non-empty, voice playback
goes through /synthesize; if empty, it falls back to browser Web
Speech API (window.speechSynthesis) — same as before.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from ..deps.auth import CurrentUser, get_current_user
from ..deps.hub import get_hub

logger = logging.getLogger("tudouclaw.api.tts")

router = APIRouter(prefix="/api/portal/tts", tags=["tts"])


def _get_registry():
    from ...tts_providers import get_tts_registry
    return get_tts_registry()


# ── list / CRUD ────────────────────────────────────────────────────

@router.get("/providers")
async def list_tts_providers(
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    reg = _get_registry()
    items = [p.to_dict(mask_key=True) for p in reg.list()]
    from ...tts_providers import get_active_provider_id
    return {"providers": items, "active_provider_id": get_active_provider_id()}


@router.post("/providers")
async def create_tts_provider(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    name = (body.get("name") or "").strip()
    kind = (body.get("kind") or "").strip().lower()
    if not name:
        raise HTTPException(400, "name is required")
    if kind not in ("openai", "elevenlabs", "azure", "edge", "browser"):
        raise HTTPException(
            400,
            f"kind must be one of openai/elevenlabs/azure/edge/browser, got {kind!r}",
        )
    try:
        p = _get_registry().add(
            name=name, kind=kind,
            base_url=body.get("base_url") or "",
            api_key=body.get("api_key") or "",
            voice=body.get("voice") or "",
            model=body.get("model") or "",
            lang=body.get("lang") or "zh-CN",
            speed=float(body.get("speed") or 1.0),
            enabled=bool(body.get("enabled", True)),
            extra=body.get("extra") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info("tts provider added: %s (%s) by %s", p.name, p.kind, user.user_id)
    return {"ok": True, "provider": p.to_dict()}


@router.patch("/providers/{provider_id}")
async def update_tts_provider(
    provider_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    reg = _get_registry()
    # Only forward keys the registry knows about
    allowed = {"name", "base_url", "api_key", "voice", "model",
               "lang", "speed", "enabled", "extra"}
    fields = {k: v for k, v in body.items() if k in allowed}
    # Preserve existing api_key if caller sends a masked placeholder
    if "api_key" in fields:
        v = (fields["api_key"] or "").strip()
        if not v or "****" in v:
            fields.pop("api_key")
    p = reg.update(provider_id, **fields)
    if p is None:
        raise HTTPException(404, f"provider {provider_id!r} not found")
    return {"ok": True, "provider": p.to_dict()}


@router.delete("/providers/{provider_id}")
async def delete_tts_provider(
    provider_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    ok = _get_registry().remove(provider_id)
    if not ok:
        raise HTTPException(404, f"provider {provider_id!r} not found")
    # If the removed provider was active, clear the active flag too.
    from ...tts_providers import get_active_provider_id, set_active_provider_id
    if get_active_provider_id() == provider_id:
        set_active_provider_id("")
    return {"ok": True}


# ── active provider ──────────────────────────────────────────────

@router.get("/active")
async def get_active(
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    from ...tts_providers import get_active_provider_id, get_active_provider
    pid = get_active_provider_id()
    p = get_active_provider() if pid else None
    return {
        "active_provider_id": pid,
        "provider": p.to_dict() if p else None,
        # Frontend uses this to decide whether to call /synthesize
        # or fall back to window.speechSynthesis directly.
        "use_server_tts": bool(p and p.kind != "browser" and p.enabled),
    }


@router.post("/active")
async def set_active(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    pid = (body.get("provider_id") or "").strip()
    if pid:
        p = _get_registry().get(pid)
        if p is None:
            raise HTTPException(404, f"provider {pid!r} not found")
    from ...tts_providers import set_active_provider_id
    set_active_provider_id(pid)
    return {"ok": True, "active_provider_id": pid}


# ── synthesis ────────────────────────────────────────────────────

@router.post("/synthesize")
async def synthesize_audio(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Synthesize text via an LLM Provider (reused for TTS).

    The provider_id references an entry in the LLM Provider registry
    (``app.llm.get_registry()`` — same one populating the LLM dropdown).
    We assume the provider's ``base_url`` speaks the OpenAI-compatible
    /audio/speech protocol: works for OpenAI, Groq, and any local
    server that mimics the schema. ElevenLabs/Azure/Edge use different
    auth + paths and are NOT supported by this code path — to add them
    later, branch on provider.kind or add a per-provider TTS adapter.
    """
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 4000:
        text = text[:4000]
    from ...tts_providers import TTSProvider, synthesize, TTSError
    pid = (body.get("provider_id") or "").strip()
    SENTINEL_VOXCPM = "__local_voxcpm__"
    SENTINEL_EDGE = "__edge_tts__"
    if pid == SENTINEL_VOXCPM:
        # ── Route 1a: explicit local VoxCPM ──
        tts_view = TTSProvider(
            id=SENTINEL_VOXCPM, name="VoxCPM (local)", kind="voxcpm",
            voice=(body.get("voice") or ""),
            model=(body.get("model") or "openbmb/VoxCPM2"),
            lang=(body.get("lang") or "zh-CN"),
            speed=float(body.get("speed") or 1.0),
            enabled=True,
        )
    elif pid == SENTINEL_EDGE:
        # ── Route 1b: Microsoft Edge TTS — free, no API key ──
        # Voice list: zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural /
        # zh-CN-YunxiaNeural / zh-CN-XiaoyiNeural / en-US-AriaNeural / etc.
        tts_view = TTSProvider(
            id=SENTINEL_EDGE, name="Edge TTS (free)", kind="edge",
            voice=(body.get("voice") or "zh-CN-XiaoxiaoNeural"),
            lang=(body.get("lang") or "zh-CN"),
            speed=float(body.get("speed") or 1.0),
            enabled=True,
        )
    elif pid:
        # ── Route 2: cloud TTS via an LLM Provider (OpenAI-compat) ──
        try:
            from ...llm import get_registry as _get_llm_registry
            llm_reg = _get_llm_registry()
            llm_p = llm_reg.get(pid)
        except Exception as e:
            raise HTTPException(500, f"LLM registry unavailable: {e}")
        if llm_p is None:
            raise HTTPException(404, f"provider {pid!r} not found in LLM registry")
        tts_view = TTSProvider(
            id=llm_p.id, name=llm_p.name, kind="openai",
            base_url=getattr(llm_p, "base_url", "") or "",
            api_key=getattr(llm_p, "api_key", "") or "",
            voice=(body.get("voice") or "alloy"),
            model=(body.get("model") or "tts-1"),
            lang=(body.get("lang") or "zh-CN"),
            speed=float(body.get("speed") or 1.0),
            enabled=True,
        )
    else:
        # provider_id="" means browser TTS — frontend should NOT call
        # this endpoint in that case. If we got here, it's a bug.
        raise HTTPException(
            400,
            "provider_id is required. Empty value = browser Web Speech "
            "(handled client-side); use '__local_voxcpm__' for local "
            "VoxCPM, or an LLM provider id for cloud TTS.",
        )
    try:
        audio, mime = synthesize(
            tts_view, text,
            voice=body.get("voice") or "",
            lang=body.get("lang") or "",
            speed=float(body["speed"]) if "speed" in body else None,
        )
    except TTSError as e:
        # 503 (Service Unavailable) for built-in / local-style backends
        # so the frontend can distinguish "engine not available → fall
        # back to browser" from "explicitly-chosen cloud provider
        # broke → show error".
        if pid in (SENTINEL_VOXCPM, SENTINEL_EDGE):
            raise HTTPException(503, f"local TTS unavailable: {e}")
        raise HTTPException(502, f"TTS synthesis failed: {e}")
    except Exception as e:
        logger.exception("TTS synthesize unexpected error")
        raise HTTPException(500, f"unexpected error: {e}")
    return Response(
        content=audio, media_type=mime,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/test")
async def test_provider_inline(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Synthesize using an inline provider config — used by the admin
    UI's "Test" button before saving. Body shape:
       {kind, base_url?, api_key?, voice?, model?, lang?, speed?, text?}
    """
    from ...tts_providers import TTSProvider, synthesize, TTSError
    kind = (body.get("kind") or "").strip().lower()
    if kind not in ("openai", "elevenlabs", "azure", "edge"):
        raise HTTPException(400, f"kind must be openai/elevenlabs/azure/edge, got {kind!r}")
    text = (body.get("text") or "Hello, this is a test of TTS.").strip()
    p = TTSProvider(
        id="__test__", name="__test__", kind=kind,
        base_url=(body.get("base_url") or "").rstrip("/"),
        api_key=body.get("api_key") or "",
        voice=body.get("voice") or "",
        model=body.get("model") or "",
        lang=body.get("lang") or "zh-CN",
        speed=float(body.get("speed") or 1.0),
        enabled=True,
    )
    try:
        audio, mime = synthesize(p, text)
    except TTSError as e:
        raise HTTPException(502, f"TTS test failed: {e}")
    return Response(
        content=audio, media_type=mime,
        headers={"Cache-Control": "no-store"},
    )
