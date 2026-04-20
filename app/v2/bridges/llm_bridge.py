"""
V2 LLM bridge (PRD §10.5.1).

Single responsibility: resolve an agent's tier + messages into an LLM
call and normalise the result. Provider-level concerns (auth, base_url,
concurrency, fallback chains, cost tracking) live in V1's
``ProviderRegistry`` + ``app.llm.chat_no_stream`` — V2 does NOT
maintain its own provider state.

Call pipeline:

    tier (agent capability)
        → llm_tier_routing.resolve_tier()        [V1 registry lookup]
        → (provider_id, model)
        → app.llm.chat_no_stream()               [V1 HTTP + fallback chain]
        → raw assistant message
        → tool_parsers.ParserRegistry.resolve(model).parse()
        → NormalizedMessage.to_openai_dict()

The parser layer is the ONE thing V2 owns exclusively — it handles
model-output format differences (XML-tag JSON, bare JSON, provider-
normalized) via a plugin registry so new model families can be added
without core edits.
"""
from __future__ import annotations

import logging
from typing import Any

from . import llm_tier_routing as _tier_routing
from .tool_parsers import get_registry, NormalizedMessage


logger = logging.getLogger("tudouclaw.v2.llm_bridge")


def _resolve_tier(tier: str) -> tuple[str, str]:
    return _tier_routing.resolve_tier(tier)


# ── primary entry ─────────────────────────────────────────────────────


def call_llm(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    tier: str = "default",
    max_tokens: int = 4096,   # noqa: ARG001 — V1 provider handles per-call
    stream: bool = False,     # noqa: ARG001 — V2 executor is always non-stream
) -> dict:
    """Call the resolved LLM and return a normalised assistant message.

    Return shape::

        {"role": "assistant", "content": str, "tool_calls": list[dict]}

    Never returns a streaming generator — the V2 executor drives turns
    explicitly.
    """
    provider, model = _resolve_tier(tier)

    # V1 owns provider routing (registry, fallback chain, cost, pool).
    raw = _call_via_v1(
        messages=messages, tools=tools,
        provider=provider, model=model,
    )
    raw_msg = _extract_assistant_message(raw)

    # Model-aware parsing (Qwen XML, Hermes JSON, GLM function_call, …).
    registry = get_registry()
    parser = registry.resolve(model or "")
    normalized: NormalizedMessage = parser.parse(raw_msg)

    return normalized.to_openai_dict()


# ── V1 provider path ──────────────────────────────────────────────────


def _call_via_v1(
    *,
    messages: list[dict],
    tools: list[dict] | None,
    provider: str,
    model: str,
) -> Any:
    """Delegate to V1's chat_no_stream — it knows about the registry,
    the fallback chain, the connection pool, and cost tracking."""
    from app import llm as _llm
    return _llm.chat_no_stream(
        messages=messages, tools=tools,
        provider=provider, model=model,
    )


# ── message normalisation ─────────────────────────────────────────────


def _extract_assistant_message(raw) -> dict:
    """Pull the assistant message dict out of whatever shape came back."""
    if raw is None:
        return {"role": "assistant", "content": "", "tool_calls": []}

    if hasattr(raw, "model_dump"):
        try:
            d = raw.model_dump()
        except Exception:
            d = raw
    elif hasattr(raw, "dict"):
        try:
            d = raw.dict()
        except Exception:
            d = raw
    else:
        d = raw

    if isinstance(d, dict):
        # OpenAI / litellm ChatCompletion shape.
        choices = d.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                return msg
        # Ollama / V1 shape: {"message": {...}, "done": True}
        msg = d.get("message")
        if isinstance(msg, dict):
            return msg
        # Already a message dict.
        if "role" in d or "content" in d or "tool_calls" in d:
            return d
    return {"role": "assistant", "content": str(d or ""), "tool_calls": []}


__all__ = ["call_llm"]
