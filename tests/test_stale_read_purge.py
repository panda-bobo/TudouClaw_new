"""Tests for the stale-read-result auto-cleanup on edit_file failure
(2026-05-13).

User: "如果不行就把错误记忆清理掉".

When edit_file fails with `old_string not found`, the agent's
problem is usually that its history holds a stale read_file result
of the same file (read earlier in the conversation, before the
file changed). The agent quotes field names / indentation from
that stale memory instead of the current file.

Fix: on `not found` failure, scan agent.messages for read_file
tool_results targeting the same path and REPLACE their content
with a `[STALE — re-read required]` marker. Pairing structure
(asst.tool_calls ↔ tool_result) is preserved so the sanitizer
doesn't break.
"""
from __future__ import annotations

import json

import pytest

from app.tools_split.fs import (
    _tool_edit_file,
    _mark_stale_read_results_for_path,
)
from app import sandbox as _sb


class _StubAgent:
    def __init__(self):
        self.id = "test-stale-purge"
        self.messages: list[dict] = []


@pytest.fixture
def sandboxed_tmp(tmp_path):
    pol = _sb.SandboxPolicy(mode="open", root=str(tmp_path), allow_list=[])
    prev = _sb.set_current_policy(pol)
    try:
        yield tmp_path
    finally:
        _sb.set_current_policy(prev)


def _build_history_with_read(path: str, content: str,
                              tc_id: str = "tc_read_1") -> list[dict]:
    """Build a typical history: user → assistant.tool_calls(read_file) →
    tool result with file content."""
    return [
        {"role": "user", "content": "open the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": path}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": content,
        },
    ]


# ── _mark_stale_read_results_for_path basic behaviour ─────────────

