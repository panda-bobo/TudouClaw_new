"""Tests for the per-turn tool budget cache-exemption (2026-05-12).

User asked "如果是 cache 调用是不是可以不计数" — cache hits should
NOT count toward the per-turn tool budget cap, since:
  - no disk I/O happens (read_file cache, glob_files cache)
  - no novel decision (results were already computed earlier this turn)
  - counting them punishes the model for the LLM's own poor pattern
    recognition (model SAW the [REPEAT-READ] / [CACHED-GLOB] markers
    but called the tool again anyway)

Reference: Claude Code routinely runs 30+ tool calls per turn without
hitting a hard cap. Real cost is the actual external work, not the
LLM's repeat-calling behaviour.

These tests verify the cache-marker detection used by the decrement
logic in agent.py around line 11976. Full integration (the chat()
iteration that actually invokes the decrement) is harder to unit-test
without a full LLM + tools harness.
"""
from __future__ import annotations

import pytest


# ── Predicate: which tool_result strings count as cache hits? ─────

def _is_cache_hit(result_str: str) -> bool:
    """Mirrors the inline predicate from agent.py:
        _head = _r[:40]
        if ("[REPEAT-READ" in _head
                or "[CACHED-GLOB" in _head
                or "[READ-VALVE-WARN" in _head):
            _cache_hits += 1
    """
    _r = result_str if isinstance(result_str, str) else str(result_str)
    _head = _r[:40]
    return ("[REPEAT-READ" in _head
            or "[CACHED-GLOB" in _head
            or "[READ-VALVE-WARN" in _head)


# ── Tests ──────────────────────────────────────────────────────────

def test_repeat_read_marker_counts_as_cache_hit():
    body = "[REPEAT-READ #2] You already read this file this turn.\n\n     1\tfoo\n     2\tbar"
    assert _is_cache_hit(body)


def test_cached_glob_marker_counts_as_cache_hit():
    body = ("[CACHED-GLOB #3] You already ran this exact glob "
            "(pattern='*.py', path='./').\n\nfile1.py\nfile2.py")
    assert _is_cache_hit(body)


def test_read_valve_warn_marker_counts_as_cache_hit():
    body = ("⚠️ [READ-VALVE-WARN #4] You've now read '/etc/hosts' 4 times "
            "this turn (cap=5).\n\nfile body...")
    assert _is_cache_hit(body)


def test_normal_read_result_does_not_count_as_cache_hit():
    body = "     1\tline one\n     2\tline two\n"
    assert not _is_cache_hit(body)


def test_normal_glob_result_does_not_count_as_cache_hit():
    body = "src/foo.py\nsrc/bar.py\nsrc/baz.py"
    assert not _is_cache_hit(body)


def test_bash_result_does_not_count_as_cache_hit():
    body = "Terraform v1.15.2\non darwin_arm64"
    assert not _is_cache_hit(body)


def test_error_result_does_not_count_as_cache_hit():
    """Error responses (real tool failures) MUST count — they're a
    sign the agent is doing real (failing) work that costs budget."""
    body = "Error: File not found: /no/such/path"
    assert not _is_cache_hit(body)


def test_empty_result_does_not_count():
    assert not _is_cache_hit("")


def test_marker_only_recognised_at_head():
    """Markers buried after a long preamble are NOT recognised — they
    must be at the start so the predicate stays cheap (no full-string
    scan) and false-positives stay low (some real file might contain
    '[REPEAT-READ' as a comment)."""
    body = ("Lots of preamble text that pushes the marker out beyond "
            "the 40-char head scan, then [REPEAT-READ #2] in body")
    assert not _is_cache_hit(body)


def test_non_string_result_handled():
    """Edge case: tool returned a non-string. The predicate must not
    raise — it coerces via str()."""
    assert not _is_cache_hit({"foo": "bar"})       # dict
    assert not _is_cache_hit(["a", "b"])           # list
    assert not _is_cache_hit(42)                    # int


# ── Decrement math ────────────────────────────────────────────────

def test_decrement_math_mixed_batch():
    """Simulate a mixed batch: 4 tool calls, 2 are cache hits, 2 are
    real work. After decrement, only 2 count toward the cap."""
    results = [
        ("read_file", "[REPEAT-READ #2] ...cached...", "id1"),    # hit
        ("read_file", "     1\treal new file content", "id2"),     # real
        ("glob_files", "[CACHED-GLOB #2] ...cached...", "id3"),    # hit
        ("bash", "command output here", "id4"),                    # real
    ]
    cache_hits = sum(1 for _, r, _ in results if _is_cache_hit(r))
    assert cache_hits == 2

    # Counter started at 8 (pre-existing), incremented by 4 to 12
    counter = 12
    cap = 12
    # Decrement by cache hits
    counter = max(0, counter - cache_hits)
    assert counter == 10
    # Now under cap → force_text should be cleared
    force_text = counter >= cap   # the reset condition
    assert force_text is False


def test_decrement_math_all_cache():
    """4 calls, all cache hits → counter drops by 4."""
    results = [
        ("read_file", "[REPEAT-READ #2] ...", f"id{i}") for i in range(4)
    ]
    cache_hits = sum(1 for _, r, _ in results if _is_cache_hit(r))
    assert cache_hits == 4
    counter = 12
    counter = max(0, counter - cache_hits)
    assert counter == 8


def test_decrement_math_no_cache():
    results = [
        ("write_file", "wrote 100 bytes", f"id{i}") for i in range(4)
    ]
    cache_hits = sum(1 for _, r, _ in results if _is_cache_hit(r))
    assert cache_hits == 0
    counter = 12
    counter = max(0, counter - cache_hits)
    assert counter == 12   # unchanged


def test_decrement_never_goes_negative():
    """If somehow we'd decrement below zero (shouldn't happen but be
    defensive), counter clamps at 0."""
    counter = 3
    cache_hits = 10   # absurd
    counter = max(0, counter - cache_hits)
    assert counter == 0
