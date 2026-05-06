"""PEP integration smoke tests — engine reaches the call sites.

These tests don't spin up the full hub; they patch the engine
singleton with a controlled rule set and call the helper functions
directly. Verifies plumbing (context shape, denial path) without
dragging in the rest of the agent runtime.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from app.rule_engine.engine import Engine
from app.rule_engine.types import Rule, RuleScope


@pytest.fixture
def fresh_engine():
    d = tempfile.mkdtemp(prefix="tudou_pep_test_")
    # Reset singletons
    import app.rule_engine.engine as eng_mod
    import app.rule_engine.store as store_mod
    import app.rule_engine.audit as audit_mod
    eng_mod._ENGINE = None
    store_mod._STORE = None
    audit_mod._LOG = None
    eng_mod._ENGINE = Engine(Path(d))
    yield eng_mod._ENGINE
    eng_mod._ENGINE = None
    store_mod._STORE = None
    audit_mod._LOG = None
    shutil.rmtree(d, ignore_errors=True)


# ── before_file_write PEP ────────────────────────────────────────────

def test_before_file_write_pep_denies(fresh_engine):
    """A rule blocking writes outside agent's subdir must surface as
    a deny string from _rule_engine_check_file_write."""
    fresh_engine.store.add(Rule(
        name="path_must_be_in_agent_subdir",
        trigger="before_file_write",
        scope=RuleScope("project", ["proj_a"]),
        condition={"not": {"field": "args.path",
                           "matches": r"/coder-小新/"}},
        actions=[{"type": "deny",
                  "message": "must write under coder-小新/"}],
    ))
    from app.tools_split.fs import _rule_engine_check_file_write
    # Path NOT in agent subdir → should deny
    msg = _rule_engine_check_file_write(
        "/workspace/foo.md", "content",
        {"_caller_agent_id": "ag", "_caller_agent_name": "小新",
         "_caller_agent_role": "coder", "_project_id": "proj_a",
         "_workspace": "/workspace"},
    )
    assert msg.startswith("Error: write_file denied")
    assert "path_must_be_in_agent_subdir" in msg

    # Path IN agent subdir → should allow (empty string)
    msg2 = _rule_engine_check_file_write(
        "/workspace/coder-小新/foo.md", "content",
        {"_caller_agent_id": "ag", "_caller_agent_name": "小新",
         "_caller_agent_role": "coder", "_project_id": "proj_a",
         "_workspace": "/workspace"},
    )
    assert msg2 == ""


def test_before_file_write_pep_no_engine_returns_empty():
    """When engine isn't initialized (e.g. test environment without hub),
    PEP returns empty string — write proceeds normally."""
    import app.rule_engine.engine as eng_mod
    saved = eng_mod._ENGINE
    eng_mod._ENGINE = None
    try:
        from app.tools_split.fs import _rule_engine_check_file_write
        assert _rule_engine_check_file_write("/anywhere/x", "y", {}) == ""
    finally:
        eng_mod._ENGINE = saved


def test_before_file_write_pep_other_project_unaffected(fresh_engine):
    """A rule scoped to project A must NOT fire for project B."""
    fresh_engine.store.add(Rule(
        name="proj_a_only",
        trigger="before_file_write",
        scope=RuleScope("project", ["proj_a"]),
        condition={},   # always match
        actions=[{"type": "deny", "message": "blocked"}],
    ))
    from app.tools_split.fs import _rule_engine_check_file_write
    # Different project → no match
    msg = _rule_engine_check_file_write(
        "/workspace/anything.md", "x",
        {"_project_id": "proj_b", "_workspace": "/workspace"},
    )
    assert msg == ""


def test_before_file_write_global_rule_fires_in_project(fresh_engine):
    """Global rules apply across all scopes."""
    fresh_engine.store.add(Rule(
        name="no_huge_files",
        trigger="before_file_write",
        scope=RuleScope("global"),
        condition={"field": "args.size_bytes", "gt": 1000},
        actions=[{"type": "deny", "message": "file too big"}],
    ))
    from app.tools_split.fs import _rule_engine_check_file_write
    big = "x" * 2000
    msg = _rule_engine_check_file_write(
        "/workspace/big.md", big,
        {"_project_id": "proj_a", "_workspace": "/workspace"},
    )
    assert "no_huge_files" in msg
