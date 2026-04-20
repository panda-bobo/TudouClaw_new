"""Tests for the tool-call parser plugin layer."""
from __future__ import annotations

import pytest

from app.v2.bridges.tool_parsers import (
    NormalizedMessage,
    ParserRegistry,
    register,
    get_registry,
)
from app.v2.bridges.tool_parsers.builtin import (
    OpenAIPassthroughParser,
    XMLTagJSONParser,
    JSONOnlyParser,
)


# ── OpenAIPassthroughParser ───────────────────────────────────────────


def test_passthrough_preserves_structured_tool_calls():
    parser = OpenAIPassthroughParser()
    raw = {
        "role": "assistant",
        "content": "thinking...",
        "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": "{\"q\":1}"}}
        ],
    }
    out = parser.parse(raw)
    assert out.role == "assistant"
    assert out.content == "thinking..."
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0]["function"]["name"] == "search"


def test_passthrough_no_tool_calls():
    out = OpenAIPassthroughParser().parse(
        {"role": "assistant", "content": "hi", "tool_calls": []})
    assert out.tool_calls == []
    assert out.content == "hi"


def test_passthrough_anthropic_input_shape():
    """Anthropic adapter variants use ``input`` instead of ``arguments``."""
    raw = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "name": "get_weather", "input": {"city": "SF"}}
        ],
    }
    out = OpenAIPassthroughParser().parse(raw)
    assert out.tool_calls[0]["function"]["name"] == "get_weather"
    # Arguments always stringified to JSON per OpenAI spec.
    import json
    args = json.loads(out.tool_calls[0]["function"]["arguments"])
    assert args == {"city": "SF"}


# ── XMLTagJSONParser (Qwen / Hermes / …) ──────────────────────────────


def test_xml_tag_single_call():
    parser = XMLTagJSONParser()
    raw = {"role": "assistant",
           "content": 'thinking...<tool_call>{"name":"web_search","arguments":{"q":"x"}}</tool_call>'}
    out = parser.parse(raw)
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0]["function"]["name"] == "web_search"
    import json
    assert json.loads(out.tool_calls[0]["function"]["arguments"]) == {"q": "x"}
    # Strip_content default True → tag removed.
    assert "<tool_call>" not in out.content


def test_xml_tag_multiple_calls():
    parser = XMLTagJSONParser()
    raw = {"role": "assistant",
           "content": ('<tool_call>{"name":"a","arguments":{}}</tool_call>'
                       '<tool_call>{"name":"b","arguments":{"k":1}}</tool_call>')}
    out = parser.parse(raw)
    assert [tc["function"]["name"] for tc in out.tool_calls] == ["a", "b"]


def test_xml_tag_custom_markers():
    parser = XMLTagJSONParser(open_tag="[TC]", close_tag="[/TC]")
    raw = {"role": "assistant",
           "content": '[TC]{"name":"go","arguments":{}}[/TC]'}
    out = parser.parse(raw)
    assert out.tool_calls[0]["function"]["name"] == "go"


def test_xml_tag_with_native_calls_merges():
    """If the response already contained structured tool_calls AND has
    XML markers, we merge both."""
    parser = XMLTagJSONParser()
    raw = {
        "role": "assistant",
        "content": '<tool_call>{"name":"extra","arguments":{}}</tool_call>',
        "tool_calls": [
            {"id": "c1", "function": {"name": "native", "arguments": "{}"}}
        ],
    }
    out = parser.parse(raw)
    names = [tc["function"]["name"] for tc in out.tool_calls]
    assert "native" in names and "extra" in names


def test_xml_tag_tolerates_malformed_json():
    """``json_repair`` fixes unquoted keys / trailing commas."""
    parser = XMLTagJSONParser()
    raw = {"role": "assistant",
           "content": '<tool_call>{name:"x",arguments:{q:"y",},}</tool_call>'}
    out = parser.parse(raw)
    # Should recover at least the name; args may or may not parse.
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0]["function"]["name"] == "x"


def test_xml_tag_unclosed_marker_ignored():
    parser = XMLTagJSONParser()
    raw = {"role": "assistant",
           "content": '<tool_call>{"name":"x","arguments":{}}'}  # no close tag
    out = parser.parse(raw)
    assert out.tool_calls == []
    assert "<tool_call>" in out.content  # untouched when no match


