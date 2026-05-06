"""DSMLParser — tool-call rescue for DeepSeek flash variants.

Lives under tests/rule_engine/ purely for proximity (related session
work); the parser itself sits in app.v2.bridges.tool_parsers.builtin.
"""
from __future__ import annotations

import json

import pytest

from app.v2.bridges.tool_parsers.builtin import DSMLParser, OpenAIPassthroughParser


# Sample emissions in the wild — full-width pipe (｜) is what DeepSeek
# actually emits. Some clients may normalize to ASCII "|"; both must
# parse.
_FULL_WIDTH = "｜"
_ASCII = "|"


def _build_dsml(tool: str, params: dict, bar: str = _FULL_WIDTH,
                wrapped: bool = True) -> str:
    parts = []
    if wrapped:
        parts.append(f"<{bar}{bar}DSML{bar}{bar}tool_calls>")
    parts.append(f'<{bar}{bar}DSML{bar}{bar}invoke name="{tool}">')
    for k, v in params.items():
        parts.append(
            f'<{bar}{bar}DSML{bar}{bar}parameter name="{k}" string="true">{v}'
            f'</{bar}{bar}DSML{bar}{bar}parameter>')
    parts.append(f"</{bar}{bar}DSML{bar}{bar}invoke>")
    if wrapped:
        parts.append(f"</{bar}{bar}DSML{bar}{bar}tool_calls>")
    return "\n".join(parts)


def test_full_width_pipes_parsed():
    """Real-world format: ｜｜DSML｜｜ with full-width vertical bars."""
    body = _build_dsml("glob_files",
                        {"pattern": "**/*", "path": "/tmp/foo"})
    msg = {"role": "assistant", "content": body}
    result = DSMLParser().parse(msg)
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc["function"]["name"] == "glob_files"
    args = json.loads(tc["function"]["arguments"])
    assert args == {"pattern": "**/*", "path": "/tmp/foo"}


def test_ascii_pipes_also_parsed():
    """Some clients normalize ｜ → |; parser must accept both."""
    body = _build_dsml("read_file", {"path": "/x.md"}, bar=_ASCII)
    msg = {"role": "assistant", "content": body}
    result = DSMLParser().parse(msg)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "read_file"


def test_no_dsml_in_content_passes_through():
    """Plain assistant text → no tool_calls extracted."""
    msg = {"role": "assistant",
           "content": "Sure, let me think about that."}
    result = DSMLParser().parse(msg)
    assert result.tool_calls == []
    assert "Sure" in result.content


def test_native_tool_calls_preserved():
    """When provider already returned tool_calls, DSML parser doesn't
    discard them (merges with any DSML it also finds)."""
    msg = {
        "role": "assistant",
        "content": "(no DSML here)",
        "tool_calls": [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "real_tool", "arguments": '{"x": 1}'},
        }],
    }
    result = DSMLParser().parse(msg)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "real_tool"


def test_native_plus_dsml_merge():
    """Provider gave one tool_call AND content has DSML — both kept."""
    body = _build_dsml("dsml_tool", {"key": "val"})
    msg = {
        "role": "assistant",
        "content": body,
        "tool_calls": [{
            "id": "call_native",
            "type": "function",
            "function": {"name": "native_tool", "arguments": "{}"},
        }],
    }
    result = DSMLParser().parse(msg)
    names = [tc["function"]["name"] for tc in result.tool_calls]
    assert "native_tool" in names
    assert "dsml_tool" in names


def test_strip_content_removes_markup():
    """After parse, the assistant content shouldn't show raw DSML."""
    body = "Here's what I'll do:\n" + _build_dsml(
        "list_files", {"path": "."}) + "\nDone."
    msg = {"role": "assistant", "content": body}
    result = DSMLParser().parse(msg)
    assert "DSML" not in result.content
    assert "Here's what I'll do" in result.content
    assert "Done" in result.content


def test_typed_argument_coercion():
    """DSML doesn't carry arg types — parser does light coercion so
    numerics arrive as numbers, not strings."""
    bar = _FULL_WIDTH
    body = (
        f"<{bar}{bar}DSML{bar}{bar}tool_calls>"
        f'<{bar}{bar}DSML{bar}{bar}invoke name="counter">'
        f'<{bar}{bar}DSML{bar}{bar}parameter name="n">42</{bar}{bar}DSML{bar}{bar}parameter>'
        f'<{bar}{bar}DSML{bar}{bar}parameter name="ratio">0.75</{bar}{bar}DSML{bar}{bar}parameter>'
        f'<{bar}{bar}DSML{bar}{bar}parameter name="enabled">true</{bar}{bar}DSML{bar}{bar}parameter>'
        f'<{bar}{bar}DSML{bar}{bar}parameter name="tags">["a","b"]</{bar}{bar}DSML{bar}{bar}parameter>'
        f"</{bar}{bar}DSML{bar}{bar}invoke>"
        f"</{bar}{bar}DSML{bar}{bar}tool_calls>"
    )
    msg = {"role": "assistant", "content": body}
    result = DSMLParser().parse(msg)
    args = json.loads(result.tool_calls[0]["function"]["arguments"])
    assert args["n"] == 42
    assert args["ratio"] == 0.75
    assert args["enabled"] is True
    assert args["tags"] == ["a", "b"]


def test_multiple_invokes_in_one_response():
    """Model emits two consecutive tool calls in one assistant turn."""
    body = (_build_dsml("first", {"a": "1"}, wrapped=False) +
            "\n" +
            _build_dsml("second", {"b": "2"}, wrapped=False))
    msg = {"role": "assistant", "content": body}
    result = DSMLParser().parse(msg)
    assert len(result.tool_calls) == 2
    names = [tc["function"]["name"] for tc in result.tool_calls]
    assert names == ["first", "second"]
