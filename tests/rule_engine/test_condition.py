"""DSL evaluator: every operator + combinator + edge case."""
from __future__ import annotations

import pytest

from app.rule_engine.condition import evaluate, ConditionError, get_field


CTX = {
    "tool_name": "glob_files",
    "args": {
        "pattern": "**/*",
        "path": "/Users/foo/.tudou_claw/workspaces/shared/abc",
    },
    "agent": {"id": "ag1", "role": "coder", "name": "小新"},
    "scope": {"kind": "project", "project_id": "ff0cd6b745"},
    "counters": {"glob_files_per_hour": 7},
}


# ── field accessor ────────────────────────────────────────────────────

def test_get_field_dotted():
    assert get_field(CTX, "tool_name") == "glob_files"
    assert get_field(CTX, "args.path").endswith("/abc")
    assert get_field(CTX, "agent.role") == "coder"
    assert get_field(CTX, "scope.project_id") == "ff0cd6b745"


def test_get_field_missing_returns_none():
    assert get_field(CTX, "args.nope") is None
    assert get_field(CTX, "deep.deeper.nope") is None
    assert get_field(CTX, "") is None


# ── operators ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cond,expected", [
    ({"field": "tool_name", "eq": "glob_files"}, True),
    ({"field": "tool_name", "eq": "write_file"}, False),
    ({"field": "tool_name", "ne": "write_file"}, True),
    ({"field": "tool_name", "in": ["glob_files", "search_files"]}, True),
    ({"field": "tool_name", "not_in": ["write_file"]}, True),
    ({"field": "args.pattern", "matches": r"^\*\*/"}, True),
    ({"field": "args.pattern", "matches": r"^abc$"}, False),
    ({"field": "scope.project_id", "exists": True}, True),
    ({"field": "scope.meeting_id", "exists": True}, False),
    ({"field": "scope.meeting_id", "missing": True}, True),
    ({"field": "counters.glob_files_per_hour", "gt": 5}, True),
    ({"field": "counters.glob_files_per_hour", "gte": 7}, True),
    ({"field": "counters.glob_files_per_hour", "lt": 5}, False),
    ({"field": "counters.glob_files_per_hour", "lte": 7}, True),
    ({"field": "args.path", "starts_with": "/Users"}, True),
    ({"field": "args.path", "ends_with": "/abc"}, True),
    ({"field": "args.path", "contains": "tudou_claw"}, True),
    ({"field": "agent.role", "length_eq": 5}, True),
    ({"field": "agent.role", "length_gt": 3}, True),
    ({"field": "agent.role", "length_lt": 10}, True),
])
def test_operators(cond, expected):
    matched, _ = evaluate(cond, CTX)
    assert matched is expected


def test_field_vs_field_via_dollar_field():
    matched, _ = evaluate(
        {"field": "agent.id", "eq": {"$field": "agent.id"}}, CTX,
    )
    assert matched is True
    matched2, _ = evaluate(
        {"field": "agent.id", "eq": {"$field": "agent.role"}}, CTX,
    )
    assert matched2 is False


# ── combinators ──────────────────────────────────────────────────────

def test_all_short_circuits_on_first_false():
    cond = {"all": [
        {"field": "tool_name", "eq": "glob_files"},
        {"field": "agent.role", "eq": "admin"},
        {"field": "scope.project_id", "exists": True},
    ]}
    matched, ev = evaluate(cond, CTX)
    assert matched is False
    # Evidence should record only first false (short-circuit)
    assert len(ev["all"]) == 2


def test_any_short_circuits_on_first_true():
    cond = {"any": [
        {"field": "agent.role", "eq": "admin"},
        {"field": "tool_name", "eq": "glob_files"},
        {"field": "scope.kind", "eq": "global"},
    ]}
    matched, ev = evaluate(cond, CTX)
    assert matched is True
    assert len(ev["any"]) == 2


def test_not_inverts():
    matched, _ = evaluate({"not": {"field": "tool_name", "eq": "glob_files"}}, CTX)
    assert matched is False


def test_nested_combinators():
    cond = {"all": [
        {"field": "tool_name", "in": ["glob_files", "search_files"]},
        {"any": [
            {"field": "scope.kind", "eq": "project"},
            {"field": "scope.kind", "eq": "meeting"},
        ]},
        {"not": {"field": "agent.role", "eq": "admin"}},
    ]}
    matched, _ = evaluate(cond, CTX)
    assert matched is True


# ── edge cases ───────────────────────────────────────────────────────

def test_empty_condition_always_matches():
    assert evaluate({}, CTX) == (True, {"reason": "empty_condition"})
    assert evaluate(None, CTX) == (True, {"reason": "empty_condition"})


def test_malformed_condition_raises():
    with pytest.raises(ConditionError):
        evaluate({"field": "x"}, CTX)  # no op
    with pytest.raises(ConditionError):
        evaluate({"all": "not_a_list"}, CTX)
    with pytest.raises(ConditionError):
        evaluate({"eq": "x"}, CTX)  # no field


def test_missing_field_doesnt_crash_numeric_op():
    matched, _ = evaluate({"field": "args.no_such", "gt": 5}, CTX)
    assert matched is False


def test_matches_with_invalid_regex_returns_false():
    matched, _ = evaluate({"field": "args.path", "matches": "[invalid("}, CTX)
    assert matched is False
