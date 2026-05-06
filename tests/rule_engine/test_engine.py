"""End-to-end engine tests: store + scope routing + decision short-circuit."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.rule_engine.engine import Engine
from app.rule_engine.types import Rule, RuleScope


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tudou_re_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def engine(tmp_dir):
    # Reset module-level singletons so each test starts clean
    import app.rule_engine.engine as eng_mod
    import app.rule_engine.store as store_mod
    import app.rule_engine.audit as audit_mod
    eng_mod._ENGINE = None
    store_mod._STORE = None
    audit_mod._LOG = None
    return Engine(tmp_dir)


# ── Store basics ─────────────────────────────────────────────────────

def test_add_get_delete(engine):
    r = Rule(name="test", trigger="before_tool_call",
             scope=RuleScope("global"),
             actions=[{"type": "warn", "message": "hi"}])
    engine.store.add(r)
    assert engine.store.get(r.id) is not None
    assert engine.store.delete(r.id) is True
    assert engine.store.get(r.id) is None


def test_persistence_roundtrip(tmp_dir):
    """Rules survive process restart (re-instantiate Engine on same dir)."""
    # Singletons may be left over from earlier tests in the suite —
    # reset before constructing e1 so init_store actually creates a
    # fresh store pointing at OUR tmp_dir.
    import app.rule_engine.store as store_mod
    import app.rule_engine.audit as audit_mod
    store_mod._STORE = None
    audit_mod._LOG = None
    e1 = Engine(tmp_dir)
    e1.store.add(Rule(name="persisted", trigger="before_tool_call",
                      scope=RuleScope("global"),
                      actions=[{"type": "deny", "message": "no"}]))
    # Reset singletons + new instance reads the same file
    store_mod._STORE = None
    audit_mod._LOG = None
    e2 = Engine(tmp_dir)
    rules = e2.store.all()
    assert len(rules) == 1
    assert rules[0].name == "persisted"


def test_revisions_appended(engine):
    r = Rule(name="rev_test", trigger="before_tool_call",
             scope=RuleScope("global"),
             actions=[{"type": "log"}])
    engine.store.add(r, by="alice")
    engine.store.update(r.id, {"name": "renamed"}, by="bob",
                        revision_note="renaming")
    engine.store.delete(r.id, by="carol")

    rev_path = engine.data_dir / "rules" / "revisions.jsonl"
    lines = rev_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    actions = [json.loads(l)["action"] for l in lines]
    assert actions == ["add", "update", "delete"]


# ── Scope filtering ──────────────────────────────────────────────────

def test_scope_global_always_matches(engine):
    r = Rule(name="global", trigger="before_tool_call",
             scope=RuleScope("global"),
             actions=[{"type": "log"}])
    engine.store.add(r)
    decisions = engine.evaluate("before_tool_call", {
        "scope": {"kind": "project", "project_id": "p1"},
    })
    assert len(decisions) == 1
    assert decisions[0].rule_id == r.id


def test_scope_project_specific(engine):
    """A rule scoped to project=p1 doesn't fire for p2."""
    r = Rule(name="only_p1", trigger="before_tool_call",
             scope=RuleScope("project", ["p1"]),
             actions=[{"type": "log"}])
    engine.store.add(r)
    # Scope p1 → matches
    d1 = engine.evaluate("before_tool_call", {
        "scope": {"kind": "project", "project_id": "p1"},
    })
    assert len(d1) == 1
    # Scope p2 → no match
    d2 = engine.evaluate("before_tool_call", {
        "scope": {"kind": "project", "project_id": "p2"},
    })
    assert len(d2) == 0


def test_scope_wildcard_target(engine):
    r = Rule(name="any_meeting", trigger="before_tool_call",
             scope=RuleScope("meeting", ["*"]),
             actions=[{"type": "log"}])
    engine.store.add(r)
    d = engine.evaluate("before_tool_call", {
        "scope": {"kind": "meeting", "meeting_id": "m_xyz"},
    })
    assert len(d) == 1


