"""Tests for Step 3 of history-compaction overhaul: structured fact
extraction + renderer.

The deterministic extractor scans the compressed-out range and
reports facts the narrative LLM can't be trusted to enumerate
(files modified, tool errors, bash commands, tool-call frequency).

Tests here verify:
  1. Files written/edited get picked up across Write/Edit/MultiEdit
  2. Tool errors detected via marker heuristics (case-insensitive)
  3. Bash commands extracted from args + truncated if huge
  4. Tools-called count is correct
  5. Renderer skips empty sections
  6. Renderer dedup keeps last op per file path
"""
from __future__ import annotations

import json

from app.agent import (
    _extract_structured_facts,
    _render_structured_facts,
)


def _assistant_tc(name: str, args: dict, tc_id: str = "tc1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tc_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            },
        }],
    }


def _tool_result(tc_id: str, content: str, is_error: bool = False):
    m = {"role": "tool", "tool_call_id": tc_id, "content": content}
    if is_error:
        m["is_error"] = True
    return m


def test_files_modified_picked_up():
    msgs = [
        _assistant_tc("Write", {"file_path": "/a/b/foo.py", "content": "x"},
                      tc_id="t1"),
        _assistant_tc("Edit", {"file_path": "/a/b/bar.py",
                               "old_string": "x", "new_string": "y"},
                      tc_id="t2"),
        _assistant_tc("Read", {"file_path": "/c/d/read.py"}, tc_id="t3"),
    ]
    facts = _extract_structured_facts(msgs)
    paths = [f["path"] for f in facts["files_touched"]]
    # Read should NOT count as modified
    assert "/a/b/foo.py" in paths
    assert "/a/b/bar.py" in paths
    assert "/c/d/read.py" not in paths


def test_tool_errors_detected_by_marker():
    msgs = [
        _assistant_tc("Bash", {"command": "ls /nonexistent"}, tc_id="t1"),
        _tool_result("t1", "Error: No such file or directory"),
        _assistant_tc("Bash", {"command": "ls /tmp"}, tc_id="t2"),
        _tool_result("t2", "/tmp/foo\n/tmp/bar"),
    ]
    facts = _extract_structured_facts(msgs)
    assert len(facts["tools_with_errors"]) == 1
    assert facts["tools_with_errors"][0]["tool"] == "Bash"
    assert "no such file" in facts["tools_with_errors"][0]["error"].lower()


def test_tool_errors_detected_via_is_error_flag():
    msgs = [
        _assistant_tc("Read", {"file_path": "/missing"}, tc_id="t1"),
        _tool_result("t1", "file does not exist", is_error=True),
    ]
    facts = _extract_structured_facts(msgs)
    assert len(facts["tools_with_errors"]) == 1
    assert facts["tools_with_errors"][0]["tool"] == "Read"


def test_tool_errors_does_not_misfire_on_word_error_in_middle():
    # "error" appearing deep in normal output shouldn't trigger.
    msgs = [
        _assistant_tc("Bash", {"command": "echo hi"}, tc_id="t1"),
        _tool_result(
            "t1",
            "hi\n" * 50 + "some line mentioning error in the middle\n"
            + "rest of output"),
    ]
    facts = _extract_structured_facts(msgs)
    assert facts["tools_with_errors"] == []


def test_bash_commands_collected_and_truncated():
    big_cmd = "echo " + "x" * 500
    msgs = [
        _assistant_tc("Bash", {"command": "ls"}, tc_id="t1"),
        _assistant_tc("Bash", {"command": big_cmd}, tc_id="t2"),
    ]
    facts = _extract_structured_facts(msgs)
    assert "ls" in facts["bash_commands"]
    # Long one got head-truncated with the +c marker
    assert any("…(+" in c for c in facts["bash_commands"])


def test_tools_called_count():
    msgs = [
        _assistant_tc("Read", {"file_path": "/a"}, tc_id="t1"),
        _assistant_tc("Read", {"file_path": "/b"}, tc_id="t2"),
        _assistant_tc("Read", {"file_path": "/c"}, tc_id="t3"),
        _assistant_tc("Bash", {"command": "ls"}, tc_id="t4"),
    ]
    facts = _extract_structured_facts(msgs)
    assert facts["tools_called_count"]["Read"] == 3
    assert facts["tools_called_count"]["Bash"] == 1


def test_renderer_skips_empty_sections():
    facts = {
        "files_touched": [],
        "tools_called_count": {},
        "tools_with_errors": [],
        "bash_commands": [],
        "todos_changed": 0,
    }
    assert _render_structured_facts(facts) == ""


def test_renderer_dedups_files_keeps_last_op():
    facts = {
        "files_touched": [
            {"tool": "Write", "path": "/a.py"},
            {"tool": "Edit", "path": "/a.py"},      # later edit wins
            {"tool": "Write", "path": "/b.py"},
        ],
        "tools_called_count": {},
        "tools_with_errors": [],
        "bash_commands": [],
        "todos_changed": 0,
    }
    out = _render_structured_facts(facts)
    assert out.count("/a.py") == 1
    assert "/a.py` (Edit)" in out
    assert "/b.py` (Write)" in out


