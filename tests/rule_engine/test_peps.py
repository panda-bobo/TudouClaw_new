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


# ── before_dispatch_task PEP ─────────────────────────────────────────

def test_before_dispatch_task_capacity_rule(fresh_engine, monkeypatch):
    """Capacity rule denying when target agent has too much in-flight."""
    fresh_engine.store.add(Rule(
        name="overload_protection",
        trigger="before_dispatch_task",
        scope=RuleScope("global"),
        condition={"field": "to_agent.inflight", "gt": 2},
        actions=[{"type": "deny", "message": "agent already at capacity"}],
    ))
    from app.tools_split import coordination as _coord
    # Stub hub + task store
    class _A: id="a"; name="alice"; role="coder"
    class _Hub:
        agents = {"target": _A()}
    monkeypatch.setattr(_coord, "_get_hub", lambda: _Hub())
    class _TaskStore:
        def list_for_agent(self, aid):
            return [type("X", (), {"status": "in_progress"})() for _ in range(3)]
    import app.core.task_assignment as ta
    monkeypatch.setattr(ta, "get_store", lambda: _TaskStore())
    msg = _coord._rule_engine_check_dispatch_task(
        from_agent_id="pm1", to_agent_id="target",
        brief="do thing", priority=0, deadline="", project_id="p1",
    )
    assert "overload_protection" in msg
    assert "capacity" in msg


def test_before_dispatch_task_no_engine_returns_empty(monkeypatch):
    """When engine isn't initialized, dispatch passes through."""
    import app.rule_engine.engine as eng_mod
    saved = eng_mod._ENGINE
    eng_mod._ENGINE = None
    try:
        from app.tools_split import coordination as _coord
        # Stub hub even though engine is off (resolution still runs)
        class _Hub: agents = {}
        monkeypatch.setattr(_coord, "_get_hub", lambda: _Hub())
        msg = _coord._rule_engine_check_dispatch_task(
            from_agent_id="x", to_agent_id="y",
            brief="b", priority=0, deadline="", project_id="",
        )
        assert msg == ""
    finally:
        eng_mod._ENGINE = saved


# ── before_milestone_done PEP ────────────────────────────────────────

def test_before_milestone_done_requires_evidence(fresh_engine):
    """Rule blocking milestone confirmation when evidence is empty."""
    fresh_engine.store.add(Rule(
        name="evidence_required",
        trigger="before_milestone_done",
        scope=RuleScope("global"),
        condition={"field": "milestone.evidence_length", "lt": 10},
        actions=[{"type": "deny", "message": "evidence too short"}],
    ))
    # Build minimal mock objects rather than spinning up a Project
    class _MS:
        id = "m1"; name = "M1"; status = "in_progress"
        evidence = ""; responsible_agent_id = ""
    class _Proj:
        id = "p1"; name = "P1"
        deliverables = []
    from app.project import _rule_engine_check_milestone_done
    deny = _rule_engine_check_milestone_done(_Proj(), _MS(), "admin")
    assert "evidence_required" in deny


def test_before_milestone_done_passes_when_evidence_long_enough(fresh_engine):
    """Same rule, but milestone has enough evidence — no deny."""
    fresh_engine.store.add(Rule(
        name="evidence_required",
        trigger="before_milestone_done",
        scope=RuleScope("global"),
        condition={"field": "milestone.evidence_length", "lt": 10},
        actions=[{"type": "deny", "message": "evidence too short"}],
    ))
    class _MS:
        id = "m1"; name = "M1"; status = "in_progress"
        evidence = "x" * 100; responsible_agent_id = ""
    class _Proj:
        id = "p1"; name = "P1"
        deliverables = []
    from app.project import _rule_engine_check_milestone_done
    deny = _rule_engine_check_milestone_done(_Proj(), _MS(), "admin")
    assert deny == ""
