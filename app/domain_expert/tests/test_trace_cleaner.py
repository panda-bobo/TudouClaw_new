"""Track C task 1 tests: trace_cleaner.

Five cleaning rules + orchestrator. Pure stdlib, no I/O.
"""
from __future__ import annotations

from app.domain_expert.training import trace_cleaner


# ── helpers ──

def _t(qid: str, q: str, a: str, **kw) -> dict:
    """Quick trace builder."""
    base = {"id": qid, "question": q, "answer": a}
    base.update(kw)
    return base


# ── rule 1: dedup ──

def test_dedup_drops_identical_pairs():
    traces = [
        _t("1", "What is law?", "Law is rules."),
        _t("2", "What is law?", "Law is rules."),  # exact duplicate
        _t("3", "What is law?", "Law is order."),  # different answer → keep
    ]
    out = trace_cleaner.dedup(traces)
    assert len(out) == 2
    # First occurrence wins
    assert out[0]["id"] == "1"
    assert out[1]["id"] == "3"


def test_dedup_normalizes_whitespace_and_case():
    traces = [
        _t("1", "What is law?", "Law is rules."),
        _t("2", "what is  LAW?", "law is rules."),  # whitespace + case variant
    ]
    out = trace_cleaner.dedup(traces)
    assert len(out) == 1


def test_dedup_skips_non_dict_entries():
    traces = [_t("1", "Q", "A"), "not a dict", None, 42, _t("2", "Q2", "A2")]
    out = trace_cleaner.dedup(traces)
    assert len(out) == 2
    assert {t["id"] for t in out} == {"1", "2"}


# ── rule 2: length filter ──

def test_length_filter_drops_empty_qa():
    traces = [
        _t("1", "", "valid answer here"),
        _t("2", "valid q", ""),
        _t("3", "valid q", "valid answer here"),
    ]
    out = trace_cleaner.length_filter(traces)
    assert len(out) == 1
    assert out[0]["id"] == "3"


def test_length_filter_respects_bounds():
    traces = [
        _t("short_q", "ab", "valid answer xyz"),       # q too short
        _t("short_a", "valid q", "x"),                 # a too short
        _t("ok",      "valid q", "valid answer xyz"),  # ok
    ]
    out = trace_cleaner.length_filter(
        traces, min_q=4, min_a=8, max_q=100, max_a=100,
    )
    assert [t["id"] for t in out] == ["ok"]


def test_length_filter_max_bounds():
    traces = [
        _t("over_q", "x" * 50, "valid answer xyz"),
        _t("ok",     "valid q", "valid answer xyz"),
    ]
    out = trace_cleaner.length_filter(
        traces, min_q=4, min_a=8, max_q=20, max_a=100,
    )
    assert [t["id"] for t in out] == ["ok"]


# ── rule 3: garbage filter ──

def test_garbage_filter_drops_placeholder_text():
    traces = [
        _t("1", "What is X?", "TODO: write this answer later"),
        _t("2", "What is Y?", "[insert here]"),
        _t("3", "What is Z?", "Lorem ipsum dolor sit amet"),
        _t("4", "What is W?", "A real legitimate answer."),
    ]
    out = trace_cleaner.garbage_filter(traces)
    assert [t["id"] for t in out] == ["4"]


def test_garbage_filter_drops_control_chars():
    traces = [
        _t("ctrl", "good q", "answer\x00with null byte"),
        _t("ok",   "good q", "clean answer"),
    ]
    out = trace_cleaner.garbage_filter(traces)
    assert [t["id"] for t in out] == ["ok"]


# ── rule 4: low-quality flag ──

def test_low_quality_flag_marks_hedges():
    traces = [
        _t("hedge", "What is X?", "I don't know what X is."),
        _t("good",  "What is Y?", "Y is a defined legal term meaning ..."),
    ]
    out = trace_cleaner.low_quality_flag(traces)
    flags_by_id = {t["id"]: t["flags"] for t in out}
    assert "low_quality" in flags_by_id["hedge"]
    assert "low_quality" not in flags_by_id["good"]


def test_low_quality_flag_marks_curt_answers():
    """Answer shorter than question is suspicious."""
    traces = [
        _t("curt", "Please explain the doctrine of stare decisis", "Yes."),
    ]
    out = trace_cleaner.low_quality_flag(traces)
    assert "low_quality" in out[0]["flags"]


def test_low_quality_flag_low_score():
    traces = [_t("lo", "Q", "A long enough answer here", score=0.1)]
    out = trace_cleaner.low_quality_flag(traces)
    assert "low_quality" in out[0]["flags"]


def test_low_quality_flag_does_not_mutate_input():
    traces = [_t("hedge", "What?", "I don't know.")]
    out = trace_cleaner.low_quality_flag(traces)
    # Input untouched
    assert "flags" not in traces[0] or traces[0].get("flags", []) == []
    # Output flagged
    assert "low_quality" in out[0]["flags"]


# ── rule 5: clean orchestrator ──

def test_clean_runs_all_steps_and_returns_report():
    traces = [
        _t("dup1", "What is law?", "Law is rules."),
        _t("dup2", "What is law?", "Law is rules."),  # dropped by dedup
        _t("empty", "", "Some answer"),               # dropped by length
        _t("garbage", "Question?",
           "TODO: write later"),                      # dropped by garbage
        _t("hedge", "What is justice?",
           "I don't know what justice means."),       # flagged
        _t("good", "What is precedent?",
           "Precedent is a legal principle."),
    ]
    cleaned, report = trace_cleaner.clean(traces)

    # Counts
    assert report["input"] == 6
    assert report["dropped_dedup"] == 1
    assert report["dropped_length"] == 1
    assert report["dropped_garbage"] == 1
    assert report["output"] == 3        # dup1, hedge, good
    assert report["flagged_low_quality"] == 1
    # Right traces survived
    ids = {t["id"] for t in cleaned}
    assert ids == {"dup1", "hedge", "good"}


def test_clean_empty_input():
    cleaned, report = trace_cleaner.clean([])
    assert cleaned == []
    assert report["input"] == 0
    assert report["output"] == 0


def test_clean_rejects_non_list():
    import pytest
    with pytest.raises(TypeError):
        trace_cleaner.clean("not a list")  # type: ignore[arg-type]
