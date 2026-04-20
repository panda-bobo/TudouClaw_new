"""Tests for the message-history compression that keeps token in-budget
under control during long multi-tool sessions.

Baseline concern
----------------
web_fetch tool results are 5-10k chars each. With the default keep-all
behavior, a 10-iteration research loop would resend the entire body on
every turn, blowing past 40k input tokens. We compress tool-result
bodies that are more than ``keep_last`` messages stale.
"""
from __future__ import annotations

import pytest

from app.agent import _compress_old_tool_results


def _tool_msg(body: str):
    return {"role": "tool", "tool_call_id": "t1", "content": body}


def test_no_compression_when_below_keep_last():
    """Up to keep_last tool messages — leave them alone."""
    msgs = [_tool_msg("A" * 5000) for _ in range(3)]
    out = _compress_old_tool_results(msgs, keep_last=4)
    assert out == msgs
    assert all(len(m["content"]) == 5000 for m in out)


def test_oldest_tool_results_get_truncated():
    """With 6 tool messages and keep_last=4, the 2 oldest are compressed."""
    msgs = [_tool_msg("X" * 3000) for _ in range(6)]
    out = _compress_old_tool_results(msgs, keep_last=4, max_body_chars=600)
    # First two truncated
    assert len(out[0]["content"]) < 3000
    assert "truncated from 3000" in out[0]["content"]
    assert len(out[1]["content"]) < 3000
    # Last four untouched
    for m in out[-4:]:
        assert len(m["content"]) == 3000


def test_short_tool_results_pass_through_even_when_old():
    """Tool results already below max_body_chars aren't re-truncated."""
    short = _tool_msg("OK")
    big = _tool_msg("X" * 5000)
    msgs = [short] + [big] * 10  # short is oldest
    out = _compress_old_tool_results(msgs, keep_last=4, max_body_chars=600)
    # Short one stays exactly as-is
    assert out[0]["content"] == "OK"


def test_non_tool_roles_untouched():
    """Only `role=tool` messages are considered — user/assistant/system
    content stays full-fidelity."""
    msgs = [
        {"role": "system", "content": "S" * 3000},
        {"role": "user", "content": "U" * 3000},
        _tool_msg("T1" * 2000),
        {"role": "assistant", "content": "A" * 3000},
        _tool_msg("T2" * 2000),
        _tool_msg("T3" * 2000),
        _tool_msg("T4" * 2000),
        _tool_msg("T5" * 2000),
        _tool_msg("T6" * 2000),
    ]
    out = _compress_old_tool_results(msgs, keep_last=4, max_body_chars=100)
    # system / user / assistant lengths preserved
    assert len(out[0]["content"]) == 3000
    assert len(out[1]["content"]) == 3000
    assert len(out[3]["content"]) == 3000
    # 6 tool messages; keep_last=4 → oldest 2 compressed
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 6
    assert len(tool_msgs[0]["content"]) <= 250   # 100 head + truncation marker
    assert len(tool_msgs[1]["content"]) <= 250
    # Four most-recent unchanged length
    for m in tool_msgs[-4:]:
        assert len(m["content"]) == 4000  # "T2" * 2000


def test_compression_is_pure_never_mutates_input():
    """Returned list is new; original left intact for callers that still
    hold a reference to the pre-compression sequence (debugging, logs)."""
    msgs = [_tool_msg("X" * 3000) for _ in range(6)]
    original_0 = msgs[0]["content"]
    _ = _compress_old_tool_results(msgs, keep_last=4, max_body_chars=600)
    assert msgs[0]["content"] == original_0  # input untouched


def test_non_string_content_passes_through():
    """If someone stored structured content on a tool message we don't
    touch it — truncation only applies to plain strings."""
    msgs = (
        [_tool_msg("X" * 3000) for _ in range(3)]
        + [{"role": "tool", "content": [{"type": "text", "text": "hi"}]}]
        + [_tool_msg("X" * 3000) for _ in range(4)]
    )
    out = _compress_old_tool_results(msgs, keep_last=4, max_body_chars=100)
    # The list-valued tool message at index 3 must NOT be string-sliced
    assert out[3]["content"] == [{"type": "text", "text": "hi"}]
