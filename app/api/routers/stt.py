"""Local + third-party STT — backend selection via system_settings.

Supported engines (read from `system_settings: stt.engine`):
  - browser                  Frontend uses webkitSpeechRecognition;
                             this router is not called for that path,
                             only the health probe reports it.
  - funasr                   FunASR paraformer-zh (Alibaba/DAMO).
                             Best Chinese accuracy. ~2-3s on Mac MPS.
  - mlx_whisper              Apple MLX whisper-large-v3-turbo. Apple
                             Silicon optimized. ~0.5-1s/utterance.
  - __provider__:<llm_id>    Cloud STT via an existing LLM Provider
                             registry entry — assumes its base_url
                             speaks OpenAI-compatible
                             /audio/transcriptions (works for OpenAI,
                             Groq, DeepInfra, etc.). Mirrors the same
                             "reuse LLM Provider" pattern used by
                             /api/portal/tts/synthesize.

Endpoints:
    GET  /api/portal/stt/health      → {engine, ok, loaded, load_failed}
    POST /api/portal/stt/transcribe  → {text, duration_ms, engine}
"""
from __future__ import annotations

import io
import logging
import threading
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form

from ..deps.auth import CurrentUser, get_current_user
from ..deps.hub import get_hub

logger = logging.getLogger("tudouclaw.api.stt")
router = APIRouter(prefix="/api/portal/stt", tags=["stt"])

# Per-engine singletons + load-failure cache
_FUNASR_MODEL = None
_FUNASR_LOAD_FAILED = False
_MLX_WHISPER_REPO_LOADED = ""  # "" = not loaded; HF id = loaded for that repo
_MLX_WHISPER_LOAD_FAILED = False

_LOAD_LOCK = threading.Lock()


PROVIDER_PREFIX = "__provider__:"


def _get_engine_setting() -> str:
    """Resolve current STT engine from system settings. Default 'funasr'.

    Accepted forms:
      - "browser" / "funasr" / "mlx_whisper"           (built-ins)
      - "__provider__:<llm_provider_id>"               (cloud via LLM reg)
    """
    try:
        from ...system_settings import get_store
        store = get_store()
        if store is None:
            return "funasr"
        raw = (store.get("stt.engine") or "").strip()
        v = raw.lower()
        if v in ("browser", "funasr", "mlx_whisper"):
            return v
        # Provider sentinel: keep original casing for the id portion
        if raw.startswith(PROVIDER_PREFIX):
            return raw
    except Exception as e:
        logger.warning("Failed to read stt.engine setting: %s", e)
    return "funasr"


def _resolve_llm_provider(engine: str):
    """If engine is `__provider__:<id>`, look up the LLM provider entry.
    Returns (provider_entry, provider_id) or (None, "") for built-ins."""
    if not engine.startswith(PROVIDER_PREFIX):
        return None, ""
    pid = engine[len(PROVIDER_PREFIX):].strip()
    if not pid:
        return None, ""
    try:
        from ...llm import get_registry as _get_llm_registry
        llm_reg = _get_llm_registry()
        return llm_reg.get(pid), pid
    except Exception as e:
        logger.warning("LLM registry lookup failed for stt provider: %s", e)
        return None, pid


def _get_mlx_whisper_repo() -> str:
    try:
        from ...system_settings import get_store
        store = get_store()
        if store is None:
            return "mlx-community/whisper-large-v3-turbo"
        return (store.get("stt.mlx_whisper_repo") or
                "mlx-community/whisper-large-v3-turbo").strip()
    except Exception:
        return "mlx-community/whisper-large-v3-turbo"


# ── Engine: FunASR ────────────────────────────────────────────────
def _get_funasr_model():
    global _FUNASR_MODEL, _FUNASR_LOAD_FAILED
    if _FUNASR_LOAD_FAILED:
        raise HTTPException(503, "funasr previously failed to load")
    with _LOAD_LOCK:
        if _FUNASR_MODEL is None:
            try:
                from funasr import AutoModel
            except ImportError as e:
                _FUNASR_LOAD_FAILED = True
                raise HTTPException(503, f"funasr not installed: {e}")
            import os as _os
            no_punc = _os.environ.get("TUDOU_FUNASR_NO_PUNC", "1") == "1"
            no_vad = _os.environ.get("TUDOU_FUNASR_NO_VAD", "1") == "1"
            kwargs = {"model": "paraformer-zh", "disable_update": True}
            if not no_vad:
                kwargs["vad_model"] = "fsmn-vad"
            if not no_punc:
                kwargs["punc_model"] = "ct-punc"
            try:
                logger.info("Loading FunASR paraformer-zh%s%s…",
                            " + VAD" if not no_vad else "",
                            " + punc" if not no_punc else "")
                _FUNASR_MODEL = AutoModel(**kwargs)
                logger.info("FunASR loaded")
            except Exception as e:
                _FUNASR_LOAD_FAILED = True
                logger.exception("FunASR load failed")
                raise HTTPException(503, f"funasr load failed: {e}")
    return _FUNASR_MODEL