def test_marks_matching_read_result_as_stale(tmp_path):
    target = str(tmp_path / "config.tf")
    agent = _StubAgent()
    agent.messages = _build_history_with_read(
        target, "old field_name = 1\n")

    n = _mark_stale_read_results_for_path(agent, target)
    assert n == 1
    # Tool message content was replaced with the marker
    tool_msg = agent.messages[-1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["content"].startswith("[STALE")
    assert target in tool_msg["content"]
    assert "read_file" in tool_msg["content"]


def test_does_not_mark_other_files(tmp_path):
    target = str(tmp_path / "a.tf")
    other = str(tmp_path / "b.tf")
    agent = _StubAgent()
    agent.messages = (
        _build_history_with_read(target, "a content", tc_id="tc_a")
        + _build_history_with_read(other, "b content", tc_id="tc_b"))

    n = _mark_stale_read_results_for_path(agent, target)
    assert n == 1
    # b's content untouched
    b_tool = next(m for m in agent.messages
                  if m.get("role") == "tool"
                  and m.get("tool_call_id") == "tc_b")
    assert b_tool["content"] == "b content"


def test_no_op_when_no_prior_read(tmp_path):
    target = str(tmp_path / "x.tf")
    agent = _StubAgent()
    agent.messages = [{"role": "user", "content": "hi"}]
    n = _mark_stale_read_results_for_path(agent, target)
    assert n == 0


def test_no_op_when_agent_is_none():
    n = _mark_stale_read_results_for_path(None, "/anything")
    assert n == 0


def test_idempotent_does_not_double_mark(tmp_path):
    """Running twice doesn't keep wrapping the marker."""
    target = str(tmp_path / "x.tf")
    agent = _StubAgent()
    agent.messages = _build_history_with_read(target, "real content")
    n1 = _mark_stale_read_results_for_path(agent, target)
    n2 = _mark_stale_read_results_for_path(agent, target)
    assert n1 == 1
    assert n2 == 0   # already marked, no double-marking


def test_preserves_asst_tool_calls_structure(tmp_path):
    """The assistant.tool_calls msg + its tool result pairing must
    stay intact — only the tool result's content changes. Sanitizer
    Pass 3 needs the pair to match."""
    target = str(tmp_path / "x.tf")
    agent = _StubAgent()
    agent.messages = _build_history_with_read(target, "stale")
    _mark_stale_read_results_for_path(agent, target)
    # Asst still has its tool_calls
    asst = next(m for m in agent.messages
                if m.get("role") == "assistant" and m.get("tool_calls"))
    assert len(asst["tool_calls"]) == 1
    assert asst["tool_calls"][0]["id"] == "tc_read_1"
    # Tool msg still references the same id
    tool = next(m for m in agent.messages if m.get("role") == "tool")
    assert tool["tool_call_id"] == "tc_read_1"


def test_handles_relative_path_in_args(tmp_path, monkeypatch):
    """Args can be either absolute or relative; matching is by
    abspath(). A relative-path read_file targets the same canonical
    absolute path."""
    target_abs = str(tmp_path / "x.tf")
    agent = _StubAgent()
    # Read was logged with relative path
    agent.messages = _build_history_with_read("x.tf", "stale")
    monkeypatch.chdir(tmp_path)
    n = _mark_stale_read_results_for_path(agent, target_abs)
    assert n == 1


def test_handles_malformed_tool_call_args_gracefully(tmp_path):
    """If args isn't valid JSON, skip that tool_call but don't crash."""
    target = str(tmp_path / "x.tf")
    agent = _StubAgent()
    agent.messages = [
        {"role": "assistant", "content": "",
         "tool_calls": [{
             "id": "tc1", "type": "function",
             "function": {"name": "read_file", "arguments": "{not json"},
         }]},
        {"role": "tool", "tool_call_id": "tc1", "content": "x"},
    ]
    n = _mark_stale_read_results_for_path(agent, target)
    assert n == 0   # silently skipped


# ── Integration: edit_file failure triggers cleanup ─────────────────

def test_edit_file_failure_marks_stale_history(sandboxed_tmp):
    """End-to-end: real file + agent with prior read → edit fails →
    history is auto-cleaned."""
    f = sandboxed_tmp / "config.tf"
    f.write_text("real_field = 1\n", encoding="utf-8")

    # Build agent with stale read of this file
    agent = _StubAgent()
    agent.messages = _build_history_with_read(
        str(f), "OLD CONTENT — old_field_name = 99\n")

    # Patch _get_caller_agent to return our stub
    import app.tools_split.fs as _fs
    orig = _fs._get_caller_agent
    _fs._get_caller_agent = lambda cid: agent if cid else None
    try:
        result = _tool_edit_file(
            path=str(f),
            old_string="old_field_name = 99",  # was in stale memory
            new_string="x",
            _caller_agent_id=agent.id)
    finally:
        _fs._get_caller_agent = orig

    # Error returned
    assert "not found" in result
    # Auto-cleanup notice in the error
    assert "Auto-cleanup" in result
    assert "STALE" in result
    # Agent's history was rewritten
    tool_msg = next(m for m in agent.messages
                    if m.get("role") == "tool")
    assert tool_msg["content"].startswith("[STALE")
    assert "OLD CONTENT" not in tool_msg["content"]


def test_no_cleanup_notice_when_no_stale_history(sandboxed_tmp):
    """If agent has no prior read of this file, edit_file failure is
    NOT decorated with the auto-cleanup notice (don't lie about
    cleanup that didn't happen)."""
    f = sandboxed_tmp / "x.tf"
    f.write_text("content\n", encoding="utf-8")

    agent = _StubAgent()
    # Agent has read SOMETHING ELSE, not this file
    agent.messages = _build_history_with_read(
        "/some/other/file", "irrelevant")

    import app.tools_split.fs as _fs
    orig = _fs._get_caller_agent
    _fs._get_caller_agent = lambda cid: agent if cid else None
    try:
        result = _tool_edit_file(
            path=str(f), old_string="not in file", new_string="x",
            _caller_agent_id=agent.id)
    finally:
        _fs._get_caller_agent = orig

    assert "not found" in result
    # No cleanup notice (nothing to clean up)
    assert "Auto-cleanup" not in result
