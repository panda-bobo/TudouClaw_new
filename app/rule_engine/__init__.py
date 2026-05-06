"""TudouClaw Rule Engine — unified policy decision/enforcement layer.

Replaces the scattered if-checks across auth.py / project.py /
tools_split/* with a single declarative rule store + evaluator.

Public API:

    from app.rule_engine import get_engine, Rule, Decision

    eng = get_engine()
    decisions = eng.evaluate(
        trigger="before_tool_call",
        context={
            "tool_name": "glob_files",
            "args": {...},
            "agent": {"id": "abc", "role": "coder", "name": "小新"},
            "scope": {"kind": "project", "project_id": "ff0cd6b745"},
        },
    )
    for d in decisions:
        if d.action == "deny":
            return d.message  # short-circuits the call

Module layout:
    __init__.py       — public exports + module-level helpers
    types.py          — Rule, Decision, RuleScope dataclasses
    store.py          — file-backed PolicyStore with versioning
    condition.py      — JSON-DSL evaluator (no eval/exec)
    action.py         — action handler registry (deny/warn/log/etc)
    engine.py         — Engine singleton: evaluate / register_trigger / audit
    audit.py          — append-only JSONL audit log
"""
from __future__ import annotations

from .types import Rule, Decision, RuleScope, ActionType
from .engine import Engine, get_engine, init_engine

__all__ = [
    "Rule", "Decision", "RuleScope", "ActionType",
    "Engine", "get_engine", "init_engine",
]
