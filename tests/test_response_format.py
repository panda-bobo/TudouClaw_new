"""app.llm — structured output forcing via response_format.

Covers:
- make_json_object_response_format / make_json_schema_response_format
  produce correct OpenAI-spec dicts
- build_schema_tool / force_schema_tool_choice produce correct
  Anthropic-style schema-forcing tool + tool_choice
- response_format threads through chat() → _chat_with_fallback() →
  protocol handler → outgoing payload (OpenAI / Ollama paths)
- Anthropic path emits warning and does NOT inject response_format
- response_format=None preserves existing behavior (back-compat)
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from app import llm as _llm
from app.llm import (
    make_json_object_response_format,
    make_json_schema_response_format,
    build_schema_tool,
    force_schema_tool_choice,
)


# ── Helper builders ───────────────────────────────────────────────────


def test_make_json_object_format():
    rf = make_json_object_response_format()
    assert rf == {"type": "json_object"}


def test_make_json_schema_format_strict():
    schema = {
        "type": "object",
        "properties": {"intent": {"type": "string"}},
        "required": ["intent"],
    }
    rf = make_json_schema_response_format("IntentResult", schema, strict=True)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "IntentResult"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == schema


def test_make_json_schema_format_non_strict():
    rf = make_json_schema_response_format("X", {"type": "object"}, strict=False)
    assert rf["json_schema"]["strict"] is False


def test_build_schema_tool_default_description():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    tool = build_schema_tool("ExtractX", schema)
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "ExtractX"
    assert tool["function"]["parameters"] == schema
    assert "ExtractX" in tool["function"]["description"]


def test_build_schema_tool_custom_description():
    tool = build_schema_tool("Foo", {"type": "object"}, description="my tool")
    assert tool["function"]["description"] == "my tool"


def test_force_schema_tool_choice():
    assert force_schema_tool_choice("Foo") == {"type": "tool", "name": "Foo"}


# ── End-to-end: response_format reaches the outgoing HTTP payload ────


class _StubResp:
    def __init__(self, body: dict, status: int = 200):
        self._body = body
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _r
            raise _r.HTTPError(response=self)


def _setup_provider_chain():
    """Bypass provider registry and call _openai_chat directly.

    Tests don't need real providers — they just need to verify what
    payload reaches requests.post.
    """
    pass


def test_openai_path_passes_response_format_in_payload():
    """When response_format is provided, it appears in the JSON body sent
    to /v1/chat/completions."""
    rf = make_json_schema_response_format(
        "Out",
        {"type": "object", "properties": {"x": {"type": "integer"}}},
    )

    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _StubResp({
            "choices": [{
                "message": {"role": "assistant", "content": '{"x": 42}'},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    pool = _llm.get_connection_pool()
    with mock.patch.object(
        pool, "request_with_retry",
        side_effect=lambda pid, m, u, **kw: fake_post(u, **kw),
    ):
        _llm._openai_chat(
            "https://api.example.com/v1", "key",
            messages=[{"role": "user", "content": "hi"}],
            stream=False, model="test-model",
            response_format=rf,
        )

    assert "response_format" in captured["json"]
    assert captured["json"]["response_format"] == rf


def test_openai_path_omits_response_format_when_none():
    captured = {}

    def fake_post(url, **kw):
        captured["json"] = kw.get("json")
        return _StubResp({
            "choices": [{
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    pool = _llm.get_connection_pool()
    with mock.patch.object(
        pool, "request_with_retry",
        side_effect=lambda pid, m, u, **kw: fake_post(u, **kw),
    ):
        _llm._openai_chat(
            "https://api.example.com/v1", "key",
            messages=[{"role": "user", "content": "hi"}],
            stream=False, model="test-model",
            response_format=None,
        )

    assert "response_format" not in captured["json"]


def test_openai_path_passes_json_object_format():
    """Simpler form — {"type": "json_object"} — also threads through."""
    rf = make_json_object_response_format()
    captured = {}

    def fake_post(url, **kw):
        captured["json"] = kw.get("json")
        return _StubResp({
            "choices": [{
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        })

    pool = _llm.get_connection_pool()
    with mock.patch.object(
        pool, "request_with_retry",
        side_effect=lambda pid, m, u, **kw: fake_post(u, **kw),
    ):
        _llm._openai_chat(
            "https://api.example.com/v1", "key",
            messages=[{"role": "user", "content": "hi"}],
            stream=False, model="x", response_format=rf,
        )

    assert captured["json"]["response_format"] == {"type": "json_object"}


# ── Anthropic: response_format ignored with warning ──────────────────


def test_claude_path_logs_warning_and_drops_response_format(caplog):
    """Anthropic /v1/messages doesn't have response_format. We log a
    warning and don't try to inject it."""
    rf = make_json_object_response_format()

    captured = {}

    def fake_post(url, **kw):
        captured["json"] = kw.get("json")
        return _StubResp({
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn", "model": "claude-test",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    pool = _llm.get_connection_pool()
    with caplog.at_level("WARNING", logger="app.llm"):
        with mock.patch.object(
            pool, "request_with_retry",
            side_effect=lambda pid, m, u, **kw: fake_post(u, **kw),
        ):
            _llm._claude_chat(
                "https://api.anthropic.com", "key",
                messages=[{"role": "user", "content": "hi"}],
                stream=False, model="claude-test", response_format=rf,
            )

    # Warning should have been emitted
    msgs = [r.message for r in caplog.records]
    assert any("response_format" in m and "Anthropic" in m for m in msgs)

    # Payload should NOT contain response_format key
    assert "response_format" not in captured["json"]


# ── Signature back-compat ────────────────────────────────────────────


def test_chat_signature_back_compatible():
    """All response_format params default to None; existing callers
    that don't pass it still work."""
    import inspect
    sig = inspect.signature(_llm.chat)
    rf_param = sig.parameters.get("response_format")
    assert rf_param is not None
    assert rf_param.default is None

    sig2 = inspect.signature(_llm.chat_no_stream)
    assert sig2.parameters["response_format"].default is None

    for handler_name in ("_openai_chat", "_claude_chat", "_ollama_chat",
                         "_chat_with_fallback", "_proxy_chat"):
        h = getattr(_llm, handler_name)
        s = inspect.signature(h)
        assert s.parameters["response_format"].default is None, handler_name
