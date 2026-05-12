"""Tests for explicit "DO NOT use this when..." clauses in tool
descriptions (P2 of Claude-Code parity, 2026-05-12).

The point: Claude Code's tool descriptions are aggressively prompt-
engineered with explicit prohibitions. Functional descriptions ("reads
a file") leave the LLM to its own exploration habits; prohibitions
shape behaviour at the schema level, before the loop even starts.

These tests are regression guards — if someone simplifies a tool
description back to a one-liner, this catches it.
"""
from __future__ import annotations

import pytest

from app import tools


def _get_tool_def(name: str) -> dict:
    """Find the tool function spec by name in TOOL_DEFINITIONS."""
    for t in tools.TOOL_DEFINITIONS:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return fn
    raise KeyError(f"tool not found: {name}")


def test_read_file_warns_against_redundant_reads():
    desc = _get_tool_def("read_file")["description"]
    assert "DO NOT call this tool when" in desc
    # Specific anti-patterns
    assert "REPEAT-READ" in desc, \
        "should reference the [REPEAT-READ #N] cache marker the LLM sees"
    assert ("verifying" in desc.lower()
            or "trust it" in desc.lower()), \
        "should discourage post-write verification reads"


def test_glob_files_warns_against_re_globbing():
    desc = _get_tool_def("glob_files")["description"]
    assert "DO NOT call this tool when" in desc
    assert "CACHED-GLOB" in desc, \
        "should reference the [CACHED-GLOB] cache marker"
    assert "exact file_path" in desc.lower() or \
           "user already gave you" in desc.lower(), \
        "should redirect to read_file when path is already known"


def test_bash_warns_against_using_for_dedicated_tools():
    desc = _get_tool_def("bash")["description"]
    assert "DO NOT use bash to do things dedicated tools can do" in desc
    # Check the four anti-patterns are listed
    for hint in ("read_file", "write_file", "edit_file"):
        assert hint in desc, f"bash desc should redirect to {hint}"
    # Anti-tools that LLMs commonly try
    for cmd in ("cat", "grep", "find", "sed", "echo"):
        assert cmd in desc, f"bash desc should warn against `{cmd}`"


def test_search_files_warns_against_when_path_known():
    desc = _get_tool_def("search_files")["description"]
    assert "DO NOT call this tool when" in desc
    assert "read_file directly" in desc, \
        "should redirect to read_file when path is known"
    assert "glob_files" in desc, \
        "should redirect to glob_files for name-based search"


def test_edit_file_warns_against_unread_files():
    desc = _get_tool_def("edit_file")["description"]
    assert "DO NOT call this tool when" in desc
    assert "read_file first" in desc.lower() or \
           "haven't read" in desc.lower(), \
        "should require read before edit"
    assert ("write_file" in desc), \
        "should redirect to write_file for full rewrites"


def test_all_prohibitive_tools_have_consistent_marker():
    """Sanity: every tool with a prohibition uses the same DO NOT
    marker so the LLM can pattern-match across them."""
    targets = ["read_file", "glob_files", "bash", "search_files", "edit_file"]
    for name in targets:
        desc = _get_tool_def(name)["description"]
        # Some variant of the marker is present
        assert "DO NOT" in desc, \
            f"{name} description missing DO NOT prohibition section"


def test_descriptions_remain_under_LLM_friendly_length():
    """Anti-bloat: descriptions are useful at <800 chars; beyond that
    they start eating system-prompt budget. Real Claude Code tool
    descriptions are usually 200-600 chars including prohibitions."""
    for name in ("read_file", "glob_files", "bash",
                 "search_files", "edit_file"):
        desc = _get_tool_def(name)["description"]
        assert len(desc) < 1000, (
            f"{name} description too long ({len(desc)} chars) — "
            f"trim or move detail to system prompt")
