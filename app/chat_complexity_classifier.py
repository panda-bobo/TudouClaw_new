"""
Chat message complexity classifier.

Decides whether a chat message is:
    - "simple"   → run V1 chat path (single LLM call + tool use)
    - "complex"  → route to V2 TaskLoop (6-phase state machine, FIFO queue,
                   verify/deliver/report)

Strategy is rule-based first (zero-cost, deterministic) with an optional
LLM fallback for ambiguous cases. The rules are conservative on BOTH
sides:
    * small-talk / Q&A / single-tool asks → simple
    * multi-step asks or explicit delivery verbs → complex
    * the middle gets an LLM verdict

This module is pure / side-effect free so it can be imported from the
HTTP request path without extra setup.
"""
from __future__ import annotations

import logging
import re
from typing import Optional


logger = logging.getLogger("tudou.chat_classifier")


# ── Lexicons ──────────────────────────────────────────────────────────

# Delivery verbs — a hard signal that the user wants a tangible output.
_DELIVERY_VERBS = {
    "生成", "制作", "做一份", "做一个", "撰写", "写一份", "起草",
    "导出", "产出", "输出", "整理成", "汇总", "梳理成",
    "发邮件", "发送给", "发给我", "email", "send",
    "上传", "入库", "录入",
    "generate", "create", "produce", "write", "draft", "export",
    "compile", "compose",
}

# Multi-step connectives — the user is chaining steps.
_MULTI_STEP_CONNECTIVES = {
    "然后", "接着", "之后", "最后", "最终", "并", "同时",
    "先…再", "先...再",
    "and then", "then", "afterwards", "finally",
}

# Long-horizon targets — heavy operations with implicit multi-step work.
_HEAVY_TARGETS = {
    "报告", "PPT", "pptx", "ppt", "幻灯片", "纪要", "研究",
    "调研", "分析", "对比", "竞品", "市场", "方案", "计划书",
    "report", "research", "analysis", "comparison",
}

# Explicit "send to external destination" = definitely multi-step.
_EXTERNAL_DESTINATIONS = {
    "邮件", "邮箱", "@gmail", "@qq", "@outlook", "@hotmail",
    "slack", "lark", "飞书", "钉钉", "teams", "telegram",
}

# Small-talk / simple QA — short-circuit to V1.
_SIMPLE_QA_PREFIXES = (
    "你好", "hi", "hello", "嗨", "在吗", "在么",
    "谢谢", "thanks", "thank you",
    "?", "？",
)
_SIMPLE_QA_REGEX = re.compile(
    r"^(what|how|why|when|where|who|是什么|为什么|怎么|如何|啥|哪"
    r"|can you explain|能解释|能告诉我|tell me)",
    re.IGNORECASE,
)


# Email/phone-like token → often a destination, signals complex task.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


# ── Public API ────────────────────────────────────────────────────────


def classify(
    message: str,
    *,
    llm_fallback: Optional[callable] = None,
) -> dict:
    """Classify ``message`` as simple or complex.

    Returns::

        {
          "route":   "v1" | "v2",
          "reason":  str,          # human-readable rationale
          "signals": list[str],    # which rules fired
          "via":     "rules" | "llm" | "default",
        }

    ``llm_fallback`` is an optional callable accepting a message string
    and returning one of ``"simple"`` / ``"complex"``. Used only when
    rules don't give a confident verdict.
    """
    msg = (message or "").strip()
    if not msg:
        return {"route": "v1", "reason": "empty message", "signals": [], "via": "default"}

    signals: list[str] = []

    # 1. SHORT small-talk / single Q → V1, definitely.
    if len(msg) <= 20 and any(msg.lower().startswith(p) for p in _SIMPLE_QA_PREFIXES):
        return {"route": "v1", "reason": "short greeting/small-talk",
                "signals": ["short_smalltalk"], "via": "rules"}
    if len(msg) <= 40 and _SIMPLE_QA_REGEX.search(msg):
        return {"route": "v1", "reason": "short factual question",
                "signals": ["short_qa_prefix"], "via": "rules"}

    # 2. DELIVERY VERB + TARGET → V2 for sure.
    has_delivery = any(v.lower() in msg.lower() for v in _DELIVERY_VERBS)
    has_heavy = any(t.lower() in msg.lower() for t in _HEAVY_TARGETS)
    has_external = any(d.lower() in msg.lower() for d in _EXTERNAL_DESTINATIONS)
    has_email_token = bool(_EMAIL_RE.search(msg))
    has_multi_step = any(c.lower() in msg.lower() for c in _MULTI_STEP_CONNECTIVES)

    if has_delivery:
        signals.append("delivery_verb")
    if has_heavy:
        signals.append("heavy_target")
    if has_external or has_email_token:
        signals.append("external_destination")
    if has_multi_step:
        signals.append("multi_step_connective")

    # Strong positive indicator: delivery + (heavy OR external OR multi-step)
    strong_positive = has_delivery and (has_heavy or has_external or has_email_token or has_multi_step)
    if strong_positive:
        return {"route": "v2", "reason": "delivery + heavy/external signal",
                "signals": signals, "via": "rules"}

    # Two or more signals → V2 (conservative).
    if len(signals) >= 2:
        return {"route": "v2", "reason": "multiple complexity signals",
                "signals": signals, "via": "rules"}

    # Very long message — likely complex.
    if len(msg) >= 200:
        signals.append("long_message")
        return {"route": "v2", "reason": "long-form request",
                "signals": signals, "via": "rules"}

    # 3. AMBIGUOUS middle ground → ask the LLM if one was provided.
    #    - length 40..200
    #    - one weak signal or none
    if llm_fallback is not None and 40 <= len(msg) < 200:
        try:
            verdict = llm_fallback(msg)
            if verdict in ("simple", "complex"):
                return {
                    "route": "v2" if verdict == "complex" else "v1",
                    "reason": f"LLM classified as {verdict}",
                    "signals": signals,
                    "via": "llm",
                }
        except Exception as e:
            logger.debug("LLM classifier failed, defaulting to V1: %s", e)

    # 4. Default fallback: short+weak signals → V1.
    return {"route": "v1", "reason": "no strong complexity signal",
            "signals": signals, "via": "default"}


# ── Optional LLM fallback that plugs into app.llm ──────────────────────


def make_llm_fallback(call_llm_fn) -> callable:
    """Factory — wraps the repo's LLM bridge into the classifier's
    callable interface. Returns a function that answers
    ``"simple"`` / ``"complex"`` for a given message.

    Prompt is intentionally narrow so even a tiny model can classify well.
    """
    def _classify_via_llm(msg: str) -> str:
        system = (
            "Classify the user's message into exactly one of: SIMPLE or COMPLEX.\n"
            "SIMPLE = single question, chitchat, one-shot explanation, "
            "short factual lookup.\n"
            "COMPLEX = multi-step work (research + write + deliver), "
            "tangible artifact production, multiple tool calls in sequence, "
            "sending outputs to external destinations.\n"
            "Reply with ONLY one word: SIMPLE or COMPLEX."
        )
        try:
            resp = call_llm_fn(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": msg[:1000]},
                ],
                tools=None,
                max_tokens=8,
            )
        except Exception:
            return ""
        text = (resp.get("content") or "").strip().upper()
        if "COMPLEX" in text:
            return "complex"
        if "SIMPLE" in text:
            return "simple"
        return ""
    return _classify_via_llm


__all__ = ["classify", "make_llm_fallback"]
