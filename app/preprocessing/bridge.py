"""Preprocessor bridge — single entry point with fail-safe fallback.

Every node calls this module's ``invoke()`` function. The bridge:
  1. Checks ``agent.preprocessor_model`` — empty = skip
  2. Checks LRU cache (content_hash → result)
  3. Checks failure-rate circuit breaker (auto-pause if > threshold)
  4. Calls Ollama (or whatever provider the model maps to) with timeout
  5. Caches successful results
  6. ALWAYS returns ``PreprocessorResult`` — caller checks ``.ok``

Caller pattern:
    res = bridge.invoke(agent, kind="prompt_optimize", payload={...})
    if res.ok:
        # use res.value (already small-model-processed)
        ...
    else:
        # fall back to original (res.skip_reason explains why)
        ...
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("tudou.preprocessing")


# ─── failure-rate circuit breaker ────────────────────────────────────
# Track last N attempts per (agent_id, kind). If failure rate exceeds
# threshold, auto-pause that (agent, kind) combo for 60s. The next
# attempt after pause expires resets the window.
_BREAKER_WINDOW = 5
_BREAKER_FAIL_THRESHOLD = 0.4  # 5 attempts, ≥ 2 failures → pause
_BREAKER_PAUSE_S = 60.0

_breaker_state: dict[tuple[str, str], dict] = {}
_breaker_lock = threading.Lock()


def _circuit_open(agent_id: str, kind: str) -> bool:
    """Return True if the breaker is currently open (skip preprocessor)."""
    key = (agent_id, kind)
    with _breaker_lock:
        st = _breaker_state.get(key)
        if not st:
            return False
        if time.time() < st.get("paused_until", 0):
            return True
        # Pause expired — clear and retry
        if st.get("paused_until", 0) and time.time() >= st["paused_until"]:
            _breaker_state.pop(key, None)
        return False


def _record_attempt(agent_id: str, kind: str, success: bool) -> None:
    key = (agent_id, kind)
    with _breaker_lock:
        st = _breaker_state.setdefault(key, {"history": [], "paused_until": 0})
        st["history"].append(1 if success else 0)
        st["history"] = st["history"][-_BREAKER_WINDOW:]
        if len(st["history"]) >= _BREAKER_WINDOW:
            fails = st["history"].count(0)
            if fails / len(st["history"]) >= _BREAKER_FAIL_THRESHOLD:
                st["paused_until"] = time.time() + _BREAKER_PAUSE_S
                logger.warning(
                    "preprocessor circuit OPEN for agent=%s kind=%s (%d/%d failures); pausing %ds",
                    agent_id, kind, fails, _BREAKER_WINDOW, int(_BREAKER_PAUSE_S),
                )


# ─── LRU cache ───────────────────────────────────────────────────────
_LRU_CAPACITY = 256
_lru: OrderedDict[str, Any] = OrderedDict()
_lru_lock = threading.Lock()
_lru_hits = 0
_lru_misses = 0


def _cache_get(key: str) -> Optional[Any]:
    global _lru_hits, _lru_misses
    with _lru_lock:
        if key in _lru:
            _lru.move_to_end(key)
            _lru_hits += 1
            return _lru[key]
        _lru_misses += 1
        return None


def _cache_put(key: str, value: Any) -> None:
    with _lru_lock:
        _lru[key] = value
        _lru.move_to_end(key)
        while len(_lru) > _LRU_CAPACITY:
            _lru.popitem(last=False)


def _cache_key(agent_id: str, kind: str, model: str, payload: dict) -> str:
    """Stable hash of (agent_id, kind, model, payload-sorted)."""
    blob = json.dumps(
        {"a": agent_id, "k": kind, "m": model, "p": payload},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ─── observability counters ──────────────────────────────────────────
_metrics: dict[str, dict] = {}  # kind → {calls, hits, fallbacks, total_saved_tokens}
_metrics_lock = threading.Lock()


def _bump_metric(kind: str, **kw) -> None:
    with _metrics_lock:
        m = _metrics.setdefault(kind, {
            "calls": 0, "cache_hits": 0, "fallbacks": 0,
            "tokens_in": 0, "tokens_out": 0, "tokens_saved": 0,
            "latency_ms_total": 0,
        })
        for k, v in kw.items():
            m[k] = m.get(k, 0) + v


def get_metrics() -> dict[str, dict]:
    """Snapshot of per-kind metrics for observability."""
    with _metrics_lock:
        snap = {k: dict(v) for k, v in _metrics.items()}
    snap["_cache"] = {
        "size": len(_lru), "capacity": _LRU_CAPACITY,
        "hits": _lru_hits, "misses": _lru_misses,
    }
    return snap


def get_breaker_state() -> list[dict]:
    """Snapshot of circuit breaker state. Returns one row per
    (agent_id, kind) currently being tracked. UI shows this as
    "phase paused" badges."""
    now = time.time()
    out: list[dict] = []
    with _breaker_lock:
        for (agent_id, kind), st in _breaker_state.items():
            history = st.get("history", []) or []
            paused_until = float(st.get("paused_until", 0) or 0)
            paused = paused_until > now
            out.append({
                "agent_id": agent_id,
                "kind": kind,
                "history_size": len(history),
                "fail_count": history.count(0),
                "success_count": history.count(1),
                "fail_rate": (
                    round(history.count(0) / len(history), 3)
                    if history else 0.0
                ),
                "paused": paused,
                "paused_remaining_s": max(0, int(paused_until - now)) if paused else 0,
            })
    return out


# ─── public API ──────────────────────────────────────────────────────
def is_enabled(agent) -> bool:
    """Empty preprocessor_model = disabled. Single-source-of-truth."""
    val = getattr(agent, "preprocessor_model", "") or ""
    return bool(val.strip())


def get_model(agent) -> str:
    return (getattr(agent, "preprocessor_model", "") or "").strip()


def get_modes(agent) -> list[str]:
    """Modes for first-turn-setup node. Returns lowercase list."""
    raw = getattr(agent, "preprocessor_modes", []) or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    return [str(m).strip().lower() for m in raw if m]


def get_fallback_chain(agent) -> list[tuple[str, str]]:
    """Parse ``preprocessor_fallback`` into ``[(model, endpoint), ...]``.

    Each entry can be ``"model"`` (uses agent's primary endpoint) or
    ``"model@endpoint"`` (explicit endpoint per fallback). Empty list
    when no fallbacks configured.
    """
    raw = getattr(agent, "preprocessor_fallback", []) or []
    primary_endpoint = (getattr(agent, "preprocessor_endpoint", "") or "").strip()
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not entry:
            continue
        s = str(entry).strip()
        if "@" in s:
            model, _, ep = s.partition("@")
            model = model.strip()
            ep = ep.strip() or primary_endpoint
        else:
            model = s
            ep = primary_endpoint
        if model:
            out.append((model, ep))
    return out


@dataclass
class PreprocessorResult:
    """Bridge return value — caller checks ``ok`` first."""
    ok: bool = False
    value: Any = None
    skip_reason: str = ""           # populated when ok=False
    cache_hit: bool = False
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


# Default per-call timeout (seconds). Calls exceeding this fall back.
_TIMEOUT_S = 3.0


def invoke(
    agent,
    *,
    kind: str,
    payload: dict,
    timeout_s: Optional[float] = None,
    model_override: str = "",
) -> PreprocessorResult:
    """Run the preprocessor for ``kind`` (the node identifier).

    Caller is responsible for:
      - knowing what payload shape ``kind`` expects
      - interpreting ``result.value`` per ``kind``
      - falling back to the original behaviour when ``result.ok == False``

    Currently supported ``kind`` values:
      - ``"prompt_optimize"`` — payload {"prompt": str, "context": dict?},
        returns dict {"prompt": optimised_str, "saved_tokens": int}.
      - ``"task_decompose"`` — payload {"intent": str, "n": int=4},
        returns dict {"sub_tasks": list, "rationale": str}.
      - ``"task_understanding"`` — payload {"intent": str},
        returns dict {"summary": str, "recommended_tier": str,
                      "rag_needed": bool, "decompose_needed": bool}.

    Each kind has its own implementation in ``app/preprocessing/<kind>.py``.
    Implementations are loaded lazily (no import overhead when disabled).
    """
    if not is_enabled(agent):
        return PreprocessorResult(ok=False, skip_reason="not_configured")

    aid = getattr(agent, "id", "unknown")
    primary_model = (model_override or get_model(agent)).strip()
    if not primary_model:
        return PreprocessorResult(ok=False, skip_reason="no_model")

    if _circuit_open(aid, kind):
        return PreprocessorResult(ok=False, skip_reason="circuit_breaker_open")

    # Build the model chain: primary first, then user-configured fallbacks.
    # Each entry: (model_name, endpoint_override_or_empty).
    primary_endpoint = (getattr(agent, "preprocessor_endpoint", "") or "").strip()
    chain: list[tuple[str, str]] = [(primary_model, primary_endpoint)]
    if not model_override:
        # Only chain fallbacks when caller didn't pin a specific model
        for fb_model, fb_endpoint in get_fallback_chain(agent):
            if (fb_model, fb_endpoint) not in chain:
                chain.append((fb_model, fb_endpoint))

    # Dispatch to per-kind implementation
    impl = _load_impl(kind)
    if impl is None:
        return PreprocessorResult(
            ok=False, skip_reason=f"unknown_kind:{kind}",
        )

    last_error = ""
    last_latency = 0
    for idx, (try_model, try_endpoint) in enumerate(chain):
        # LRU cache lookup (per-model — different models give different outputs)
        ckey = _cache_key(aid, kind, try_model, payload)
        cached = _cache_get(ckey)
        if cached is not None:
            _bump_metric(kind, calls=1, cache_hits=1)
            return PreprocessorResult(
                ok=True, value=cached, cache_hit=True, latency_ms=0,
            )

        # Patch the agent's endpoint for this attempt if fallback specifies one.
        # We do this carefully — only override if try_endpoint differs and is set.
        original_endpoint = primary_endpoint
        endpoint_was_overridden = False
        if try_endpoint and try_endpoint != original_endpoint:
            try:
                agent.preprocessor_endpoint = try_endpoint
                endpoint_was_overridden = True
            except Exception:
                pass

        t0 = time.time()
        try:
            value, tin, tout = impl(
                agent=agent, model=try_model, payload=payload,
                timeout_s=timeout_s if timeout_s is not None else _TIMEOUT_S,
            )
            latency_ms = int((time.time() - t0) * 1000)
            _cache_put(ckey, value)
            _record_attempt(aid, kind, success=True)
            _bump_metric(
                kind, calls=1, tokens_in=tin, tokens_out=tout,
                latency_ms_total=latency_ms,
            )
            if idx > 0:
                logger.info(
                    "preprocessor agent=%s kind=%s succeeded with FALLBACK model=%s "
                    "(primary failed)",
                    aid, kind, try_model,
                )
            return PreprocessorResult(
                ok=True, value=value, cache_hit=False,
                latency_ms=latency_ms, tokens_in=tin, tokens_out=tout,
            )
        except Exception as e:
            last_latency = int((time.time() - t0) * 1000)
            last_error = f"{type(e).__name__}"
            _record_attempt(aid, kind, success=False)
            logger.debug(
                "preprocessor agent=%s kind=%s model=%s failed: %s%s",
                aid, kind, try_model, e,
                " (trying fallback)" if idx + 1 < len(chain) else " (no more fallbacks)",
            )
        finally:
            # Restore endpoint so the agent's record isn't permanently mutated
            if endpoint_was_overridden:
                try:
                    agent.preprocessor_endpoint = original_endpoint
                except Exception:
                    pass

    # All chain entries failed → fall through to caller's default path
    _bump_metric(kind, calls=1, fallbacks=1, latency_ms_total=last_latency)
    skip_reason = (
        f"all_models_failed:{last_error}" if len(chain) > 1
        else f"error:{last_error}"
    )
    return PreprocessorResult(
        ok=False, skip_reason=skip_reason, latency_ms=last_latency,
    )


def _load_impl(kind: str):
    """Lazy import per-kind implementation. Returns callable or None.

    Each impl signature: ``(agent, model, payload, timeout_s) -> (value, tokens_in, tokens_out)``.
    """
    try:
        if kind == "prompt_optimize":
            from .prompt_optimize import run as _run
            return _run
        if kind == "task_decompose":
            from .decompose import run as _run
            return _run
        if kind == "task_understanding":
            from .task_understanding import run as _run
            return _run
    except ImportError:
        return None
    return None
