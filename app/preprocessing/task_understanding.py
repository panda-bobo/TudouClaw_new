"""Phase: task understanding (first-turn-setup mode).

Bridge contract: ``run(agent, model, payload, timeout_s) ->
(value, tokens_in, tokens_out)``.

Payload shape:
    {
        "intent": "<user's first message in this task / conversation>",
    }

Returns:
    {
        "summary": "<1-2 sentence task understanding>",
        "complexity": "simple" | "medium" | "complex",
        "recommended_tier": "fast_cheap" | "default" | "reasoning_strong" | "coding_strong",
        "rag_needed": true | false,
        "decompose_needed": true | false,
        "estimated_steps": <int 1-10>,
    }

The orchestrator (caller) consumes this to:
  * Pick LLM tier for the actual task (skip strong model for simple Q&A)
  * Decide whether to trigger RAG retrieval (skip for trivial chat)
  * Decide whether to auto-call propose_decomposition

Why pipe-delimited (not JSON): 3B-class models are unreliable at strict
JSON. Single-line key:value pairs are forgiving — partial output still
parses, missing fields fall back to defaults.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ._client import chat_completion

logger = logging.getLogger("tudou.preprocessing.task_understanding")


_VALID_TIERS = ("fast_cheap", "default", "reasoning_strong",
                "coding_strong", "writing_strong", "creative")
_VALID_COMPLEXITY = ("simple", "medium", "complex")


# Single-turn user prompt — small models reliable with this style.
_USER_PROMPT_TEMPLATE_ZH = (
    "分析以下用户请求，输出 5 行格式化结果（严格按格式，不要多余解释）：\n"
    "summary: <1-2 句话总结这是什么类型的任务>\n"
    "complexity: <simple | medium | complex>\n"
    "tier: <fast_cheap | default | reasoning_strong | coding_strong>\n"
    "rag: <yes | no>\n"
    "decompose: <yes | no>\n"
    "steps: <1-10>\n"
    "\n"
    "判断依据：\n"
    "- complexity: 一句话回答 → simple；多步操作或推理 → medium；跨文件/跨系统/长报告 → complex\n"
    "- tier: 简单聊天 → fast_cheap；普通任务 → default；推理/规划重 → reasoning_strong；写代码 → coding_strong\n"
    "- rag: 需要查文档/历史/规范 → yes；纯生成/闲聊 → no\n"
    "- decompose: 任务可拆为 ≥3 个独立步骤 → yes；单步任务 → no\n"
    "- steps: 你估计完成需要的步骤数（含工具调用、审批、复盘）\n"
    "\n"
    "## 用户请求\n"
    "{intent}\n"
    "\n"
    "## 你的分析（只输出 6 行 key: value）"
)


def _parse_understanding(text: str) -> dict:
    """Parse ``key: value`` lines into a dict. Tolerant — missing keys
    get sensible defaults.
    """
    out = {
        "summary": "",
        "complexity": "medium",
        "recommended_tier": "default",
        "rag_needed": False,
        "decompose_needed": False,
        "estimated_steps": 3,
    }
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        # Strip surrounding quotes / brackets if model added them
        val = val.strip('"\'`<>[]')
        if not val:
            continue
        if key == "summary":
            out["summary"] = val[:300]
        elif key == "complexity":
            v = val.lower()
            if v in _VALID_COMPLEXITY:
                out["complexity"] = v
        elif key == "tier":
            v = val.lower()
            # Tolerate model-added prefixes ("tier_fast_cheap", "fast cheap")
            v = v.replace(" ", "_").replace("-", "_")
            if v in _VALID_TIERS:
                out["recommended_tier"] = v
        elif key == "rag":
            out["rag_needed"] = val.lower() in ("yes", "true", "1", "needed", "y")
        elif key == "decompose":
            out["decompose_needed"] = val.lower() in ("yes", "true", "1", "needed", "y")
        elif key == "steps":
            try:
                n = int(re.search(r"\d+", val).group(0))
                out["estimated_steps"] = max(1, min(n, 10))
            except (AttributeError, ValueError):
                pass
    return out


def run(*, agent, model: str, payload: dict, timeout_s: float = 5.0):
    intent = (payload or {}).get("intent") or ""
    if not intent or not intent.strip():
        return {
            "summary": "",
            "complexity": "simple",
            "recommended_tier": "fast_cheap",
            "rag_needed": False,
            "decompose_needed": False,
            "estimated_steps": 1,
        }, 0, 0

    endpoint = getattr(agent, "preprocessor_endpoint", "") or ""
    user_msg = _USER_PROMPT_TEMPLATE_ZH.format(intent=intent[:1000])
    messages = [{"role": "user", "content": user_msg}]

    content, tin, tout = chat_completion(
        endpoint=endpoint,
        model=model,
        messages=messages,
        temperature=0.0,
        # ~6 lines × 50 chars = ~300 chars / ~75 tokens. 200 gives headroom.
        max_tokens=200,
        timeout_s=timeout_s,
    )

    return _parse_understanding(content or ""), tin, tout
