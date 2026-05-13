"""Tests for edit_file's actionable 'old_string not found' error
(2026-05-13).

Real symptom from today: agent (刘老师, mimo-v2.5-pro) tried to edit
security/main.tf 4 times in a row with old_string strings it had
reconstructed from MEMORY. The actual file had different field
names and indentation. Each call returned:

    Error: old_string not found in /path/to/security/main.tf

Agent had no way to see WHAT the file actually contained, so it
retried with the same wrong text 4 times, hit the soft-cap budget
warning, and gave up.

Fix: when old_string isn't found, return:
  - explanation that whitespace / field-name drift is the likely cause
  - up to 3 closest-match lines from the file (with line numbers)
  - first 40 lines of the file (for the agent to copy from)
  - explicit instruction to NOT retry with same old_string
"""
from __future__ import annotations

import pytest

from app.tools_split.fs import _tool_edit_file
from app import sandbox as _sb


@pytest.fixture
def sandboxed_tmp(tmp_path):
    """Install an open sandbox rooted at tmp_path."""
    pol = _sb.SandboxPolicy(mode="open", root=str(tmp_path), allow_list=[])
    prev = _sb.set_current_policy(pol)
    try:
        yield tmp_path
    finally:
        _sb.set_current_policy(prev)


def _write(path, content):
    path.write_text(content, encoding="utf-8")
    return str(path)


# ── Old behavior preserved when string IS found ────────────────────

def test_successful_edit_unchanged(sandboxed_tmp):
    f = sandboxed_tmp / "x.txt"
    _write(f, "hello world\n")
    result = _tool_edit_file(
        path=str(f), old_string="hello", new_string="HI")
    # Success path returns formatted snippet, NOT an error
    assert "Error" not in result
    assert f.read_text() == "HI world\n"


# ── New: actionable error when not found ──────────────────────────

def test_not_found_includes_likely_cause(sandboxed_tmp):
    f = sandboxed_tmp / "x.tf"
    _write(f, "  field_a = 1\n  field_b = 2\n")
    result = _tool_edit_file(
        path=str(f),
        old_string="  totally_different_field = 99",
        new_string="x")
    assert "Likely cause" in result
    assert "whitespace" in result.lower()


def test_not_found_includes_file_preview(sandboxed_tmp):
    f = sandboxed_tmp / "x.tf"
    _write(f, "line1 = a\nline2 = b\nline3 = c\nline4 = d\n")
    result = _tool_edit_file(
        path=str(f), old_string="not in file", new_string="x")
    # File content present, with line numbers
    assert "line1" in result
    assert "line2" in result
    assert "line4" in result
    # Numbered (e.g. "   1  line1")
    import re
    assert re.search(r"\s+1\s+line1", result)


def test_not_found_explicitly_forbids_same_retry(sandboxed_tmp):
    f = sandboxed_tmp / "x.tf"
    _write(f, "abc\n")
    result = _tool_edit_file(
        path=str(f), old_string="not here", new_string="x")
    assert "DO NOT retry" in result
    assert "read_file" in result   # remediation hint mentioning read_file


def test_close_match_hint_when_field_name_typo(sandboxed_tmp):
    """The exact reported scenario: agent gave field name with typo /
    drift. The fuzzy hint should surface the real line so agent can
    self-correct."""
    f = sandboxed_tmp / "main.tf"
    _write(f, (
        "resource \"huaweicloud_kms_key_rotation\" \"default\" {\n"
        "  count = var.create_kms_key && var.enable_key_rotation ? 1 : 0\n"
        "\n"
        "  key_id     = huaweicloud_kms_key.default[0].id\n"
        "  interval   = var.key_rotation_interval # 天\n"
        "  is_enabled = true\n"
        "}\n"
    ))
    # Agent's wrong guess (uses `kms_key_rotation_enabled` and
    # `rotation_days` instead of the actual `enable_key_rotation` /
    # `interval`):
    bad = (
        "  count           = var.create_kms_key && "
        "var.kms_key_rotation_enabled ? 1 : 0\n"
        "  rotation_days   = var.kms_key_rotation_days"
    )
    result = _tool_edit_file(
        path=str(f), old_string=bad, new_string="# fixed")
    # Either close-match hint surfaces the right line, or at minimum
    # the file preview shows the real field names.
    assert ("enable_key_rotation" in result), (
        "agent should see real field name `enable_key_rotation` in "
        "either the close-match hint or the file preview")


def test_preview_capped_at_40_lines(sandboxed_tmp):
    """Don't dump giant files. 40-line cap keeps token cost bounded
    while giving enough context."""
    f = sandboxed_tmp / "big.tf"
    _write(f, "\n".join(f"line_{i}" for i in range(200)) + "\n")
    result = _tool_edit_file(
        path=str(f), old_string="missing", new_string="x")
    # Line 40 shown
    assert "line_39" in result   # 0-indexed line_39 = file line 40
    # Line 50 NOT shown (we cap at 40)
    assert "line_60" not in result
    assert "line_100" not in result


def test_count_gt_1_unchanged(sandboxed_tmp):
    """Existing >1 match error stays as-is."""
    f = sandboxed_tmp / "x.tf"
    _write(f, "foo = 1\nfoo = 2\n")
    result = _tool_edit_file(
        path=str(f), old_string="foo", new_string="bar")
    assert "found 2 times" in result
    assert "Must be unique" in result


# ── Robustness ────────────────────────────────────────────────────

def test_difflib_failure_falls_back_to_simple_error(sandboxed_tmp,
                                                     monkeypatch):
    """If the fuzzy-match logic somehow throws, the original
    'Error: old_string not found' is returned (no crash)."""
    f = sandboxed_tmp / "x.tf"
    _write(f, "abc\n")
    # Force difflib to crash
    import difflib
    def boom(*a, **k): raise RuntimeError("difflib down")
    monkeypatch.setattr(difflib, "get_close_matches", boom)
    result = _tool_edit_file(
        path=str(f), old_string="missing", new_string="x")
    # Falls back gracefully
    assert "Error" in result
    assert "not found" in result


def test_empty_file_handled(sandboxed_tmp):
    f = sandboxed_tmp / "empty.tf"
    _write(f, "")
    result = _tool_edit_file(
        path=str(f), old_string="anything", new_string="x")
    assert "Error" in result
    assert "not found" in result
