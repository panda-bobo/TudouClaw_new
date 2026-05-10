"""Track C task 2 tests: eval_suite framework + runner registry.

Tests focus on the framework only (registration, lookup, suite
execution, error handling). The bundled runners (legalbench_zh,
citation_accuracy) have their own test files.
"""
from __future__ import annotations

import pytest

from app.domain_expert.training import eval_suite as es
from app.domain_expert.training.eval_suite import EvalReport


# ── fixtures ──

class _FakeRunner:
    """Minimal valid runner for tests."""
    def __init__(self, runner_id: str = "fake", score: float = 0.5,
                 *, raises: bool = False):
        self.runner_id = runner_id
        self.score = score
        self.raises = raises
        self.calls: list[dict] = []

    def run(self, model, **kwargs) -> EvalReport:
        self.calls.append({"model": model, "kwargs": kwargs})
        if self.raises:
            raise RuntimeError("boom")
        return EvalReport(
            runner_id=self.runner_id,
            score=self.score,
            n_examples=2,
            n_correct=1,
        )


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Each test starts with an empty registry, then defaults restored
    after.
    """
    es.clear_registry()
    yield
    es.clear_registry()
    es._bootstrap_default_runners()


# ── EvalReport ──

def test_evalreport_to_dict_roundtrip():
    r = EvalReport(runner_id="x", score=0.8, n_examples=10, n_correct=8)
    d = r.to_dict()
    assert d["runner_id"] == "x"
    assert d["score"] == 0.8
    assert d["n_examples"] == 10
    assert d["succeeded"] is True


# ── registry ──

def test_register_and_get():
    runner = _FakeRunner("foo")
    es.register(runner)
    assert es.get("foo") is runner


def test_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        es.get("does_not_exist")


def test_register_rejects_runner_without_id():
    class Bad:
        run = lambda self, m: None
    with pytest.raises(ValueError):
        es.register(Bad())


def test_register_rejects_runner_without_run():
    class Bad:
        runner_id = "x"
    with pytest.raises(ValueError):
        es.register(Bad())


def test_list_runners_returns_sorted():
    es.register(_FakeRunner("zeta"))
    es.register(_FakeRunner("alpha"))
    es.register(_FakeRunner("mu"))
    assert es.list_runners() == ["alpha", "mu", "zeta"]


def test_unregister_is_noop_when_missing():
    es.unregister("never_registered")  # does not raise


def test_register_replaces_existing():
    es.register(_FakeRunner("dup", score=0.1))
    es.register(_FakeRunner("dup", score=0.9))
    assert es.get("dup").score == 0.9


# ── run_suite ──

def test_run_suite_executes_all_runners_in_order():
    es.register(_FakeRunner("a", score=0.1))
    es.register(_FakeRunner("b", score=0.9))

    def model(prompt: str, **kw) -> str:
        return "answer"

    reports = es.run_suite(model, [
        {"runner_id": "a"},
        {"runner_id": "b"},
    ])
    assert [r.runner_id for r in reports] == ["a", "b"]
    assert reports[0].score == 0.1
    assert reports[1].score == 0.9
    assert all(r.succeeded for r in reports)


def test_run_suite_passes_kwargs_to_runner():
    runner = _FakeRunner("k")
    es.register(runner)

    def model(prompt: str, **kw) -> str:
        return "x"

    es.run_suite(model, [
        {"runner_id": "k", "kwargs": {"max_examples": 5}},
    ])
    assert runner.calls[0]["kwargs"] == {"max_examples": 5}


def test_run_suite_isolates_runner_failures():
    """If one runner raises, others still execute and the error is
    captured in a failed report."""
    es.register(_FakeRunner("good", score=0.7))
    es.register(_FakeRunner("bad", raises=True))

    def model(prompt: str, **kw) -> str:
        return "x"

    reports = es.run_suite(model, [
        {"runner_id": "bad"},
        {"runner_id": "good"},
    ])
    assert len(reports) == 2
    assert reports[0].succeeded is False
    assert "boom" in " ".join(reports[0].errors)
    assert reports[1].succeeded is True
    assert reports[1].score == 0.7


def test_run_suite_handles_unknown_runner_as_failure():
    def model(p, **kw): return "x"
    reports = es.run_suite(model, [{"runner_id": "ghost"}])
    assert len(reports) == 1
    assert reports[0].succeeded is False
    assert reports[0].runner_id == "ghost"


def test_run_suite_validates_input():
    with pytest.raises(TypeError):
        es.run_suite(lambda p: "x", "not a list")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        es.run_suite(lambda p: "x", ["not a dict"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        es.run_suite(lambda p: "x", [{}])
