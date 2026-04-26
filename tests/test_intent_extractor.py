"""app.intent_extractor — 中文短指令意图提取。

Covers:
- should_extract heuristic gate (短输入触发,长/结构化输入跳过)
- extract_intent with mocked LLM caller — multiple intent types
- missing_required derivation (LLM-provided + heuristic fallback)
- IntentResult.should_clarify / clarifying_questions / as_system_block
- Tolerant JSON parsing (markdown fences, prefix text)
- extractor_failed paths (LLM raises, malformed output)
"""
from __future__ import annotations

import json

import pytest

from app.intent_extractor import (
    IntentResult,
    extract_intent,
    should_extract,
)


# ── should_extract ────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "做个 PPT",
    "搞一下",
    "看看",
    "帮我写个 doc",
    "生成报表",
])
def test_short_chinese_inputs_trigger_extraction(msg):
    assert should_extract(msg) is True


@pytest.mark.parametrize("msg", [
    "请基于 /Users/me/data.csv 生成一个 Q3 销售复盘报告,受众是 CEO 和 CFO,要求 12-15 页,包含趋势图、对标分析、改进建议",
    "帮我看这个 https://example.com/foo.html 的内容",
    "@coder 处理一下 {action: deploy}",
    'json: {"task": "review"}',
    "```python\nprint(1)\n```",
])
def test_long_or_structured_inputs_bypass_extraction(msg):
    assert should_extract(msg) is False


def test_empty_input_does_not_extract():
    assert should_extract("") is False
    assert should_extract("   ") is False


# ── extract_intent — happy paths ──────────────────────────────────────


def _mock_caller(response_text: str):
    """Return a closure that ignores prompt and returns the canned response."""
    def _c(_prompt: str) -> str:
        return response_text
    return _c


def test_extract_pptx_with_missing_fields():
    fake = json.dumps({
        "intent": "create_pptx",
        "deliverable_type": "pptx",
        "topic": None,
        "audience": None,
        "page_count": None,
        "missing_required": ["topic", "audience", "page_count"],
    })
    r = extract_intent("做个 PPT", llm_caller=_mock_caller(fake))
    assert r.intent == "create_pptx"
    assert r.deliverable_type == "pptx"
    assert r.topic is None
    assert r.should_clarify is True
    assert "topic" in r.missing_required
    assert r.extractor_failed is False


def test_extract_pptx_complete():
    fake = json.dumps({
        "intent": "create_pptx",
        "deliverable_type": "pptx",
        "topic": "AI 安全",
        "audience": "高管",
        "page_count": 12,
        "missing_required": [],
    })
    r = extract_intent("AI 安全 给高管看 12 页", llm_caller=_mock_caller(fake))
    assert r.topic == "AI 安全"
    assert r.audience == "高管"
    assert r.page_count == 12
    assert r.should_clarify is False


def test_missing_required_derived_when_llm_omits_field():
    """If LLM returns intent + topic only (no missing_required field), we
    derive it from _REQUIRED_BY_INTENT."""
    fake = json.dumps({
        "intent": "create_pptx",
        "topic": "AI 安全",
        # No audience, no page_count, no missing_required
    })
    r = extract_intent("做个 AI 安全 PPT", llm_caller=_mock_caller(fake))
    assert "audience" in r.missing_required
    assert "page_count" in r.missing_required
    assert "topic" not in r.missing_required  # already filled


def test_search_info_intent_only_needs_topic():
    fake = json.dumps({
        "intent": "search_info",
        "topic": "Q3 行业政策变化",
    })
    r = extract_intent("查一下 Q3 行业政策", llm_caller=_mock_caller(fake))
    assert r.missing_required == []  # heuristic: only topic required, present
    assert r.should_clarify is False


def test_casual_chat_no_required_fields():
    fake = json.dumps({"intent": "casual_chat"})
    r = extract_intent("早上好", llm_caller=_mock_caller(fake))
    assert r.should_clarify is False


# ── IntentResult helpers ─────────────────────────────────────────────


def test_clarifying_questions_chinese_text():
    r = IntentResult(
        intent="create_pptx",
        missing_required=["topic", "audience", "page_count"],
    )
    q = r.clarifying_questions()
    assert "主题" in q
    assert "受众" in q
    assert "页数" in q


