"""Track C task 3 tests: LegalBench-zh runner.

We force the offline (mock) path via TUDOU_EXPERT_OFFLINE=1 so tests
never hit the network. Verifies parsing, scoring, and the registry
side-effect of the import.
"""
from __future__ import annotations

import pytest

from app.domain_expert.training import eval_suite as es
from app.domain_expert.training import eval_legalbench_zh as lb
from app.domain_expert.training.eval_legalbench_zh import (
    LegalBenchZhRunner,
    _parse_answer,
    _format_prompt,
    RUNNER_ID,
)


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
    """Make every test go through the bundled mock, never HF."""
    monkeypatch.setenv("TUDOU_EXPERT_OFFLINE", "1")


# ── helpers / parser ──

def test_parse_answer_finds_letter():
    assert _parse_answer("B") == "B"
    assert _parse_answer("答案是 C。") == "C"
    # Surrounded by lowercase noise → the first standalone uppercase
    # letter wins (\b respects case in our regex pattern).
    assert _parse_answer("the answer is A.") == "A"
    assert _parse_answer("D 是正确答案") == "D"


def test_parse_answer_returns_empty_on_no_match():
    assert _parse_answer("我不知道") == ""
    assert _parse_answer("") == ""
    assert _parse_answer(None) == ""  # type: ignore[arg-type]


def test_format_prompt_includes_choices():
    ex = {
        "question": "Q?",
        "choices": {"A": "a-text", "B": "b-text", "C": "c", "D": "d"},
        "answer": "A",
    }
    p = _format_prompt(ex)
    assert "Q?" in p
    assert "A. a-text" in p
    assert "B. b-text" in p
    assert "请只回答字母选项" in p


# ── registry side-effect ──

def test_runner_is_registered():
    assert RUNNER_ID == "legalbench_zh"
    assert RUNNER_ID in es.list_runners()
    runner = es.get(RUNNER_ID)
    assert runner.runner_id == RUNNER_ID


# ── run() ──

def test_run_perfect_model_scores_one():
    """A model that always returns the correct gold answer → score 1.0."""
    examples = lb._MOCK_EXAMPLES

    def perfect(prompt: str, **_) -> str:
        # Find which mock example matches and return its answer.
        for ex in examples:
            if ex["question"] in prompt:
                return ex["answer"]
        return "A"

    report = LegalBenchZhRunner().run(perfect)
    assert report.runner_id == "legalbench_zh"
    assert report.succeeded is True
    assert report.n_examples == len(examples)
    assert report.n_correct == len(examples)
    assert report.score == 1.0
    assert report.metrics["source"] == "mock"


def test_run_wrong_model_scores_zero():
    def wrong(prompt: str, **_) -> str:
        # Always answer Z which never matches.
        return "Z"

    report = LegalBenchZhRunner().run(wrong)
    assert report.n_correct == 0
    assert report.score == 0.0


def test_run_max_examples_caps_dataset():
    def constant_b(prompt: str, **_) -> str:
        return "B"

    report = LegalBenchZhRunner().run(constant_b, max_examples=2)
    assert report.n_examples == 2


def test_run_handles_model_exceptions():
    """If the model raises mid-run, the example is recorded as an error
    but the run continues."""
    state = {"calls": 0}

    def flaky(prompt: str, **_) -> str:
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("transient")
        return "A"

    report = LegalBenchZhRunner().run(flaky, max_examples=4)
    assert report.succeeded is True             # whole run still succeeded
    assert report.n_examples == 4
    assert any("transient" in e for e in report.errors)


def test_run_via_suite():
    """End-to-end: run_suite → registered legalbench_zh → report."""
    def constant_a(prompt: str, **_) -> str:
        return "A"

    reports = es.run_suite(constant_a, [
        {"runner_id": "legalbench_zh", "kwargs": {"max_examples": 3}},
    ])
    assert len(reports) == 1
    assert reports[0].runner_id == "legalbench_zh"
    assert reports[0].n_examples == 3
    assert 0.0 <= reports[0].score <= 1.0