def test_disabled_rule_skipped(engine):
    r = Rule(name="disabled", trigger="before_tool_call",
             scope=RuleScope("global"), enabled=False,
             actions=[{"type": "log"}])
    engine.store.add(r)
    d = engine.evaluate("before_tool_call", {"scope": {"kind": "global"}})
    assert len(d) == 0


# ── Condition + action wiring ────────────────────────────────────────

def test_condition_filters_match(engine):
    r = Rule(name="only_glob", trigger="before_tool_call",
             scope=RuleScope("global"),
             condition={"field": "tool_name", "eq": "glob_files"},
             actions=[{"type": "warn", "message": "no glob plz"}])
    engine.store.add(r)
    # tool_name == glob_files → match
    d1 = engine.evaluate("before_tool_call", {
        "tool_name": "glob_files",
        "scope": {"kind": "global"},
    })
    assert len(d1) == 1
    assert d1[0].action == "warn"
    # tool_name == read_file → no match (no decision)
    d2 = engine.evaluate("before_tool_call", {
        "tool_name": "read_file",
        "scope": {"kind": "global"},
    })
    assert len(d2) == 0


def test_deny_short_circuits(engine):
    """A 'deny' action stops further rules from being evaluated."""
    r1 = Rule(name="deny_me", trigger="before_tool_call",
              scope=RuleScope("global"), priority=10,
              actions=[{"type": "deny", "message": "blocked"}])
    r2 = Rule(name="warn_me", trigger="before_tool_call",
              scope=RuleScope("global"), priority=1,
              actions=[{"type": "warn", "message": "would warn"}])
    engine.store.add(r1)
    engine.store.add(r2)
    decisions = engine.evaluate("before_tool_call", {"scope": {"kind": "global"}})
    # Only one decision because r1's deny short-circuited
    assert len(decisions) == 1
    assert decisions[0].action == "deny"
    assert decisions[0].is_terminal


def test_priority_ordering(engine):
    r_low = Rule(name="low", trigger="before_tool_call",
                 scope=RuleScope("global"), priority=1,
                 actions=[{"type": "log", "marker": "low"}])
    r_high = Rule(name="high", trigger="before_tool_call",
                  scope=RuleScope("global"), priority=10,
                  actions=[{"type": "log", "marker": "high"}])
    engine.store.add(r_low)
    engine.store.add(r_high)
    decisions = engine.evaluate("before_tool_call", {"scope": {"kind": "global"}})
    # High-priority rule decision first
    assert decisions[0].rule_name == "high"
    assert decisions[1].rule_name == "low"


# ── Audit log ────────────────────────────────────────────────────────

def test_audit_emits_per_decision(engine):
    r = Rule(name="audited", trigger="before_tool_call",
             scope=RuleScope("global"),
             actions=[{"type": "warn", "message": "hi"}])
    engine.store.add(r)
    engine.evaluate("before_tool_call", {
        "tool_name": "x",
        "agent": {"id": "a1", "name": "alice", "role": "coder"},
        "scope": {"kind": "global"},
    })
    audit_path = engine.data_dir / "rules" / "audit.jsonl"
    assert audit_path.is_file()
    lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["trigger"] == "before_tool_call"
    assert entry["decision"] == "warn"
    assert entry["agent"]["name"] == "alice"


# ── Failure isolation ────────────────────────────────────────────────

def test_malformed_condition_doesnt_break_chain(engine):
    """A rule with broken condition becomes a log entry but doesn't
    stop other rules from evaluating."""
    bad = Rule(name="bad", trigger="before_tool_call",
               scope=RuleScope("global"),
               condition={"all": "not_a_list"},   # malformed
               actions=[{"type": "deny", "message": "would deny"}])
    good = Rule(name="good", trigger="before_tool_call",
                scope=RuleScope("global"),
                actions=[{"type": "warn", "message": "still here"}])
    engine.store.add(bad)
    engine.store.add(good)
    decisions = engine.evaluate("before_tool_call", {"scope": {"kind": "global"}})
    # Bad rule produces a Decision with action=log + error set; good one fires.
    actions = [d.action for d in decisions]
    assert "log" in actions
    assert "warn" in actions
    # No deny because bad rule's condition errored — never matched.
    assert "deny" not in actions