def test_clarifying_questions_empty_when_complete():
    r = IntentResult(intent="create_pptx", topic="X", audience="Y", page_count=10)
    assert r.clarifying_questions() == ""


def test_as_system_block_only_when_complete():
    """should_clarify=True → as_system_block returns empty (not a useful hint)."""
    incomplete = IntentResult(
        intent="create_pptx",
        topic=None,
        missing_required=["topic"],
    )
    # should_clarify is True → block still has 'intent' line so non-empty,
    # but typically caller checks should_clarify first
    block = incomplete.as_system_block()
    assert "intent: create_pptx" in block
    # No topic line (topic is None)
    assert "topic:" not in block

    complete = IntentResult(
        intent="create_pptx",
        deliverable_type="pptx",
        topic="AI 安全",
        audience="高管",
        page_count=12,
    )
    block = complete.as_system_block()
    assert "[USER_INTENT]" in block
    assert "intent: create_pptx" in block
    assert "topic: AI 安全" in block
    assert "audience: 高管" in block
    assert "page_count: 12" in block


def test_as_system_block_returns_empty_on_failure():
    r = IntentResult(extractor_failed=True)
    assert r.as_system_block() == ""


# ── Tolerant JSON parsing ─────────────────────────────────────────────


def test_parses_markdown_fenced_json():
    fake = '```json\n{"intent": "create_pptx", "topic": "X"}\n```'
    r = extract_intent("做个 PPT", llm_caller=_mock_caller(fake))
    assert r.intent == "create_pptx"
    assert r.topic == "X"


def test_parses_json_with_prefix_text():
    fake = '好的,以下是分析结果:\n\n{"intent": "search_info", "topic": "Q3 政策"}'
    r = extract_intent("查 Q3 政策", llm_caller=_mock_caller(fake))
    assert r.intent == "search_info"
    assert r.topic == "Q3 政策"


def test_unparsable_response_returns_extractor_failed():
    fake = "这不是有效的 JSON,模型乱说了"
    r = extract_intent("做个 PPT", llm_caller=_mock_caller(fake))
    assert r.extractor_failed is True
    # Caller should NOT clarify based on this — fallback to old path
    assert r.should_clarify is False


def test_llm_caller_raises_returns_extractor_failed():
    def boom(_prompt):
        raise ConnectionError("network down")

    r = extract_intent("做个 PPT", llm_caller=boom)
    assert r.extractor_failed is True
    assert r.should_clarify is False


def test_no_llm_caller_returns_safe_default():
    """When caller passes llm_caller=None, we return a safe 'unknown' result
    that does NOT trigger clarification (back-compat for callers that
    haven't wired up an extractor yet)."""
    r = extract_intent("做个 PPT", llm_caller=None)
    assert r.intent == "unknown"
    assert r.extractor_failed is False
    assert r.should_clarify is False


# ── Type coercion edge cases ─────────────────────────────────────────


def test_string_null_treated_as_none():
    fake = json.dumps({
        "intent": "create_doc",
        "topic": "null",        # LLM literal "null" string
        "audience": "",         # empty string
        "page_count": 0,        # zero is also "missing"
    })
    r = extract_intent("写个文档", llm_caller=_mock_caller(fake))
    assert r.topic is None
    assert r.audience is None
    assert r.page_count is None


def test_page_count_string_coerces_to_int():
    fake = json.dumps({"intent": "create_pptx", "page_count": "12"})
    r = extract_intent("做 12 页", llm_caller=_mock_caller(fake))
    assert r.page_count == 12


def test_page_count_garbage_returns_none():
    fake = json.dumps({"intent": "create_pptx", "page_count": "many"})
    r = extract_intent("做几页", llm_caller=_mock_caller(fake))
    assert r.page_count is None


# ── Empty / edge inputs ──────────────────────────────────────────────


def test_empty_user_message_returns_failure():
    r = extract_intent("", llm_caller=_mock_caller("{}"))
    assert r.extractor_failed is True


def test_non_dict_json_response_returns_failure():
    fake = json.dumps(["not", "a", "dict"])
    r = extract_intent("做 PPT", llm_caller=_mock_caller(fake))
    assert r.extractor_failed is True
