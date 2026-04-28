"""Phase: prompt optimization (compress / restructure).

Bridge contract: ``run(agent, model, payload, timeout_s) ->
(value, tokens_in, tokens_out)``.

Payload shape:
    {
        "prompt": "<the long prompt to compress>",
        "context": {  # optional
            "language": "zh" | "en",
            "preserve_sections": ["tools", "constraints"],
        }
    }

Returns:
    {
        "prompt": "<compressed prompt>",
        "saved_tokens": <int estimate>,
        "original_chars": <int>,
        "compressed_chars": <int>,
    }

The compression instruction is hardcoded — small models follow it
reliably. We don't ask for JSON output (3B-class models are unreliable
at structured output); we just take the raw text back.
"""
from __future__ import annotations

import logging
from typing import Any

from ._client import chat_completion

logger = logging.getLogger("tudou.preprocessing.prompt_optimize")


# Single-turn user prompt — small models (3-7B) handle this reliably,
# system+user splits often fail (model treats system as conversation
# context and replies to user instead of operating on it).
_USER_PROMPT_TEMPLATE_ZH = (
    "下面三个反引号之间是一段较长的 prompt。你的任务是压缩它到原长度一半以下，"
    "保留所有约束、工具名、目标、关键参数，删除：客套话、重复表述、冗余举例、修饰副词。"
    "直接输出压缩后的纯文本，不要解释、不要添加 markdown 标题、不要写\"以下是\"这种开头语。\n\n"
    "```\n{prompt}\n```\n\n"
    "压缩后的 prompt:"
)


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def run(*, agent, model: str, payload: dict, timeout_s: float = 3.0):
    prompt = (payload or {}).get("prompt") or ""
    if not prompt:
        return {
            "prompt": "",
            "saved_tokens": 0,
            "original_chars": 0,
            "compressed_chars": 0,
        }, 0, 0

    endpoint = getattr(agent, "preprocessor_endpoint", "") or ""

    user_msg = _USER_PROMPT_TEMPLATE_ZH.format(prompt=prompt)
    messages = [
        {"role": "user", "content": user_msg},
    ]

    content, tin, tout = chat_completion(
        endpoint=endpoint,
        model=model,
        messages=messages,
        temperature=0.0,
        # Cap output to roughly half the input — that's the compression target.
        max_tokens=max(128, _est_tokens(prompt) // 2 + 80),
        timeout_s=timeout_s,
    )

    optimized = (content or "").strip()
    # Sanity: if the small model produced something longer than the
    # original (failure mode), discard and signal no benefit.
    if len(optimized) >= len(prompt):
        logger.debug(
            "prompt_optimize: model produced longer output (%d >= %d); discarding",
            len(optimized), len(prompt),
        )
        return {
            "prompt": prompt,  # return original
            "saved_tokens": 0,
            "original_chars": len(prompt),
            "compressed_chars": len(prompt),
            "no_improvement": True,
        }, tin, tout

    saved = _est_tokens(prompt) - _est_tokens(optimized)
    return {
        "prompt": optimized,
        "saved_tokens": max(0, saved),
        "original_chars": len(prompt),
        "compressed_chars": len(optimized),
    }, tin, tout
