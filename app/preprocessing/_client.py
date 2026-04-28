"""Tiny OpenAI-compat client for preprocessor calls.

Speaks ``POST /v1/chat/completions`` to whatever endpoint the agent
configured: Ollama (default ``http://localhost:11434``), MLX-LM
(``http://127.0.0.1:10240``), vLLM, llama.cpp server, etc.

Synchronous + stdlib-only (no httpx/requests dep). Built-in 3s default
timeout. Returns ``(content_str, tokens_in, tokens_out)``.

Failures raise — the bridge converts to ``PreprocessorResult(ok=False)``
and the caller falls back to the original behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("tudou.preprocessing.client")


_DEFAULT_ENDPOINT = os.environ.get(
    "TUDOU_PREPROCESSOR_DEFAULT_ENDPOINT",
    "http://localhost:11434",
).rstrip("/")


def chat_completion(
    *,
    endpoint: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.0,
    max_tokens: int = 512,
    timeout_s: float = 3.0,
) -> tuple[str, int, int]:
    """Call OpenAI-compat /v1/chat/completions.

    Returns (content, tokens_in, tokens_out). Raises on transport error,
    HTTP non-2xx, or malformed response — the bridge handles fallback.
    """
    base = (endpoint or _DEFAULT_ENDPOINT).rstrip("/")
    url = f"{base}/v1/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "tudouclaw-preprocessor/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body_text}") from e
    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("no choices in response")
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    return content, tin, tout