def _funasr_transcribe(wav_path: str) -> str:
    model = _get_funasr_model()
    try:
        res = model.generate(input=wav_path, batch_size_s=300)
    except Exception as e:
        logger.exception("FunASR generate failed")
        raise HTTPException(502, f"transcribe failed: {e}")
    if res and isinstance(res, list) and res:
        item = res[0]
        if isinstance(item, dict):
            return (item.get("text") or "").strip()
        return str(item).strip()
    return ""


# ── Engine: mlx-whisper (Apple Silicon native) ────────────────────
def _ensure_mlx_whisper_loaded():
    """mlx-whisper doesn't expose a model object; loading happens on
    every transcribe call. We pre-warm by importing and (optionally)
    pre-fetching the model weights via huggingface_hub.
    """
    global _MLX_WHISPER_REPO_LOADED, _MLX_WHISPER_LOAD_FAILED
    if _MLX_WHISPER_LOAD_FAILED:
        raise HTTPException(503, "mlx-whisper previously failed to load")
    repo = _get_mlx_whisper_repo()
    if _MLX_WHISPER_REPO_LOADED == repo:
        return repo
    with _LOAD_LOCK:
        if _MLX_WHISPER_REPO_LOADED == repo:
            return repo
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as e:
            _MLX_WHISPER_LOAD_FAILED = True
            raise HTTPException(
                503,
                f"mlx-whisper not installed: {e}. "
                f"Run: pip install mlx-whisper",
            )
        # Touch the repo to trigger the one-time download. We do a
        # tiny dummy generate later — for now just check the repo
        # is reachable. (mlx_whisper.transcribe handles download
        # internally but it'd happen on first /transcribe call,
        # blocking that user's request.)
        _MLX_WHISPER_REPO_LOADED = repo
        logger.info("mlx-whisper engine ready (repo=%s)", repo)
        return repo


def _mlx_whisper_transcribe(wav_path: str) -> str:
    repo = _ensure_mlx_whisper_loaded()
    try:
        import mlx_whisper
        # language='zh' forces Chinese decoding — without it Whisper
        # is prone to hallucinating English fillers like "Look at that"
        # / "Thanks for watching!" / "Bye!" on quiet audio (well-known
        # Whisper failure mode trained from YouTube transcripts).
        res = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=repo,
            language="zh",
            # Don't condition on previous text — prevents the "snowball"
            # hallucination effect where one bad token drags the next.
            condition_on_previous_text=False,
            # Higher temp 0 = greedy, deterministic
            temperature=0.0,
        )
        text = (res.get("text") or "").strip()
        # Last-resort filter — if Whisper still spat a known hallucination,
        # drop it. The frontend treats empty result as "no speech" and
        # silently skips auto-send.
        if _is_whisper_hallucination(text):
            logger.info("mlx-whisper hallucination filtered: %r", text)
            return ""
        return text
    except Exception as e:
        logger.exception("mlx-whisper generate failed")
        raise HTTPException(502, f"transcribe failed: {e}")


# Known Whisper hallucination patterns — tokens it generates from
# silence / breath / mic noise instead of "no speech detected".
# Source: empirical + Whisper community reports.
_HALLUCINATIONS = {
    "look at that.", "look at that. look at that.",
    "thanks for watching!", "thanks for watching.",
    "thank you.", "thank you for watching.",
    "bye!", "bye.", "bye-bye!", "okay.", "ok.",
    "you", "yeah.", "uh-huh.", "mm-hmm.",
    "请订阅", "请订阅本频道", "请订阅、点赞、转发",
    "感谢观看", "谢谢观看", "谢谢大家",
    "字幕由 amara.org 社区提供",
    "字幕志愿者", "中文字幕", "翻译/校对",
}


