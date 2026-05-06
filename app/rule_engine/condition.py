"""JSON-DSL condition evaluator — pure, no eval/exec.

Grammar (recursive):

    expr := operator | combinator
    combinator := {"all": [expr, ...]} | {"any": [expr, ...]} | {"not": expr}
    operator   := {"field": "<dotted.path>", "<op>": <value>}
                 | {"left": "<dotted>", "<op>": "<dotted>"}   # field-vs-field

Supported ops:
    eq, ne, in, not_in, exists, missing, matches (regex), gt, lt, gte, lte,
    starts_with, ends_with, contains, length_eq, length_gt, length_lt

Examples:

    {"field": "tool_name", "eq": "glob_files"}
    {"field": "scope.project_id", "exists": true}
    {"all": [
        {"field": "tool_name", "in": ["write_file", "edit_file"]},
        {"field": "args.path", "matches": r"^/tmp/.*"},
    ]}
    {"not": {"field": "agent.role", "eq": "admin"}}

Field accessor walks dotted paths against the context dict; missing
intermediate keys → None (which makes ``exists`` return False, ``eq``
return False unless the comparand is also None).
"""
from __future__ import annotations

import re
from typing import Any


_OPS = (
    "eq", "ne", "in", "not_in", "exists", "missing",
    "matches", "gt", "lt", "gte", "lte",
    "starts_with", "ends_with", "contains",
    "length_eq", "length_gt", "length_lt",
)
_COMBINATORS = ("all", "any", "not")


class ConditionError(Exception):
    """Raised when the condition JSON is malformed. Engine catches this
    and treats the rule as 'failed to evaluate' — defensively skipping
    it rather than crashing the call site."""


def get_field(context: dict, path: str) -> Any:
    """Walk dotted path through context. Returns None on any miss."""
    if not path:
        return None
    cursor: Any = context
    for part in path.split("."):
        if cursor is None:
            return None
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, (list, tuple)) and part.isdigit():
            idx = int(part)
            cursor = cursor[idx] if 0 <= idx < len(cursor) else None
        else:
            # Try attribute access for dataclass-like objects
            cursor = getattr(cursor, part, None)
    return cursor


def _resolve_value(spec: Any, context: dict) -> Any:
    """If spec is the marker {"$field": "..."}, look it up in context;
    otherwise return spec literally. Lets a rule compare two context
    fields to each other (e.g. agent.id == task.assigned_to)."""
    if isinstance(spec, dict) and "$field" in spec and len(spec) == 1:
        return get_field(context, spec["$field"])
    return spec


def evaluate(condition: dict, context: dict) -> tuple[bool, dict]:
    """Evaluate ``condition`` against ``context``.

    Returns (matched, evidence). ``evidence`` is the subtree of the
    condition that decided the outcome — used for audit log so admin
    can see "rule X fired because field Y == Z".

    Empty condition ({}) always matches (treated as "any context").
    """
    if condition is None or condition == {}:
        return True, {"reason": "empty_condition"}

    if not isinstance(condition, dict):
        raise ConditionError(f"condition must be a dict, got {type(condition).__name__}")

    # Combinators
    if "all" in condition:
        clauses = condition["all"]
        if not isinstance(clauses, list):
            raise ConditionError("'all' takes a list")
        evidence: dict = {"all": []}
        for c in clauses:
            ok, sub = evaluate(c, context)
            evidence["all"].append({"matched": ok, **sub})
            if not ok:
                return False, evidence
        return True, evidence

    if "any" in condition:
        clauses = condition["any"]
        if not isinstance(clauses, list):
            raise ConditionError("'any' takes a list")
        evidence = {"any": []}
        for c in clauses:
            ok, sub = evaluate(c, context)
            evidence["any"].append({"matched": ok, **sub})
            if ok:
                return True, evidence
        return False, evidence

    if "not" in condition:
        sub_cond = condition["not"]
        ok, sub = evaluate(sub_cond, context)
        return (not ok), {"not": sub}

    # Operator — must specify a target via "field" (or "left" for
    # field-vs-field) and exactly one op key.
    target_path = condition.get("field") or condition.get("left")
    if not isinstance(target_path, str):
        raise ConditionError(
            f"operator requires 'field' or 'left' string, got {condition!r}"
        )
    target_value = get_field(context, target_path)

    # Find the op key
    op_keys = [k for k in condition.keys() if k in _OPS]
    if len(op_keys) != 1:
        raise ConditionError(
            f"operator must specify exactly one of {_OPS}, got {op_keys}"
        )
    op = op_keys[0]
    rhs_spec = condition[op]
    rhs = _resolve_value(rhs_spec, context)

    matched = _apply_op(op, target_value, rhs)
    return matched, {
        "field": target_path,
        "op": op,
        "lhs": _safe_repr(target_value),
        "rhs": _safe_repr(rhs),
        "matched": matched,
    }


def _safe_repr(v: Any) -> Any:
    """JSON-friendly repr for evidence (truncate big strings)."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        if isinstance(v, str) and len(v) > 200:
            return v[:200] + "..."
        return v
    if isinstance(v, (list, tuple)):
        return [_safe_repr(x) for x in v[:10]]
    if isinstance(v, dict):
        return {k: _safe_repr(v) for k, v in list(v.items())[:10]}
    return repr(v)[:200]


def _apply_op(op: str, lhs: Any, rhs: Any) -> bool:
    """Apply a single operator. None lhs is treated leniently — ``exists``
    handles it explicitly; numeric ops treat None as "no match"."""
    if op == "exists":
        return lhs is not None and rhs is True or (lhs is None and rhs is False)
    if op == "missing":
        return lhs is None
    if op == "eq":
        return lhs == rhs
    if op == "ne":
        return lhs != rhs
    if op == "in":
        if not isinstance(rhs, (list, tuple, set)):
            return False
        return lhs in rhs
    if op == "not_in":
        if not isinstance(rhs, (list, tuple, set)):
            return True
        return lhs not in rhs
    if op == "matches":
        if not isinstance(lhs, str) or not isinstance(rhs, str):
            return False
        try:
            return re.search(rhs, lhs) is not None
        except re.error:
            return False
    if op == "starts_with":
        return isinstance(lhs, str) and isinstance(rhs, str) and lhs.startswith(rhs)
    if op == "ends_with":
        return isinstance(lhs, str) and isinstance(rhs, str) and lhs.endswith(rhs)
    if op == "contains":
        if isinstance(lhs, str) and isinstance(rhs, str):
            return rhs in lhs
        if isinstance(lhs, (list, tuple, set)):
            return rhs in lhs
        return False
    if op in ("gt", "lt", "gte", "lte"):
        if lhs is None or rhs is None:
            return False
        try:
            if op == "gt":  return lhs > rhs
            if op == "lt":  return lhs < rhs
            if op == "gte": return lhs >= rhs
            if op == "lte": return lhs <= rhs
        except TypeError:
            return False
    if op in ("length_eq", "length_gt", "length_lt"):
        try:
            n = len(lhs)
        except TypeError:
            return False
        try:
            r = int(rhs)
        except (TypeError, ValueError):
            return False
        if op == "length_eq": return n == r
        if op == "length_gt": return n > r
        if op == "length_lt": return n < r
    return False
