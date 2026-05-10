"""TTS provider registry — third-party text-to-speech adapters.

Mirrors the LLM ProviderRegistry pattern (app/llm.py) but for speech
synthesis. Browser-side Web Speech API remains the default fallback;
this module enables a server-side path where the backend calls a
third-party TTS service (OpenAI /v1/audio/speech, ElevenLabs,
Azure Cognitive Services, Edge-TTS, etc.) and streams the audio
back to the browser as audio/mpeg.

Storage: ``~/.tudou_claw/tts_providers.json`` — list of provider
entries; one of them may be flagged ``active=True`` to override the
browser-side default.

Built-in adapter kinds:
  - "openai"     — POST {base_url}/audio/speech → mp3 bytes
  - "elevenlabs" — POST {base_url}/text-to-speech/{voice_id} → mp3
  - "azure"     — POST {base_url}/cognitiveservices/v1 (SSML)
  - "edge"       — Microsoft Edge TTS (free, no key) via edge-tts pkg

The synthesize() function returns raw audio bytes (mp3) and a content
type. Errors raise TTSError with a user-actionable message.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("tudouclaw.tts")


class TTSError(Exception):
    """Raised by adapter functions when synthesis fails. The message
    is shown to the user, so phrase it actionably (include provider
    name, status code, and remediation hint when possible)."""


@dataclass
class TTSProvider:
    """A configured TTS provider entry."""
    id: str
    name: str                     # human-friendly label
    kind: str                     # "openai" | "elevenlabs" | "azure" | "edge" | "browser"
    base_url: str = ""            # https://api.openai.com/v1 (no trailing slash)
    api_key: str = ""             # raw key — masked in to_dict by default
    voice: str = ""               # provider-specific default voice id/name
    model: str = ""               # e.g. "tts-1", "tts-1-hd" for openai
    lang: str = "zh-CN"           # default language hint (BCP-47)
    speed: float = 1.0            # playback rate, 0.5–2.0
    enabled: bool = True
    extra: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self, *, mask_key: bool = True) -> dict:
        d = asdict(self)
        if mask_key and self.api_key:
            d["api_key"] = self.api_key[:4] + "****" + self.api_key[-2:] \
                if len(self.api_key) > 8 else "****"
            d["api_key_set"] = True
        else:
            d["api_key_set"] = bool(self.api_key)
        return d


class TTSRegistry:
    """Persisted registry of TTS provider configs.

    One provider can be flagged ``active`` (via system_settings
    ``tts.active_provider_id``). When the frontend asks the server to
    synthesize, that provider is used; if no active provider, the
    frontend falls back to browser Web Speech API.
    """

    def __init__(self, data_dir: str = ""):
        self._providers: dict[str, TTSProvider] = {}
        self._lock = threading.Lock()
        from . import DEFAULT_DATA_DIR
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        self._file = os.path.join(self._data_dir, "tts_providers.json")
        self._load()

    # ── persistence ────────────────────────────────────────────────
    def _load(self) -> None:
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for entry in raw.get("providers", []):
                p = TTSProvider(**{
                    k: v for k, v in entry.items()
                    if k in TTSProvider.__dataclass_fields__
                })
                self._providers[p.id] = p
        except Exception as e:
            logger.warning("TTSRegistry load failed: %s", e)

    def _save(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        tmp = self._file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"providers": [asdict(p) for p in self._providers.values()]},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, self._file)

    # ── CRUD ───────────────────────────────────────────────────────
    def add(self, *, name: str, kind: str, base_url: str = "",
            api_key: str = "", voice: str = "", model: str = "",
            lang: str = "zh-CN", speed: float = 1.0,
            enabled: bool = True, extra: Optional[dict] = None,
            ) -> TTSProvider:
        kind = (kind or "").strip().lower()
        if kind not in ("openai", "elevenlabs", "azure", "edge", "voxcpm", "browser"):
            raise ValueError(f"Unknown TTS kind: {kind!r}")
        if not name.strip():
            raise ValueError("name is required")
        with self._lock:
            pid = uuid.uuid4().hex[:12]
            p = TTSProvider(
                id=pid, name=name.strip(), kind=kind,
                base_url=base_url.strip().rstrip("/"),
                api_key=api_key.strip(),
                voice=voice.strip(), model=model.strip(),
                lang=lang.strip() or "zh-CN",
                speed=max(0.25, min(4.0, float(speed or 1.0))),
                enabled=bool(enabled),
                extra=dict(extra or {}),
            )
            self._providers[pid] = p
            self._save()
            return p

    def update(self, pid: str, **fields) -> Optional[TTSProvider]:
        with self._lock:
            p = self._providers.get(pid)
            if p is None:
                return None
            allowed = {"name", "base_url", "api_key", "voice", "model",
                       "lang", "speed", "enabled", "extra"}
            for k, v in fields.items():
                if k in allowed:
                    setattr(p, k, v)
            p.updated_at = time.time()
            self._save()
            return p

    def remove(self, pid: str) -> bool:
        with self._lock:
            if pid not in self._providers:
                return False
            del self._providers[pid]
            self._save()
            return True

    def get(self, pid: str) -> Optional[TTSProvider]:
        return self._providers.get(pid)

    def list(self, *, include_disabled: bool = True) -> list[TTSProvider]:
        items = list(self._providers.values())
        if not include_disabled:
            items = [p for p in items if p.enabled]
        items.sort(key=lambda p: p.created_at)
        return items


# ── module-level singleton ────────────────────────────────────────
_REGISTRY: Optional[TTSRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_tts_registry() -> TTSRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = TTSRegistry()
        return _REGISTRY


# ──────────────────────────────────────────────────────────────────
# Adapters — each takes a TTSProvider + text, returns (audio_bytes, mime)
# ──────────────────────────────────────────────────────────────────

def synthesize(provider: TTSProvider, text: str, *,
               voice: str = "", lang: str = "",
               speed: Optional[float] = None) -> tuple[bytes, str]:
    """Dispatch to the right adapter based on provider.kind. Returns
    (audio_bytes, mime_type). Raises TTSError on failure.
    """
    if not text.strip():
        raise TTSError("empty text")
    if not provider.enabled:
        raise TTSError(f"provider {provider.name!r} is disabled")
    voice = voice or provider.voice
    lang = lang or provider.lang or "zh-CN"
    speed = speed if speed is not None else provider.speed

    if provider.kind == "openai":
        return _openai_tts(provider, text, voice=voice, speed=speed)
    if provider.kind == "elevenlabs":
        return _elevenlabs_tts(provider, text, voice=voice)
    if provider.kind == "azure":
        return _azure_tts(provider, text, voice=voice, lang=lang, speed=speed)
    if provider.kind == "edge":
        return _edge_tts(provider, text, voice=voice, lang=lang, speed=speed)
    if provider.kind == "voxcpm":
        return _voxcpm_tts(provider, text, voice=voice, lang=lang, speed=speed)
    if provider.kind == "browser":
        raise TTSError("'browser' kind has no server-side synthesis — "
                       "the frontend will use Web Speech API directly")
    raise TTSError(f"unknown kind {provider.kind!r}")


def _openai_tts(p: TTSProvider, text: str, *,
                voice: str, speed: float) -> tuple[bytes, str]:
    """Call OpenAI-compatible /v1/audio/speech endpoint.

    Works with: OpenAI, OpenRouter (some models), DeepSeek (when they
    add it), local TTS servers that mimic the schema, Groq.
    """
    import httpx
    base = p.base_url or "https://api.openai.com/v1"
    url = f"{base}/audio/speech"
    payload = {
        "model": p.model or "tts-1",
        "input": text,
        "voice": voice or "alloy",
        "speed": max(0.25, min(4.0, float(speed or 1.0))),
        "response_format": "mp3",
    }
    headers = {"Content-Type": "application/json"}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
    except httpx.RequestError as e:
        raise TTSError(f"OpenAI-TTS request failed: {e}") from e
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        raise TTSError(
            f"OpenAI-TTS returned {resp.status_code}: {snippet} "
            f"(check api_key / base_url / model={p.model!r})"
        )
    return resp.content, "audio/mpeg"


def _elevenlabs_tts(p: TTSProvider, text: str, *,
                    voice: str) -> tuple[bytes, str]:
    """Call ElevenLabs /v1/text-to-speech/{voice_id}.

    voice param here is the ElevenLabs voice_id (e.g. "21m00Tcm4TlvDq8ikWAM"
    for "Rachel"). p.model can be ``eleven_multilingual_v2`` or other.
    """
    import httpx
    base = p.base_url or "https://api.elevenlabs.io/v1"
    voice_id = voice or p.voice or "21m00Tcm4TlvDq8ikWAM"
    url = f"{base}/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": p.model or "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    if p.api_key:
        headers["xi-api-key"] = p.api_key
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
    except httpx.RequestError as e:
        raise TTSError(f"ElevenLabs request failed: {e}") from e
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        raise TTSError(
            f"ElevenLabs returned {resp.status_code}: {snippet} "
            f"(check xi-api-key / voice_id={voice_id!r})"
        )
    return resp.content, "audio/mpeg"


def _azure_tts(p: TTSProvider, text: str, *,
               voice: str, lang: str, speed: float) -> tuple[bytes, str]:
    """Call Azure Cognitive Services TTS via SSML.

    base_url should be the region-specific endpoint:
    https://<region>.tts.speech.microsoft.com
    api_key is the subscription key.
    """
    import httpx
    base = p.base_url or "https://eastus.tts.speech.microsoft.com"
    url = f"{base}/cognitiveservices/v1"
    voice_name = voice or p.voice or "zh-CN-XiaoxiaoNeural"
    rate_pct = int((speed - 1.0) * 100)
    rate_str = f"{rate_pct:+d}%"
    ssml = (
        f'<speak version="1.0" xml:lang="{lang}">'
        f'<voice name="{voice_name}">'
        f'<prosody rate="{rate_str}">{_xml_escape(text)}</prosody>'
        f'</voice></speak>'
    )
    headers = {
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
    }
    if p.api_key:
        headers["Ocp-Apim-Subscription-Key"] = p.api_key
    try:
        resp = httpx.post(url, content=ssml, headers=headers, timeout=60.0)
    except httpx.RequestError as e:
        raise TTSError(f"Azure-TTS request failed: {e}") from e
    if resp.status_code != 200:
        snippet = (resp.text or "")[:200]
        raise TTSError(
            f"Azure-TTS returned {resp.status_code}: {snippet} "
            f"(check Ocp-Apim-Subscription-Key / region)"
        )
    return resp.content, "audio/mpeg"


def _edge_tts(p: TTSProvider, text: str, *,
              voice: str, lang: str, speed: float) -> tuple[bytes, str]:
    """Microsoft Edge TTS — free, no key, requires ``edge-tts`` pip pkg.

    Voice list: https://github.com/rany2/edge-tts (e.g. zh-CN-XiaoxiaoNeural,
    zh-CN-YunxiNeural, en-US-AriaNeural, etc.)

    2026-05-09: original impl used `asyncio.new_event_loop()` +
    `run_until_complete()` which crashed when called from inside a
    running event loop (FastAPI request handler):
        RuntimeError: Cannot run the event loop while another loop is
        running
    Fix: run the coroutine in a dedicated worker thread that owns its
    own loop. The thread is one-shot, joined synchronously, so callers
    still get a blocking `(bytes, mime)` return.
    """
    try:
        import edge_tts  # type: ignore
    except ImportError as e:
        raise TTSError(
            "edge-tts package not installed. Run: pip install edge-tts"
        ) from e
    voice_name = voice or p.voice or "zh-CN-XiaoxiaoNeural"
    rate_pct = int((speed - 1.0) * 100)
    rate_str = f"{rate_pct:+d}%"
    import asyncio
    import io
    import threading

    async def _run():
        comm = edge_tts.Communicate(text, voice=voice_name, rate=rate_str)
        buf = io.BytesIO()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    result: dict = {"audio": b"", "err": None}

    def _worker():
        try:
            loop = asyncio.new_event_loop()
            try:
                result["audio"] = loop.run_until_complete(_run())
            finally:
                loop.close()
        except Exception as e:
            result["err"] = e

    th = threading.Thread(target=_worker, name="edge-tts-worker", daemon=True)
    th.start()
    th.join(timeout=60.0)
    if th.is_alive():
        raise TTSError("Edge-TTS timed out (>60s)")
    if result["err"] is not None:
        raise TTSError(f"Edge-TTS failed: {result['err']}") from result["err"]
    audio = result["audio"]
    if not audio:
        raise TTSError("Edge-TTS returned empty audio")
    return audio, "audio/mpeg"


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;")
             .replace("'", "&apos;"))


# ── VoxCPM (local 2B-param TTS by OpenBMB) ────────────────────────
# Installed via `pip install voxcpm soundfile`. Model auto-downloads on
# first call from HuggingFace (`openbmb/VoxCPM2`). Requires PyTorch ≥
# 2.5; runs on CUDA (fast) or MPS (Apple Silicon, slower) or CPU
# (very slow). Output: 48kHz studio WAV.
#
# Lazy-loaded singleton — first call pays the load cost (~30s on GPU,
# longer on CPU/MPS), subsequent calls reuse the in-memory model.
_VOXCPM_MODEL = None
_VOXCPM_LOAD_LOCK = threading.Lock()
_VOXCPM_LOAD_FAILED = False  # Cached after first failure to skip retries


def _voxcpm_tts(p: TTSProvider, text: str, *,
                voice: str, lang: str, speed: float,
                prompt_wav_path: str = "") -> tuple[bytes, str]:
    """Local TTS via OpenBMB's VoxCPM2 model.

    Three voice modes (best to worst consistency):
      1. **Voice cloning** — prompt_wav_path + voice (as transcript)
         both set. Copies timbre/style from the reference clip.
      2. **Control Instruction** — voice is a NL description
         ("warm female narrator" / "年轻女性温柔"). VoxCPM2 expects
         this prepended in parentheses to the text:
             generate(text="(年轻女性温柔)你好世界")
         (Discovered from app.py — NOT a separate kwarg.)
      3. **Pure random** — both empty. Each call → new random voice.
         Seeded torch RNG so same input → same output across calls.
    """
    global _VOXCPM_MODEL, _VOXCPM_LOAD_FAILED
    if _VOXCPM_LOAD_FAILED:
        raise TTSError(
            "voxcpm previously failed to load — restart the server to retry "
            "(check torch / CUDA / MPS availability)."
        )
    try:
        with _VOXCPM_LOAD_LOCK:
            if _VOXCPM_MODEL is None:
                try:
                    from voxcpm import VoxCPM  # type: ignore
                except ImportError as e:
                    _VOXCPM_LOAD_FAILED = True
                    raise TTSError(
                        "voxcpm not installed. Run: "
                        "pip install voxcpm soundfile (requires Python "
                        ">=3.10 <3.13, PyTorch >=2.5)"
                    ) from e
                model_id = p.model or "openbmb/VoxCPM2"
                logger.info("Loading VoxCPM model %s (one-time cost)…", model_id)
                _VOXCPM_MODEL = VoxCPM.from_pretrained(
                    model_id, load_denoiser=False,
                )
                logger.info("VoxCPM model loaded")
        # Synthesize
        try:
            import soundfile as sf  # type: ignore
        except ImportError as e:
            raise TTSError(
                "soundfile not installed. Run: pip install soundfile"
            ) from e
        # ── Voice consistency hack ──
        # VoxCPM2's generate() has NO voice_description / seed param —
        # the only way to control voice is `prompt_wav_path + prompt_text`
        # (voice cloning via reference audio, requires user-uploaded
        # files). Without that, each call generates a different random
        # voice via the model's internal RNG.
        #
        # Workaround: seed torch's global RNG just before generate() so
        # the same (text, voice_hint) produces the same audio across
        # calls. The voice param here is repurposed: we use a hash of
        # it to derive the seed, so picking different "presets" in the
        # UI at least gives DIFFERENT but STABLE voices, even if the
        # mapping is opaque (no semantic meaning).
        try:
            import torch as _torch
            voice_str = (voice or "default").strip() or "default"
            # Stable 32-bit seed from hash (Python's hash() varies per
            # session, so use a deterministic hash instead).
            import hashlib
            h = hashlib.md5(voice_str.encode("utf-8")).digest()
            seed = int.from_bytes(h[:4], "big") & 0x7fffffff
            _torch.manual_seed(seed)
            if _torch.backends.mps.is_available():
                _torch.mps.manual_seed(seed)
            elif _torch.cuda.is_available():
                _torch.cuda.manual_seed_all(seed)
            logger.info("voxcpm seed = %d (from voice=%r)", seed, voice_str)
        except Exception as _se:
            logger.debug("voxcpm seed-set failed: %s", _se)
        voice_desc = None  # not actually consumed by VoxCPM2 generate()
        # Speed/quality dial — 10 was upstream default, 5 ≈ half time
        # with minor quality loss (override: TUDOU_VOXCPM_TIMESTEPS=10).
        import os as _os
        timesteps = int(_os.environ.get("TUDOU_VOXCPM_TIMESTEPS", "5"))
        # ── Choose voice control strategy ──
        gen_kwargs = {
            "cfg_value": 2.0,
            "inference_timesteps": timesteps,
        }
        final_text = text
        voice_clean = (voice or "").strip()
        # Voice cloning takes priority over Control Instruction
        if prompt_wav_path:
            import os as _os2
            if _os2.path.isfile(prompt_wav_path):
                gen_kwargs["prompt_wav_path"] = prompt_wav_path
                # Reference transcript: voice field doubles as the
                # transcript when a prompt_wav is present.
                if voice_clean:
                    gen_kwargs["prompt_text"] = voice_clean
                logger.info("voxcpm voice-cloning from %s",
                            _os2.path.basename(prompt_wav_path))
            else:
                logger.warning("voxcpm prompt_wav_path missing: %s",
                               prompt_wav_path)
        elif voice_clean:
            # Control Instruction: prepend "(description)" to the text.
            # Strip parens from the control to avoid breaking the
            # parser (matches what the upstream demo does).
            import re as _re
            ctrl = _re.sub(r"[()（）]", "", voice_clean).strip()
            if ctrl:
                final_text = "(" + ctrl + ")" + text
                logger.info("voxcpm control-instruction: %r", ctrl)
        gen_kwargs["text"] = final_text
        wav = _VOXCPM_MODEL.generate(**gen_kwargs)
        sample_rate = getattr(
            getattr(_VOXCPM_MODEL, "tts_model", None), "sample_rate", 48000)
        import io
        buf = io.BytesIO()
        sf.write(buf, wav, sample_rate, format="WAV")
        return buf.getvalue(), "audio/wav"
    except TTSError:
        raise
    except Exception as e:
        # Cache hard failures (CUDA OOM, missing model files, etc.) so
        # subsequent calls don't keep retrying the slow load path.
        if _VOXCPM_MODEL is None:
            _VOXCPM_LOAD_FAILED = True
        raise TTSError(f"voxcpm synthesis failed: {e}") from e


# ── active-provider helpers (uses system_settings) ────────────────

ACTIVE_KEY = "tts.active_provider_id"


def get_active_provider_id() -> str:
    try:
        from .system_settings import get_store
        store = get_store()
        if store is None:
            return ""
        return (store.get(ACTIVE_KEY) or "").strip()
    except Exception:
        return ""


def set_active_provider_id(pid: str) -> None:
    from .system_settings import get_store
    store = get_store()
    if store is None:
        return
    store.set(ACTIVE_KEY, pid or "")


def get_active_provider() -> Optional[TTSProvider]:
    """Return the currently-active provider, or None to signal that
    the frontend should fall back to browser Web Speech API."""
    pid = get_active_provider_id()
    if not pid:
        return None
    return get_tts_registry().get(pid)
