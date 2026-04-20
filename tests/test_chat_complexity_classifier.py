"""Tests for the chat complexity classifier.

We care about both **precision** (don't wrongly route small-talk to V2)
and **recall** (don't miss an obviously complex task).

Each test below encodes a concrete user sentence we want classified a
specific way, so a future rule tweak that breaks one of these routes
is caught immediately.
"""
from __future__ import annotations

import pytest

from app.chat_complexity_classifier import classify, make_llm_fallback


# ── SIMPLE path — must stay on V1 ─────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "你好",
    "hi",
    "在吗",
    "谢谢",
    "?",
    "什么是 kubernetes",
    "为什么天是蓝的",
    "how does caching work",
    "今天星期几",
    "帮我解释一下 RAG",
])
def test_simple_messages_route_to_v1(msg):
    r = classify(msg)
    assert r["route"] == "v1", f"expected v1 for {msg!r}, got {r}"


# ── COMPLEX path — must flip to V2 ────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "小土，你洞察一下中东中亚 VMware 的市场，竞争，用户，产品能力，等形成一个总结报告给我。发我 pang@gmail.com 邮箱",
    "调研 HCS 产品在中东市场的差异化，生成 PPT，发到我邮箱",
    "Research Huawei Cloud Stack competitors and draft a comparison report, email it to me.",
    "帮我梳理团队一季度的 OKR 完成情况，生成一份 PPT 发给老板",
    "分析这三家供应商的能力，做成表格然后发飞书给我",
    # pure delivery verb + heavy target — enough signals
    "生成一份关于 AI agent 市场的研究报告",
    # multi-step connective present
    "先用 web_search 查找最新资料然后整理成文档",
])
def test_complex_messages_route_to_v2(msg):
    r = classify(msg)
    assert r["route"] == "v2", f"expected v2 for {msg!r}, got {r}"


# ── Boundary cases ────────────────────────────────────────────────────


def test_empty_goes_to_v1():
    assert classify("")["route"] == "v1"
    assert classify("   ")["route"] == "v1"


def test_long_message_even_without_keywords_goes_v2():
    """A >200 char message without ANY keywords still goes V2 based on
    length alone. Use deliberately keyword-free content."""
    msg = (
        "这段话比较长说的是我们最近开会讨论的事情。大家都在说自己的想法，"
        "有人觉得要快一点推进，有人觉得要稳一点。我听完之后在思考。"
        "按照道理应该是这样考虑问题。"
    ) * 3
    assert len(msg) > 200
    r = classify(msg)
    assert r["route"] == "v2"
    assert "long_message" in r["signals"]


def test_ambiguous_middle_ground_uses_llm_fallback_when_provided():
    """A 40–200 char message with no strong signals: LLM gets a turn."""
    msg = (
        "帮我想想这个事情怎么办比较好呢，我有点纠结要不要"
        "直接上，感觉时机不太对但又怕错过窗口期，你怎么看"
    )
    assert 40 <= len(msg) < 200
    seen = {"called": False}

    def fake_llm(m):
        seen["called"] = True
        return "complex"  # LLM says complex

    r = classify(msg, llm_fallback=fake_llm)
    assert seen["called"] is True
    assert r["route"] == "v2"
    assert r["via"] == "llm"


def test_ambiguous_without_llm_fallback_defaults_to_v1():
    msg = "帮我想想这个事情怎么办比较好呢"
    r = classify(msg)
    # No LLM provided, rule doesn't fire strongly → V1 default.
    assert r["route"] == "v1"
    assert r["via"] in ("rules", "default")


def test_email_token_is_a_strong_external_signal():
    """Having an email address + any delivery verb → V2 immediately."""
    r = classify("生成月报 pang@example.com")
    assert r["route"] == "v2"
    assert "external_destination" in r["signals"]


# ── LLM fallback factory ──────────────────────────────────────────────


def test_llm_fallback_parses_complex():
    def call_llm(*, messages, tools=None, max_tokens=8):
        return {"role": "assistant", "content": "COMPLEX"}
    fallback = make_llm_fallback(call_llm)
    assert fallback("doesn't matter") == "complex"


def test_llm_fallback_parses_simple():
    def call_llm(*, messages, tools=None, max_tokens=8):
        return {"role": "assistant", "content": "SIMPLE — easy ask"}
    fallback = make_llm_fallback(call_llm)
    assert fallback("doesn't matter") == "simple"


def test_llm_fallback_empty_response_returns_empty():
    def call_llm(*, messages, tools=None, max_tokens=8):
        return {"role": "assistant", "content": "   "}
    fallback = make_llm_fallback(call_llm)
    assert fallback("anything") == ""


def test_llm_fallback_exception_returns_empty():
    def call_llm(*, messages, tools=None, max_tokens=8):
        raise RuntimeError("network down")
    fallback = make_llm_fallback(call_llm)
    assert fallback("anything") == ""
