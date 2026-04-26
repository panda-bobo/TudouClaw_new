"""app.apply_diff — V4A diff parser.

Covers the surface our future coding skills will rely on:
- create-mode generates a file from + lines
- update-mode replaces lines via context match
- fuzzy whitespace match works (trailing-space and full-strip fallbacks)
- invalid context raises ValueError with a useful message
- CRLF newline style is detected from input and preserved on output
- multiple chunks in one section apply in sequence
- EOF anchoring picks the right tail
"""
from __future__ import annotations

import pytest

from app.apply_diff import apply_diff


# ── create-mode ───────────────────────────────────────────────────────


def test_create_mode_writes_plus_lines():
    diff = "+line one\n+line two\n+line three\n*** End Patch"
    out = apply_diff("", diff, mode="create")
    assert out == "line one\nline two\nline three"


def test_create_mode_rejects_non_plus_lines():
    diff = "+ok\nnot a + line\n*** End Patch"
    with pytest.raises(ValueError, match="Invalid Add File Line"):
        apply_diff("", diff, mode="create")


# ── update-mode (the common case) ─────────────────────────────────────


def test_update_replaces_via_context_anchor():
    orig = "line A\nline B\nold line C\nline D\nline E"
    diff = (
        "@@ line A\n"
        " line B\n"
        "- old line C\n"
        "+ NEW line C\n"
        " line D\n"
        "*** End Patch"
    )
    out = apply_diff(orig, diff, mode="default")
    # Note V4A treats "+ X" as content " X" (the char after `+` is content,
    # so leading space is preserved verbatim) — this is upstream semantics.
    assert "old line C" not in out
    assert " NEW line C" in out


def test_update_handles_multiple_chunks():
    orig = "a\nb\nc\nd\ne\nf"
    diff = (
        "@@ a\n"
        "- b\n"
        "+ B\n"
        " c\n"
        " d\n"
        "- e\n"
        "+ E\n"
        " f\n"
        "*** End Patch"
    )
    out = apply_diff(orig, diff, mode="default")
    out_lines = out.split("\n")
    assert "b" not in out_lines
    assert "e" not in out_lines
    assert " B" in out_lines
    assert " E" in out_lines


# ── fuzzy match ───────────────────────────────────────────────────────


def test_fuzzy_match_tolerates_trailing_whitespace():
    # Original has trailing spaces, diff context doesn't — should still match
    orig = "alpha  \nbeta\ngamma"  # alpha has 2 trailing spaces
    diff = (
        "@@ alpha\n"
        " beta\n"
        "- gamma\n"
        "+ GAMMA\n"
        "*** End Patch"
    )
    # Anchor 'alpha' (with trailing spaces) won't exact-match 'alpha';
    # parser falls back to strip().
    out = apply_diff(orig, diff, mode="default")
    assert "GAMMA" in out


def test_fuzzy_match_tolerates_indent_changes():
    orig = "def foo():\n    x = 1\n    return x"
    diff = (
        "@@ def foo():\n"
        "     x = 1\n"
        "-     return x\n"
        "+     return x + 1\n"
        "*** End Patch"
    )
    out = apply_diff(orig, diff, mode="default")
    assert "return x + 1" in out
    assert "return x\n" not in out and not out.endswith("return x")


# ── error paths ───────────────────────────────────────────────────────


def test_invalid_context_raises():
    orig = "hello world"
    diff = "@@ def nonexistent():\n line never appearing here\n*** End Patch"
    with pytest.raises(ValueError, match="Invalid Context"):
        apply_diff(orig, diff, mode="default")


def test_invalid_diff_line_prefix():
    """A line in the section starting with something other than ' / + / - is invalid."""
    orig = "a\nb\nc"
    diff = "@@ a\n!  bogus prefix\n*** End Patch"
    with pytest.raises(ValueError):
        apply_diff(orig, diff, mode="default")


# ── newline preservation ─────────────────────────────────────────────


def test_crlf_newlines_preserved():
    orig = "a\r\nb\r\nc"
    diff = "@@ a\n- b\n+ B!\n c\n*** End Patch"
    out = apply_diff(orig, diff, mode="default")
    assert "\r\n" in out
    assert "B!" in out


def test_lf_newlines_preserved():
    orig = "a\nb\nc"
    diff = "@@ a\n- b\n+ B!\n c\n*** End Patch"
    out = apply_diff(orig, diff, mode="default")
    assert "\r\n" not in out
    assert out.split("\n").count("") == 0  # no empty strays


# ── EOF anchoring ────────────────────────────────────────────────────


def test_eof_section_anchors_to_tail():
    orig = "header\nmiddle\ntail-marker"
    diff = (
        "@@\n"
        " tail-marker\n"
        "*** End of File\n"
        "*** End Patch"
    )
    # No-op patch: just locates EOF context. Should not raise.
    out = apply_diff(orig, diff, mode="default")
    assert out == orig


# ── empty handling ───────────────────────────────────────────────────


def test_empty_diff_is_noop():
    orig = "a\nb\nc"
    out = apply_diff(orig, "*** End Patch", mode="default")
    assert out == orig
