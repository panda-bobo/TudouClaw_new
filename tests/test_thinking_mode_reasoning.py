"""Tests for the proactive reasoning_content injection (2026-05-13).

User report: agent's chat to MiMo (Xiaomi mimo-v2.5-pro) failed with:

    400 Param Incorrect — The reasoning_content in the thinking mode
    must be passed back to the API.

Root cause: assistant message in history had tool_calls=[6 items]
but content=None and no reasoning_content. MiMo / DeepSeek-thinking
/ o1 / qwq all require reasoning_content to be present on assistant
messages that participate in tool_calls round-trips.

Fix: in _sanitize_messages_for_openai, when the target URL/model is
a known thinking-mode endpoint AND the assistant has tool_calls but
no reasoning_content, inject a non-empty placeholder.

The existing reactive recovery (line ~2908) trims to system+last_user
on 400 — but only in the non-streaming path. Streaming raises before
recovery can run. Proactive injection covers both paths.
"""
from __future__ import annotations

import pytest

from app.llm import (
    _is_thinking_mode_target,
    _sanitize_messages_for_openai,
)


# ── Detection ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://token-plan-sgp.xiaomimimo.com/v1/chat/completions", True),
    ("https://api.deepseek.com/v1/chat/completions", True),
    ("https://api.deepseek.ai/v1/chat/completions", True),
    ("https://ark.cn-beijing.volces.com/api/v3/chat/completions", True),
    ("https://api.openai.com/v1/chat/completions", False),
    ("http://localhost:1234/v1/chat/completions", False),
    ("", False),
])
def test_thinking_mode_url_detection(url, expected):
    assert _is_thinking_mode_target(url, "") is expected


@pytest.mark.parametrize("model,expected", [
    ("mimo-v2.5-pro", True),
    ("deepseek-r1", True),
    ("deepseek-r-1-distill", True),
    ("deepseek-v4-pro", True),
    ("o1-preview", True),
    ("o1-mini", True),
    ("qwq-32b-preview", True),
    ("anything-thinking", True),
    ("gpt-4o", False),
    ("claude-sonnet-4", False),
    ("qwen3-coder-480b", False),
    ("", False),
])
def test_thinking_mode_model_detection(model, expected):
    assert _is_thinking_mode_target("", model) is expected


def test_either_match_is_enough():
    """Conservative: URL match alone OR model match alone is sufficient."""
    # URL says thinking, model doesn't
    assert _is_thinking_mode_target(
        "https://api.deepseek.com/v1/chat", "gpt-4o") is True
    # Model says thinking, URL doesn't
    assert _is_thinking_mode_target(
        "http://localhost:1234/v1", "deepseek-r1") is True


# ── Placeholder injection ─────────────────────────────────────────────

def test_assistant_with_tool_calls_no_reasoning_gets_placeholder():
    """The reported bug. assistant has tool_calls + no reasoning_content
    → MiMo would 400. Sanitizer injects placeholder."""
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "do X"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_00",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "x"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_00", "content": "result"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_model="mimo-v2.5-pro")

    asst = next(m for m in out if m.get("role") == "assistant")
    rc = asst.get("reasoning_content", "")
    assert rc, "placeholder not injected"
    assert "no chain-of-thought" in rc.lower()
    # tool_calls preserved
    assert asst.get("tool_calls")


def test_existing_reasoning_content_preserved():
    """If the model DID return reasoning_content, leave it intact."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Let me think about this carefully...",
            "tool_calls": [{
                "id": "call_00",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_00", "content": "ok"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_model="mimo-v2.5-pro")
    asst = next(m for m in out if m.get("role") == "assistant")
    assert asst["reasoning_content"] == "Let me think about this carefully..."


def test_non_thinking_target_no_injection():
    """For OpenAI / Claude / non-thinking models, don't inject — they
    might 400 on unknown reasoning_content field."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_00",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_00", "content": "ok"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_model="gpt-4o")
    asst = next(m for m in out if m.get("role") == "assistant")
    assert "reasoning_content" not in asst


def test_assistant_with_text_content_gets_backfill_from_adapter():
    """Even text-only assistant gets reasoning_content="" from the
    MiMo adapter's backfill_reasoning_content=True. Per the MiMo
    contract, the field must be PRESENT on every assistant turn (the
    adapter handles non-tool-call ones; my injection handles
    tool-call ones with a richer placeholder)."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_model="mimo-v2.5-pro")
    asst = next(m for m in out if m.get("role") == "assistant")
    # Field present (empty value is fine per backfill behaviour)
    assert "reasoning_content" in asst
    # Real text content preserved
    assert asst.get("content") == "hello back"


def test_empty_string_reasoning_treated_as_missing():
    """reasoning_content = '' or whitespace-only counts as missing —
    placeholder fills it."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "   ",  # only whitespace
            "tool_calls": [{
                "id": "call_00",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_00", "content": "ok"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_model="mimo-v2.5-pro")
    asst = next(m for m in out if m.get("role") == "assistant")
    assert asst.get("reasoning_content", "").strip()
    assert "no chain-of-thought" in asst["reasoning_content"].lower()


def test_surviving_assistants_get_placeholder():
    """Each assistant with tool_calls that SURVIVES the pipeline gets
    a non-empty reasoning_content (either from my placeholder
    injection, or from the adapter's backfill if the placeholder
    was empty before my code ran).

    Note: MiMo's max_tool_call_rounds=1 means older tool rounds get
    folded into a single text user msg by `fold_excess_tool_rounds`,
    so multi-round histories are condensed. The test asserts that
    whichever assistants SURVIVE all carry the field."""
    messages = [
        {"role": "user", "content": "do X then Y"},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_0",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "x done"},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "y", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "y done"},
    ]
    out = _sanitize_messages_for_openai(
        messages, target_url="https://xiaomimimo.com/v1/chat")
    assistants = [m for m in out if m.get("role") == "assistant"]
    # At least one assistant survives (the most recent tool round).
    assert len(assistants) >= 1
    for a in assistants:
        # Field present (the contract MiMo requires)
        assert "reasoning_content" in a, (
            "every assistant on MiMo target must have reasoning_content")


def test_url_alone_triggers_injection():
    """Even when the model name doesn't suggest thinking-mode, a known
    thinking-mode URL forces injection (covers proxies / fronts)."""
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c0",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "c0", "content": "ok"},
    ]
    out = _sanitize_messages_for_openai(
        messages,
        target_url="https://api.deepseek.com/v1/chat/completions",
        target_model="custom-model-name")
    asst = next(m for m in out if m.get("role") == "assistant")
    assert (asst.get("reasoning_content") or "").strip()