def test_renderer_includes_section_headers():
    facts = {
        "files_touched": [{"tool": "Write", "path": "/x.py"}],
        "tools_called_count": {"Read": 5},
        "tools_with_errors": [{"tool": "Bash", "error": "oops"}],
        "bash_commands": ["ls", "pwd"],
        "todos_changed": 2,
    }
    out = _render_structured_facts(facts)
    assert "### Files modified" in out
    assert "### Tool errors" in out
    assert "### Tools called" in out
    assert "### Recent bash" in out
    assert "TodoWrite invoked 2×" in out


def test_renderer_top_n_tools_truncated_at_8():
    facts = {
        "files_touched": [],
        "tools_called_count": {f"tool_{i}": (20 - i) for i in range(15)},
        "tools_with_errors": [],
        "bash_commands": [],
        "todos_changed": 0,
    }
    out = _render_structured_facts(facts)
    # Top 8 by descending count: tool_0..tool_7
    assert "tool_0×20" in out
    assert "tool_7×13" in out
    assert "tool_8×" not in out


def test_bash_recent_only_keeps_last_5_unique():
    facts = {
        "files_touched": [],
        "tools_called_count": {},
        "tools_with_errors": [],
        # 8 commands, 2 duplicates → 6 unique → show last 5 unique
        "bash_commands": ["a", "b", "a", "c", "d", "e", "f", "g"],
        "todos_changed": 0,
    }
    out = _render_structured_facts(facts)
    # Expected last-5-unique in chrono order: c, d, e, f, g
    for cmd in ("c", "d", "e", "f", "g"):
        assert f"`{cmd}`" in out
    # 'a' and 'b' (older / duplicate) dropped
    assert "`a`" not in out
    assert "`b`" not in out


def test_extractor_handles_malformed_arguments():
    # arguments not valid JSON — shouldn't raise.
    msgs = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "t1",
            "type": "function",
            "function": {"name": "Bash", "arguments": "{not json"},
        }],
    }]
    facts = _extract_structured_facts(msgs)
    # Tool got counted even though args couldn't parse
    assert facts["tools_called_count"]["Bash"] == 1
    # No bash command extracted (args were unparseable)
    assert facts["bash_commands"] == []


def test_extractor_handles_list_content_in_tool_result():
    # Anthropic-style tool_result content as list of {type, text} parts.
    msgs = [
        _assistant_tc("Bash", {"command": "false"}, tc_id="t1"),
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": [
                {"type": "text", "text": "Error: command failed"},
            ],
        },
    ]
    facts = _extract_structured_facts(msgs)
    assert len(facts["tools_with_errors"]) == 1


# ── Step 4: content-hash cache key ────────────────────────────────────
from app.agent import _hash_old_slice


def test_hash_stable_across_calls():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert _hash_old_slice(msgs) == _hash_old_slice(msgs)


def test_hash_changes_on_content_edit():
    msgs1 = [{"role": "user", "content": "hello"}]
    msgs2 = [{"role": "user", "content": "hello!"}]
    assert _hash_old_slice(msgs1) != _hash_old_slice(msgs2)


def test_hash_changes_on_role_swap():
    msgs1 = [{"role": "user", "content": "x"}]
    msgs2 = [{"role": "assistant", "content": "x"}]
    assert _hash_old_slice(msgs1) != _hash_old_slice(msgs2)


def test_hash_detects_message_addition():
    base = [{"role": "user", "content": "x"}]
    longer = base + [{"role": "assistant", "content": "y"}]
    assert _hash_old_slice(base) != _hash_old_slice(longer)


def test_hash_detects_inner_tool_result_mutation():
    # Real-world cache-poisoning scenario: a downstream sanitizer
    # truncates a tool body in place. covers_n + covers_chars stay
    # nearly the same, but content differs → hash MUST differ.
    msgs1 = [
        _assistant_tc("Read", {"file_path": "/x"}, tc_id="t1"),
        _tool_result("t1", "X" * 5000),
    ]
    msgs2 = [
        _assistant_tc("Read", {"file_path": "/x"}, tc_id="t1"),
        _tool_result("t1", "X" * 4900 + "[truncated]"),
    ]
    assert _hash_old_slice(msgs1) != _hash_old_slice(msgs2)


def test_hash_handles_list_content():
    # Anthropic-style multi-part content shouldn't crash.
    msgs = [{
        "role": "user",
        "content": [{"type": "text", "text": "hi"}],
    }]
    h = _hash_old_slice(msgs)
    assert isinstance(h, str) and len(h) == 16


def test_hash_handles_empty_slice():
    assert _hash_old_slice([]) == _hash_old_slice([])
    # And it's a valid 16-hex string
    h = _hash_old_slice([])
    assert len(h) == 16
    int(h, 16)  # parses as hex


def test_hash_handles_unserializable_content_gracefully():
    # A custom object that JSON can't serialize — should fallback
    # to repr() and not raise.
    class Weird:
        def __repr__(self): return "Weird()"
    msgs = [{"role": "user", "content": Weird()}]
    h = _hash_old_slice(msgs)
    assert len(h) == 16
