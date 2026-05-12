"""Tests for glob_files turn-cache (P1 of "Claude-Code parity", 2026-05-12).

Mirrors read_file's existing per-turn cache. Same (path, pattern) called
twice in one turn → second call returns previous result + a [CACHED-GLOB
#N] marker so the LLM stops re-globbing.

Real-world bug: 刘老师 called glob_files 13 times in one turn while
"exploring", contributing to the "假 working" pattern. The cache turns
those 13 calls into 1 actual glob + 12 instant cached returns with a
nudge to stop.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools_split.fs import _tool_glob_files, _GLOB_FILES_CACHE_ATTR
from app import sandbox as _sandbox


# ── Fixtures ──────────────────────────────────────────────────────────

class _StubAgent:
    """Minimal stand-in: just needs to hold the cache attribute."""
    def __init__(self):
        self.id = "test-agent-glob"


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a small filesystem so glob has something to find."""
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("c")
    (tmp_path / "sub" / "d.txt").write_text("d")
    return tmp_path


@pytest.fixture
def sandboxed(temp_workspace):
    """Install an unrestricted sandbox rooted at temp_workspace so
    pol.safe_path passes through. Yield then restore."""
    pol = _sandbox.SandboxPolicy(
        mode="open", root=str(temp_workspace),
        allow_list=[],
    )
    prev = _sandbox.set_current_policy(pol)
    try:
        yield temp_workspace
    finally:
        _sandbox.set_current_policy(prev)


@pytest.fixture
def agent_with_cache_wiring(monkeypatch):
    """Wire the _get_caller_agent helper to return our stub so the cache
    has somewhere to live."""
    agent = _StubAgent()
    monkeypatch.setattr(
        "app.tools_split.fs._get_caller_agent",
        lambda caller_id: agent if caller_id else None,
    )
    return agent


# ── Tests ─────────────────────────────────────────────────────────────

def test_first_call_returns_results(sandboxed, agent_with_cache_wiring):
    out = _tool_glob_files(
        pattern="*.py", path=str(sandboxed),
        _caller_agent_id="test-agent-glob")
    assert "a.py" in out
    assert "b.py" in out
    assert "[CACHED-GLOB" not in out


def test_second_identical_call_hits_cache(sandboxed, agent_with_cache_wiring):
    args = dict(pattern="*.py", path=str(sandboxed),
                _caller_agent_id="test-agent-glob")
    first = _tool_glob_files(**args)
    second = _tool_glob_files(**args)
    assert "[CACHED-GLOB #2]" in second
    # Body is preserved after the marker
    assert "a.py" in second
    assert "b.py" in second


def test_third_call_increments_hit_count(sandboxed, agent_with_cache_wiring):
    args = dict(pattern="*.py", path=str(sandboxed),
                _caller_agent_id="test-agent-glob")
    _tool_glob_files(**args)
    _tool_glob_files(**args)
    third = _tool_glob_files(**args)
    assert "[CACHED-GLOB #3]" in third


def test_different_pattern_does_not_hit_cache(sandboxed, agent_with_cache_wiring):
    _tool_glob_files(pattern="*.py", path=str(sandboxed),
                     _caller_agent_id="test-agent-glob")
    other = _tool_glob_files(pattern="**/*.txt", path=str(sandboxed),
                             _caller_agent_id="test-agent-glob")
    assert "[CACHED-GLOB" not in other
    assert "d.txt" in other


def test_different_path_does_not_hit_cache(sandboxed, agent_with_cache_wiring):
    _tool_glob_files(pattern="*.py", path=str(sandboxed),
                     _caller_agent_id="test-agent-glob")
    sub = str(sandboxed / "sub")
    other = _tool_glob_files(pattern="*.py", path=sub,
                             _caller_agent_id="test-agent-glob")
    assert "[CACHED-GLOB" not in other
    assert "c.py" in other


def test_no_caller_agent_means_no_cache(sandboxed, monkeypatch):
    """If we can't find the caller agent, cache is silently skipped —
    behaviour identical to pre-cache version."""
    monkeypatch.setattr(
        "app.tools_split.fs._get_caller_agent", lambda _id: None)
    args = dict(pattern="*.py", path=str(sandboxed),
                _caller_agent_id="ghost")
    first = _tool_glob_files(**args)
    second = _tool_glob_files(**args)
    # Both calls actually run the glob (no cache marker)
    assert "[CACHED-GLOB" not in first
    assert "[CACHED-GLOB" not in second
    assert first == second   # same files, same sort order


def test_cached_marker_includes_pattern_in_message(sandboxed,
                                                    agent_with_cache_wiring):
    args = dict(pattern="**/*.py", path=str(sandboxed),
                _caller_agent_id="test-agent-glob")
    _tool_glob_files(**args)
    second = _tool_glob_files(**args)
    # Message should mention the pattern so the LLM knows what was cached
    assert "**/*.py" in second


def test_cache_clear_restores_fresh_glob(sandboxed, agent_with_cache_wiring):
    """Simulating a turn boundary: clearing the agent's cache attr lets
    the next call hit the disk again (no [CACHED-GLOB] marker)."""
    args = dict(pattern="*.py", path=str(sandboxed),
                _caller_agent_id="test-agent-glob")
    _tool_glob_files(**args)
    # Turn boundary — chat() resets _glob_files_turn_cache to {}
    setattr(agent_with_cache_wiring, _GLOB_FILES_CACHE_ATTR, {})
    fresh = _tool_glob_files(**args)
    assert "[CACHED-GLOB" not in fresh


def test_no_files_found_is_cached_too(sandboxed, agent_with_cache_wiring):
    """Empty result still gets cached so the LLM doesn't re-glob a
    pattern that yielded nothing."""
    args = dict(pattern="*.does-not-exist", path=str(sandboxed),
                _caller_agent_id="test-agent-glob")
    first = _tool_glob_files(**args)
    second = _tool_glob_files(**args)
    assert "No files found" in first
    assert "[CACHED-GLOB #2]" in second
    assert "No files found" in second


def test_setattr_failure_degrades_silently(sandboxed, monkeypatch):
    """If setattr on the stub agent fails (frozen dataclass etc.), the
    tool still returns results — just without caching."""
    class FrozenAgent:
        __slots__ = ("id",)
        def __init__(self): self.id = "frozen"
    a = FrozenAgent()
    monkeypatch.setattr(
        "app.tools_split.fs._get_caller_agent",
        lambda _id: a if _id else None)
    args = dict(pattern="*.py", path=str(sandboxed),
                _caller_agent_id="frozen")
    first = _tool_glob_files(**args)
    second = _tool_glob_files(**args)
    # __slots__ blocks setattr → cache unavailable → both fresh
    assert "[CACHED-GLOB" not in first
    assert "[CACHED-GLOB" not in second
