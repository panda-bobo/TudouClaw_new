"""Track C task 5: integration tests.

Verify the public surface declared in the task header works as
documented, and that the bundled runner IDs match Track D's contract
verbatim ("legalbench_zh", "citation_accuracy").
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Make legalbench_zh use its mock dataset."""
    monkeypatch.setenv("TUDOU_EXPERT_OFFLINE", "1")


def test_public_imports():
    """All four sub-modules must be reachable from the package."""
    from app.domain_expert.training import (
        trace_cleaner, eval_suite, eval_legalbench_zh, eval_citation,
    )
    # Each surface module should expose its documented entry points.
    assert callable(trace_cleaner.clean)
    assert callable(eval_suite.get)
    assert callable(eval_suite.run_suite)
    assert eval_legalbench_zh.RUNNER_ID == "legalbench_zh"
    assert eval_citation.RUNNER_ID == "citation_accuracy"


def test_required_runner_ids_registered():
    """Track D's legal.yaml will reference these IDs literally."""
    from app.domain_expert.training import eval_suite as es
    runners = es.list_runners()
    assert "legalbench_zh" in runners
    assert "citation_accuracy" in runners


def test_end_to_end_clean_then_eval():
    """Full Track-C user flow:
       1. clean a batch of synthetic traces
       2. run a 2-runner suite against a fake model
       3. inspect both reports
    """
    from app.domain_expert.training import trace_cleaner, eval_suite as es

    # 1. trace cleaning
    raw_traces = [
        {"id": "1", "question": "什么是法律？",
         "answer": "法律是国家制定或认可，由国家强制力保证实施的行为规范。",
         "citations": ["Doc#1"]},
        {"id": "2", "question": "什么是法律？",
         "answer": "法律是国家制定或认可，由国家强制力保证实施的行为规范。",
         "citations": ["Doc#1"]},                 # dup
        {"id": "3", "question": "什么是合同？",
         "answer": "TODO: write later"},          # garbage
        {"id": "4", "question": "什么是合同？",
         "answer": "I don't know."},              # low-quality flagged
    ]
    cleaned, report = trace_cleaner.clean(raw_traces)
    assert report["dropped_dedup"] == 1
    assert report["dropped_garbage"] == 1
    assert report["flagged_low_quality"] >= 1
    # 2 cleaned traces remain (id 1 and id 4)
    assert {t["id"] for t in cleaned} == {"1", "4"}

    # 2. eval suite — fake model that always cites Doc#1 and answers "B"
    def fake_model(prompt: str, **_) -> str:
        # Citation accuracy expects "[Doc#<id>]"; legalbench expects A/B/C/D.
        if "[Doc#" in prompt:
            return "Per [Doc#1] and [Doc#3], the answer follows."
        return "B"

    reports = es.run_suite(fake_model, [
        {"runner_id": "legalbench_zh", "kwargs": {"max_examples": 3}},
        {"runner_id": "citation_accuracy", "kwargs": {"max_examples": 1}},
    ])

    # 3. inspect reports
    assert len(reports) == 2
    assert {r.runner_id for r in reports} == {
        "legalbench_zh", "citation_accuracy",
    }
    for r in reports:
        assert r.succeeded is True
        assert 0.0 <= r.score <= 1.0
        assert r.n_examples > 0


def test_disabled_flag_does_not_break_imports(monkeypatch):
    """Even with TUDOU_EXPERT_DISABLED=1, the training package must
    still import cleanly — the flag gates the module's runtime
    activation (api router, reply hook), not the import itself.
    Track C is pure backend computation.
    """
    monkeypatch.setenv("TUDOU_EXPERT_DISABLED", "1")
    # Force a fresh import.
    import importlib
    from app.domain_expert import training as t
    importlib.reload(t)
    assert hasattr(t, "trace_cleaner")
    assert hasattr(t, "eval_suite")
