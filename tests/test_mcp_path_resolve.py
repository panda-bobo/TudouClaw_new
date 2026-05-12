"""Tests for _resolve_relative_path_args (2026-05-12).

Real bug: agent called terraform MCP with
working_dir="landing-zone-sample/modules/monitoring" (relative). MCP's
_validate_working_dir requires absolute path → returns
"working_dir must be absolute" in 168ms. mcp_router logged "ok" (no
crash) but content was an error. After 8 such failures the
same_tool_halt guardrail kicked in and the agent gave up on MCP.

Fix: agent-side mcp_call resolves relative path-like args using the
calling agent's working_dir as the base BEFORE dispatching to the
MCP. Every MCP that takes paths gets absolute paths for free.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.tools_split.mcp import (
    _resolve_relative_path_args,
    _MCP_PATH_LIKE_KEYS,
)


class _StubAgent:
    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        self.id = "test-agent-resolve"


class _StubHub:
    def __init__(self, agent):
        self.agents = {agent.id: agent} if agent else {}


def _install_hub_with(agent, monkeypatch):
    """Make app.llm._active_hub return our stub hub."""
    import sys
    fake_llm_mod = MagicMock()
    fake_llm_mod._active_hub = _StubHub(agent)
    monkeypatch.setitem(sys.modules, "app.llm", fake_llm_mod)


# ── Basic resolution ─────────────────────────────────────────────────

def test_relative_working_dir_resolved_to_absolute(monkeypatch):
    agent = _StubAgent("/Users/me/workspace")
    _install_hub_with(agent, monkeypatch)

    args = {"working_dir": "landing-zone-sample/modules/monitoring"}
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert os.path.isabs(resolved["working_dir"])
    assert resolved["working_dir"] == (
        "/Users/me/workspace/landing-zone-sample/modules/monitoring")


def test_absolute_path_unchanged(monkeypatch):
    agent = _StubAgent("/Users/me/workspace")
    _install_hub_with(agent, monkeypatch)

    args = {"working_dir": "/etc/already/absolute"}
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert resolved["working_dir"] == "/etc/already/absolute"


def test_all_path_like_keys_resolved(monkeypatch):
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)

    args = {
        "file_path": "src/foo.py",
        "directory": "build",
        "src": "a.txt",
        "non_path_arg": "leave/me/alone",
    }
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert resolved["file_path"] == "/Users/me/ws/src/foo.py"
    assert resolved["directory"] == "/Users/me/ws/build"
    assert resolved["src"] == "/Users/me/ws/a.txt"
    # Non-path key untouched (it's "leave/me/alone", not a known
    # path key, so stays relative)
    assert resolved["non_path_arg"] == "leave/me/alone"


# ── No-op cases ──────────────────────────────────────────────────────

def test_empty_args(monkeypatch):
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)
    assert _resolve_relative_path_args({}, "id") == {}


def test_no_relative_paths_skips_hub_lookup(monkeypatch):
    """Fast-path: if no path-like arg is present, don't even try to
    fetch the agent. Verified by setting hub to None — the call must
    still succeed."""
    import sys
    fake_llm_mod = MagicMock()
    fake_llm_mod._active_hub = None
    monkeypatch.setitem(sys.modules, "app.llm", fake_llm_mod)

    args = {"foo": "bar", "count": 5}
    resolved = _resolve_relative_path_args(args, "any")
    assert resolved == args


def test_non_dict_input(monkeypatch):
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)
    assert _resolve_relative_path_args([], "id") == []
    assert _resolve_relative_path_args("not a dict", "id") == "not a dict"
    assert _resolve_relative_path_args(None, "id") is None


def test_empty_string_value_not_resolved(monkeypatch):
    """Empty string isn't a real path — don't try to resolve it
    (would become the workspace root, surprising)."""
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)

    args = {"path": ""}
    resolved = _resolve_relative_path_args(args, "id")
    assert resolved["path"] == ""


# ── Failure modes ─────────────────────────────────────────────────────

def test_agent_not_found_returns_args_unchanged(monkeypatch):
    """If caller agent isn't in the hub, MCP gets the relative path
    as-is and produces its own error. Better than fabricating an
    absolute path that doesn't exist."""
    _install_hub_with(None, monkeypatch)

    args = {"working_dir": "some/relative"}
    resolved = _resolve_relative_path_args(args, "ghost-id")
    assert resolved["working_dir"] == "some/relative"


def test_agent_without_working_dir(monkeypatch):
    """Agent exists but has empty working_dir → don't resolve."""
    agent = _StubAgent("")
    _install_hub_with(agent, monkeypatch)

    args = {"working_dir": "relative/path"}
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert resolved["working_dir"] == "relative/path"


def test_agent_working_dir_not_absolute(monkeypatch):
    """Bad config: agent.working_dir is itself relative. Don't compound
    the badness — leave args alone, let MCP error meaningfully."""
    agent = _StubAgent("relative/workspace")
    _install_hub_with(agent, monkeypatch)

    args = {"working_dir": "modules/x"}
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert resolved["working_dir"] == "modules/x"


def test_hub_import_fails_silently(monkeypatch):
    """If app.llm module is missing entirely, return args unchanged."""
    import sys
    monkeypatch.setitem(sys.modules, "app.llm", None)
    args = {"working_dir": "x/y"}
    resolved = _resolve_relative_path_args(args, "id")
    assert resolved["working_dir"] == "x/y"


# ── Non-mutation ─────────────────────────────────────────────────────

def test_original_args_not_mutated(monkeypatch):
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)

    original = {"working_dir": "modules/x"}
    _resolve_relative_path_args(original, "test-agent-resolve")
    # Original unchanged
    assert original["working_dir"] == "modules/x"


# ── Path normalization ───────────────────────────────────────────────

def test_normpath_collapses_dotdot(monkeypatch):
    """os.path.normpath collapses /a/b/../c → /a/c, so the resolved
    path is clean."""
    agent = _StubAgent("/Users/me/ws")
    _install_hub_with(agent, monkeypatch)

    args = {"working_dir": "modules/foo/../bar"}
    resolved = _resolve_relative_path_args(args, "test-agent-resolve")
    assert resolved["working_dir"] == "/Users/me/ws/modules/bar"


def test_known_path_keys_complete():
    """Sanity: the path-like key set covers common arg names used in
    MCP schemas."""
    expected = {"path", "file_path", "working_dir", "directory",
                "cwd", "src", "dst", "target"}
    assert expected.issubset(_MCP_PATH_LIKE_KEYS), (
        f"missing keys: {expected - _MCP_PATH_LIKE_KEYS}")