def _is_whisper_hallucination(text: str) -> bool:
    """Return True if `text` is almost certainly a Whisper artifact
    rather than real user speech, so the caller can substitute "".

    Detection layers (ordered cheapest first):
      1. Empty / sub-2-char strings.
      2. Exact match against the `_HALLUCINATIONS` blocklist (known
         English/Chinese tokens Whisper emits on silence/breath).
      3. Repeated-half-sentence ("Sean Sean Sean") — confidence collapse.
      4. **Mixed-script tinies** (added 2026-05-10 after user reported
         `är冷` slipping through): if the entire transcript is short
         (≤ 6 stripped chars) AND contains BOTH ASCII letters and CJK
         characters, it's overwhelmingly likely garbage. Real Chinese
         speakers don't randomly insert Swedish into 3-character
         utterances.
      5. **Latin-only tinies in zh context** (≤ 3 chars, no CJK, no
         digit) — Whisper drifting into another language on near-
         silent input. Drops "you" / "ok" / "är" but a real "OK" is
         rare in Chinese voice mode and easily re-stated.
    """
    if not text:
        return True
    norm = text.lower().strip()
    if len(norm) < 2:
        return True
    if norm in _HALLUCINATIONS:
        return True
    # Repeated-half-sentence
    halves = norm.split()
    if len(halves) >= 2:
        first_half = " ".join(halves[: len(halves) // 2])
        if first_half and norm.startswith(first_half + " " + first_half):
            return True
    # Strip whitespace + punctuation for length / character-class checks
    stripped = "".join(
        c for c in norm
        if c.isalnum() or '一' <= c <= '鿿' or '㐀' <= c <= '䶿'
    )
    if len(stripped) <= 6:
        has_latin = any(('a' <= c <= 'z') or ('A' <= c <= 'Z') for c in stripped)
        has_cjk = any(
            '一' <= c <= '鿿' or '㐀' <= c <= '䶿'
            for c in stripped
        )
        # Layer 4: short + both scripts → hallucination
        if has_latin and has_cjk:
            return True
        # Layer 5: very short Latin-only when zh was expected
        if len(stripped) <= 3 and has_latin and not has_cjk \
                and not any(c.isdigit() for c in stripped):
            return True
    return False


# ── Engine: Third-party via LLM Provider (OpenAI-compatible) ────
def _llm_provider_transcribe(llm_p, wav_path: str, lang: str,
                             model_override: str = "") -> str:
    """POST audio file to {base_url}/audio/transcriptions.

    Schema mirrors OpenAI's Whisper endpoint and is honored by Groq,
    DeepInfra, Together AI, OpenRouter (some models), and many local
    OpenAI-compatible servers (whisper.cpp's HTTP server, etc.).

    Args:
        llm_p: a `ProviderEntry` from app.llm registry (id/name/kind/
            base_url/api_key).
        wav_path: 16kHz mono WAV file (already decoded by
            _decode_audio_to_wav_path).
        lang: BCP-47 hint (e.g. "zh", "zh-CN") — converted to ISO-639-1
            for the API.
        model_override: explicit model name; defaults to "whisper-1".

    Returns the transcript text; raises HTTPException on failure.
    """
    import httpx
    base = (getattr(llm_p, "base_url", "") or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/audio/transcriptions"
    # Convert "zh-CN" → "zh" (OpenAI Whisper expects ISO-639-1)
    iso_lang = (lang or "zh").split("-")[0].lower() or "zh"
    model = (model_override or "whisper-1").strip() or "whisper-1"
    headers = {}
    api_key = getattr(llm_p, "api_key", "") or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with open(wav_path, "rb") as f:
            files = {
                "file": ("utterance.wav", f, "audio/wav"),
            }
            data = {
                "model": model,
                "language": iso_lang,
                # response_format=json keeps the parser simple.
                "response_format": "json",
                # temperature=0 → greedy, deterministic; reduces drift
                # into hallucinations on short / silent clips.
                "temperature": "0",
            }
            try:
                resp = httpx.post(
                    url, headers=headers, files=files, data=data,
                    timeout=60.0,
                )
            except httpx.RequestError as e:
                raise HTTPException(
                    502, f"STT provider request failed: {e}"
                )
    except OSError as e:
        raise HTTPException(500, f"could not read decoded audio: {e}")
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        raise HTTPException(
            502,
            f"STT provider {llm_p.name!r} returned {resp.status_code}: "
            f"{snippet} (check api_key / base_url / model={model!r})",
        )
    try:
        body = resp.json()
        text = (body.get("text") or "").strip()
    except Exception as e:
        raise HTTPException(
            502, f"STT provider returned non-JSON body: {e}"
        )
    # Reuse the same hallucination guard for whisper-style providers —
    # they're prone to the same "Look at that" / "感谢观看" artifacts.
    if _is_whisper_hallucination(text):
        logger.info(
            "stt provider hallucination filtered: provider=%s text=%r",
            llm_p.name, text,
        )
        return ""
    return text


# ── Audio decoding (shared across engines) ────────────────────────
def _decode_audio_to_wav_path(audio_bytes: bytes) -> str:
    """Decode WebM/MP4/MP3/WAV bytes → temp 16kHz mono WAV file.

    Both FunASR and Whisper expect 16kHz mono. Browser MediaRecorder
    typically outputs 48kHz WebM/Opus, so we resample.
    """
    import tempfile, os
    import soundfile as sf
    import numpy as np
    try:
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception:
        try:
            import torchaudio
            tmp_in = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
            tmp_in.write(audio_bytes)
            tmp_in.close()
            try:
                wav, sr = torchaudio.load(tmp_in.name)
                data = wav.mean(dim=0).numpy().astype("float32") if wav.ndim > 1 else wav.numpy().astype("float32")
            finally:
                os.unlink(tmp_in.name)
        except Exception as e:
            raise HTTPException(400, f"cannot decode audio: {e}")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        try:
            import librosa
            data = librosa.resample(data, orig_sr=sr, target_sr=16000)
            sr = 16000
        except Exception:
            ratio = 16000 / sr
            new_len = int(len(data) * ratio)
            data = np.interp(
                np.linspace(0, len(data) - 1, new_len),
                np.arange(len(data)), data,
            ).astype("float32")
            sr = 16000
    tmp_out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_out.close()
    sf.write(tmp_out.name, data, sr, format="WAV")
    return tmp_out.name


# ── Endpoints ─────────────────────────────────────────────────────
@router.get("/health")
async def stt_health(user: CurrentUser = Depends(get_current_user)):
    """Liveness probe — does NOT load any model.

    Returns the current engine setting + per-engine load state so the
    frontend can decide between server-side STT vs browser STT without
    forcing a model download up front.
    """
    eng = _get_engine_setting()
    info = {
        "ok": True,
        "engine": eng,
        # `loaded` is engine-specific
        "loaded": (
            (_FUNASR_MODEL is not None) if eng == "funasr"
            else (bool(_MLX_WHISPER_REPO_LOADED) if eng == "mlx_whisper"
                  else False)
        ),
        "load_failed": (
            _FUNASR_LOAD_FAILED if eng == "funasr"
            else _MLX_WHISPER_LOAD_FAILED if eng == "mlx_whisper"
            else False
        ),
        "mlx_whisper_repo": _get_mlx_whisper_repo(),
    }
    # When using a third-party LLM provider, the engine ID can't be
    # introspected (it's resolved at /transcribe time). Surface "loaded"
    # = True if the provider exists in the registry; "load_failed" =
    # True if the sentinel points at a missing/disabled provider.
    if eng.startswith(PROVIDER_PREFIX):
        prov, pid = _resolve_llm_provider(eng)
        if prov is None:
            info["load_failed"] = True
            info["provider_error"] = (
                f"LLM provider {pid!r} not found in registry"
            )
        elif not getattr(prov, "enabled", True):
            info["load_failed"] = True
            info["provider_error"] = (
                f"LLM provider {prov.name!r} is disabled"
            )
        else:
            info["loaded"] = True
            info["provider_id"] = pid
            info["provider_name"] = prov.name
            info["provider_kind"] = prov.kind
    return info


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    lang: str = Form("zh"),
    # 2026-05-09 user-suggested fix for "front-back mismatch" complaint:
    # frontend stamps a per-call ID, server echoes it back. If the JSON
    # the client receives carries a different `req_id` than what it sent
    # (race, proxy weirdness, multipart parsing bug, etc.), the client
    # discards the response. Empty/missing is allowed for backwards
    # compat and just echoes "" back.
    req_id: str = Form(""),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Transcribe audio → text via the configured engine.

    Engine is chosen at request time from system_settings (so admins
    can swap engines without restarting the server). 503 = engine
    can't load → frontend falls back to browser STT.

    `req_id` is a frontend-supplied correlation token; we just echo it.
    """
    t0 = time.time()
    eng = _get_engine_setting()
    if eng == "browser":
        raise HTTPException(
            400,
            "STT engine is set to 'browser' — frontend should not "
            "call this endpoint. Either change the engine in System "
            "Settings or update the frontend.",
        )
    audio_bytes = await audio.read()
    if len(audio_bytes) < 200:
        return {"text": "", "duration_ms": 0, "skipped": "too_short",
                "engine": eng, "req_id": req_id}
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "audio too large (>25 MB)")

    import os as _os
    wav_path = _decode_audio_to_wav_path(audio_bytes)
    try:
        if eng == "funasr":
            text = _funasr_transcribe(wav_path)
        elif eng == "mlx_whisper":
            text = _mlx_whisper_transcribe(wav_path)
        elif eng.startswith(PROVIDER_PREFIX):
            llm_p, pid = _resolve_llm_provider(eng)
            if llm_p is None:
                raise HTTPException(
                    503,
                    f"STT LLM provider {pid!r} not found in registry — "
                    f"open System Settings and pick a different STT engine.",
                )
            if not getattr(llm_p, "enabled", True):
                raise HTTPException(
                    503,
                    f"STT LLM provider {llm_p.name!r} is disabled.",
                )
            # Optional model override via system_settings.stt.provider_model
            try:
                from ...system_settings import get_store
                _store = get_store()
                _model = (_store.get("stt.provider_model") or "").strip() \
                    if _store else ""
            except Exception:
                _model = ""
            text = _llm_provider_transcribe(
                llm_p, wav_path, lang, model_override=_model,
            )
        else:
            raise HTTPException(500, f"unknown engine {eng!r}")
    finally:
        try: _os.unlink(wav_path)
        except OSError: pass
    # 2026-05-09: also log the transcript we're about to return — gives
    # us a server-side audit trail for the "log right, chat wrong" debug
    # flow. Truncated to 120 chars.
    _show = text if len(text) <= 120 else text[:120] + "…"
    logger.info(
        "stt.transcribe req_id=%s engine=%s text=%r ms=%d",
        req_id or "-", eng, _show, int((time.time() - t0) * 1000),
    )
    return {
        "text": text,
        "duration_ms": int((time.time() - t0) * 1000),
        "engine": eng,
        "req_id": req_id,
    }


@router.get("/engines")
async def list_engines(user: CurrentUser = Depends(get_current_user)):
    """List available engines + which packages are installed.

    Also includes available LLM providers (kind=openai) so the System
    Settings dropdown can offer them as `__provider__:<id>` choices.
    """
    def _has(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except ImportError:
            return False
    engines = [
        {"id": "browser", "label": "Browser Web Speech",
         "available": True, "speed": "instant", "accuracy": "low"},
        {"id": "funasr", "label": "FunASR Paraformer-zh",
         "available": _has("funasr"), "speed": "2-3s/句",
         "accuracy": "中文极佳"},
        {"id": "mlx_whisper", "label": "MLX Whisper-large-v3-turbo",
         "available": _has("mlx_whisper"), "speed": "0.5-1s/句",
         "accuracy": "多语言极佳"},
    ]
    # LLM providers — only "openai"-kind entries can host
    # /audio/transcriptions. Skip ollama / claude (no whisper endpoint).
    providers: list[dict] = []
    try:
        from ...llm import get_registry as _get_llm_registry
        for p in _get_llm_registry().list(include_disabled=False):
            if getattr(p, "kind", "") != "openai":
                continue
            providers.append({
                "id": PROVIDER_PREFIX + p.id,
                "label": f"{p.name} (cloud STT)",
                "provider_id": p.id,
                "provider_name": p.name,
                "base_url": getattr(p, "base_url", "") or "",
                "available": True,
                "speed": "~0.3-1s/句",
                "accuracy": "依模型",
            })
    except Exception as e:
        logger.warning("Failed to enumerate LLM providers for STT: %s", e)
    return {
        "current": _get_engine_setting(),
        "engines": engines,
        "providers": providers,
    }
