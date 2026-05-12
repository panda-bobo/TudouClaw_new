"""Tests for the terraform MCP schema declaring working_dir must be
absolute (2026-05-12).

User: "MCP 调用的时候要声明是绝对路径" — the schema is what LLM reads
to know how to call the tool, so the absolute-path requirement
should be unambiguous there + reinforced by example + JSON-schema
``pattern`` regex.

Companion to 3c41337 (agent-side auto-resolve): the wrapper is a
safety net, but the schema declaration is what makes the LLM get
it right on first try.
"""
from __future__ import annotations

import pytest

from app.mcp.builtins.terraform import (
    TOOLS_SCHEMA,
    _validate_working_dir,
    _BASE_WD_SCHEMA,
)


def _tool(name: str) -> dict:
    for t in TOOLS_SCHEMA:
        if t.get("name") == name:
            return t
    raise KeyError(name)


# ── working_dir field declares absolute requirement ─────────────────

def test_working_dir_description_mentions_absolute():
    desc = _BASE_WD_SCHEMA["working_dir"]["description"]
    assert "absolute" in desc.lower()
    # Strong language: not just "absolute" but flagged as a MUST
    assert "must" in desc.lower()


def test_working_dir_has_pattern_regex():
    """JSON-schema ``pattern`` enforces '/'-prefix at validation time
    (model-side, when the LLM supports schema enforcement)."""
    assert _BASE_WD_SCHEMA["working_dir"].get("pattern") == "^/"


def test_working_dir_has_concrete_example():
    """Concrete example reduces the model's guesswork — it's much more
    effective at copying examples than synthesising paths from prose."""
    examples = _BASE_WD_SCHEMA["working_dir"].get("examples", [])
    assert examples, "missing examples on working_dir schema"
    # At least one example must start with '/'
    assert any(e.startswith("/") for e in examples)


def test_pattern_rejects_relative_paths():
    """Sanity: the regex literally won't accept anything that doesn't
    start with /."""
    import re
    pattern = _BASE_WD_SCHEMA["working_dir"]["pattern"]
    pat = re.compile(pattern)
    # Should match
    assert pat.match("/abs/path")
    assert pat.match("/")
    # Should NOT match
    assert not pat.match("relative/path")
    assert not pat.match("./modules/x")
    assert not pat.match("modules/x")


# ── terraform_init top-level description also reinforces ────────────

def test_terraform_init_description_reinforces_absolute_requirement():
    desc = _tool("terraform_init")["description"]
    assert "absolute" in desc.lower()


# ── _validate_working_dir error message is actionable ───────────────

def test_validate_error_message_explains_what_shape():
    """Old error: 'working_dir must be absolute'. New error includes
    'starts with /' + a concrete example so the LLM can self-correct
    on the first retry instead of looping."""
    ok, err = _validate_working_dir("modules/monitoring")
    assert not ok
    assert "absolute" in err.lower()
    # Reference to the prefix character
    assert "/" in err
    # Include the bad value verbatim so the LLM sees what went wrong
    assert "modules/monitoring" in err


def test_validate_error_includes_remediation_hint():
    """Beyond saying 'must be absolute', the message suggests HOW to
    fix it (prefix with workspace root)."""
    ok, err = _validate_working_dir("modules/x")
    assert "workspace" in err.lower() or "prefix" in err.lower()


def test_validate_empty_path_still_rejected():
    ok, err = _validate_working_dir("")
    assert not ok
    assert "required" in err.lower()


def test_validate_absolute_path_accepted_format_wise():
    """Pattern-wise the path is OK — actual isdir check may still fail
    if dir doesn't exist, but the 'absolute' check passes."""
    # Non-existent absolute path
    ok, err = _validate_working_dir("/nonexistent/path/zzz")
    # Fails on isdir, NOT on absolute
    assert not ok
    assert "absolute" not in err.lower()
    assert "does not exist" in err.lower() or "not a directory" in err.lower()


# ── every working_dir field across tool schemas inherits the base ──

def test_all_tools_inherit_base_working_dir_schema():
    """Every terraform_* tool's working_dir property must come from
    _BASE_WD_SCHEMA so the absolute-path declaration is consistent
    across the API surface."""
    expected = _BASE_WD_SCHEMA["working_dir"]
    for t in TOOLS_SCHEMA:
        props = t.get("inputSchema", {}).get("properties", {})
        if "working_dir" in props:
            wd = props["working_dir"]
            assert wd is expected or wd == expected, (
                f"tool {t['name']} has divergent working_dir schema — "
                f"should use _BASE_WD_SCHEMA via spread")
