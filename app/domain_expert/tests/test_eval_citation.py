"""Track C task 4 tests: citation accuracy runner."""
from __future__ import annotations

from app.domain_expert.training import eval_suite as es
from app.domain_expert.training import eval_citation as ec
from app.domain_expert.training.eval_citation import (
    CitationAccuracyRunner,
    RUNNER_ID,
    _extract_citations,
    _matches_doc_id,
    _score_example,
)


# ── citation extraction ──

def test_extract_citations_finds_common_forms():
    text = "See [Doc#1] and (Doc#2) plus [doc-3] also [#4]."
    out = _extract_citations(text)
    assert out == ["1", "2", "3", "4"]


def test_extract_citations_dedupes_and_lowers():
    text = "[Doc#A1] [Doc#a1] [DOC#A1]"
    out = _extract_citations(text)
    assert out == ["a1"]


def test_extract_citations_empty():
    assert _extract_citations("") == []
    assert _extract_citations("nothing to cite") == []
    assert _extract_citations(None) == []  # type: ignore[arg-type]


# ── doc_id matching ──

def test_matches_doc_id_naked_number():
    assert _matches_doc_id("3", "Doc#3") is True
    assert _matches_doc_id("doc#3", "3") is True
    assert _matches_doc_id("doc-3", "Doc#3") is True


def test_matches_doc_id_no_false_positive():
    assert _matches_doc_id("3", "4") is False
    assert _matches_doc_id("a", "b") is False


# ── _score_example ──

def test_score_example_perfect():
    p, r, f = _score_example(
        cited=["1"], context_doc_ids=["1", "2"], expected=["1"],
    )
    assert (p, r, f) == (1.0, 1.0, 1.0)


def test_score_example_hallucinated_citation():
    p, r, f = _score_example(
        cited=["1", "99"],          # 99 not in context → hallucinated
        context_doc_ids=["1", "2"],
        expected=["1"],
    )
    assert p == 0.5  # 1/2 cited are real
    assert r == 1.0  # found the expected
    assert 0 < f < 1


def test_score_example_missing_recall():
    p, r, f = _score_example(
        cited=[], context_doc_ids=["1"], expected=["1"],
    )
    assert p == 0.0
    assert r == 0.0
    assert f == 0.0


def test_score_example_no_expected_means_recall_one():
    """When expected_citations is missing, recall is vacuously 1.0."""
    p, r, f = _score_example(
        cited=["1"], context_doc_ids=["1", "2"], expected=None,
    )
    assert r == 1.0
    assert p == 1.0


# ── registry side-effect ──

def test_runner_is_registered():
    assert RUNNER_ID == "citation_accuracy"
    assert RUNNER_ID in es.list_runners()
    assert es.get(RUNNER_ID).runner_id == RUNNER_ID


# ── run() ──

def test_run_perfect_model_score_one():
    """A model that cites exactly the expected docs scores 1.0."""
    examples = [
        {
            "id": "ex1",
            "question": "Q1?",
            "context": [
                {"doc_id": "1", "text": "fact about A"},
                {"doc_id": "2", "text": "fact about B"},
            ],
            "expected_citations": ["1"],
        },
    ]

    def model(prompt: str, **_) -> str:
        return "The answer references [Doc#1] which says A."

    report = CitationAccuracyRunner().run(model, examples=examples)
    assert report.runner_id == "citation_accuracy"
    assert report.succeeded is True
    assert report.score == 1.0
    assert report.n_correct == 1
    assert report.metrics["macro_precision"] == 1.0
    assert report.metrics["macro_recall"] == 1.0


def test_run_hallucinated_model_drops_score():
    examples = [
        {
            "id": "ex1",
            "question": "Q?",
            "context": [{"doc_id": "1", "text": "real"}],
            "expected_citations": ["1"],
        },
    ]

    def model(prompt: str, **_) -> str:
        return "[Doc#1] [Doc#42]"  # 42 is hallucinated

    report = CitationAccuracyRunner().run(model, examples=examples)
    # precision = 1/2, recall = 1/1 → f1 = 2/3
    assert 0 < report.score < 1
    assert abs(report.metrics["macro_precision"] - 0.5) < 1e-9
    assert report.metrics["macro_recall"] == 1.0


def test_run_handles_model_exception():
    examples = [{
        "id": "ex1", "question": "Q?",
        "context": [{"doc_id": "1", "text": "x"}],
        "expected_citations": ["1"],
    }]

    def model(prompt: str, **_) -> str:
        raise RuntimeError("flaky")

    report = CitationAccuracyRunner().run(model, examples=examples)
    assert report.succeeded is True            # whole run still succeeded
    assert report.score == 0.0                  # this example scored 0
    assert any("flaky" in e for e in report.errors)


def test_run_default_examples_when_none_given():
    """The runner has bundled examples so it works out of the box."""
    def model(prompt: str, **_) -> str:
        return "[Doc#1] [Doc#3]"

    report = CitationAccuracyRunner().run(model)
    assert report.n_examples == len(ec._DEFAULT_EXAMPLES)


def test_run_via_suite():
    def model(prompt: str, **_) -> str:
        return "[Doc#1]"

    reports = es.run_suite(model, [
        {"runner_id": "citation_accuracy",
         "kwargs": {"max_examples": 1}},
    ])
    assert len(reports) == 1
    assert reports[0].runner_id == "citation_accuracy"
    assert 0 <= reports[0].score <= 1