def test_xml_tag_no_strip_content():
    parser = XMLTagJSONParser(strip_content=False)
    raw = {"role": "assistant",
           "content": 'prelude <tool_call>{"name":"x","arguments":{}}</tool_call> postlude'}
    out = parser.parse(raw)
    assert "<tool_call>" in out.content


# ── JSONOnlyParser ────────────────────────────────────────────────────


def test_json_only_function_call():
    parser = JSONOnlyParser()
    raw = {"role": "assistant",
           "content": '{"function_call": {"name": "x", "arguments": {"k": 1}}}'}
    out = parser.parse(raw)
    assert out.tool_calls[0]["function"]["name"] == "x"
    assert out.content == ""


def test_json_only_custom_path():
    parser = JSONOnlyParser(name_key="tool", args_key="args")
    raw = {"role": "assistant",
           "content": '{"tool": "search", "args": {"q": "foo"}}'}
    out = parser.parse(raw)
    assert out.tool_calls[0]["function"]["name"] == "search"


def test_json_only_no_match_leaves_content():
    parser = JSONOnlyParser()
    raw = {"role": "assistant", "content": "just text"}
    out = parser.parse(raw)
    assert out.tool_calls == []
    assert out.content == "just text"


def test_json_only_preserves_existing_tool_calls():
    """If the provider already parsed tool_calls natively, don't
    double-emit by re-parsing content."""
    parser = JSONOnlyParser()
    raw = {
        "role": "assistant",
        "content": '{"function_call":{"name":"a","arguments":{}}}',
        "tool_calls": [
            {"id": "c1", "function": {"name": "b", "arguments": "{}"}}
        ],
    }
    out = parser.parse(raw)
    assert [tc["function"]["name"] for tc in out.tool_calls] == ["b"]


# ── ParserRegistry ────────────────────────────────────────────────────


def _mk_parser(name: str):
    class _P:
        def parse(self_inner, raw):
            return NormalizedMessage(content=name)
    p = _P()
    p.name = name
    return p


def test_registry_most_specific_wins():
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    reg.register(_mk_parser("generic"), match="llama*")
    reg.register(_mk_parser("specific"), match="llama-3.1-*")
    assert reg.resolve("llama-3.1-70b").name == "specific"
    assert reg.resolve("llama-2-7b").name == "generic"
    assert reg.resolve("mistral-7b").name == "fallback"


def test_registry_re_register_replaces():
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    reg.register(_mk_parser("v1"), match="qwen*")
    reg.register(_mk_parser("v2"), match="qwen*")
    entries = reg.list_registered()
    assert len(entries) == 1
    assert entries[0][1] == "v2"


def test_registry_empty_model_uses_fallback():
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    reg.register(_mk_parser("qwen"), match="qwen*")
    assert reg.resolve("").name == "fallback"


def test_registry_case_insensitive_match():
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    reg.register(_mk_parser("qwen"), match="QWEN*")
    assert reg.resolve("qwen3-30b").name == "qwen"
    assert reg.resolve("QWEN3-30B").name == "qwen"


def test_registry_rejects_non_parser():
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    with pytest.raises(TypeError):
        reg.register(object(), match="x*")


def test_registry_requires_fallback():
    reg = ParserRegistry(fallback=None)
    with pytest.raises(RuntimeError, match="fallback"):
        reg.resolve("anything")


# ── decorator + discovery integration ─────────────────────────────────


def test_bootstrapped_registry_has_yaml_entries():
    """Default registry should have at least the YAML-configured entries."""
    reg = get_registry()
    patterns = [p for p, _ in reg.list_registered()]
    # All YAML patterns should appear.
    for expected in ["qwen*", "glm-4*"]:
        assert expected in patterns, f"{expected!r} missing from {patterns}"


def test_decorator_registration_staged():
    """Decorator-registered parsers land in the registry after bootstrap."""
    from app.v2.bridges.tool_parsers import base as base_mod

    @register(match="test-decorator-parser-*")
    class _TestP:
        name = "test_decorator"
        def parse(self, raw):
            return NormalizedMessage()

    # Flush pending into a fresh registry.
    reg = ParserRegistry(fallback=_mk_parser("fallback"))
    base_mod._drain_pending(reg)
    assert reg.resolve("test-decorator-parser-xyz").name == "test_decorator"
