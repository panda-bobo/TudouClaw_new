"""eval_suite — runner registry + suite execution framework.

A "runner" is anything that knows how to score a model on some benchmark
or qualitative test. Each runner conforms to the `EvalRunner` protocol:

    runner.runner_id : str            # unique key, e.g. "legalbench_zh"
    runner.run(model, **kw) -> EvalReport

`run_suite()` lets the caller execute several runners in one call and
collects all reports into a list, swallowing per-runner errors so a
single bad runner does not abort the whole suite (a per-runner error is
recorded as a failed report instead).

Concrete runner implementations live in sibling modules:

    eval_legalbench_zh.py    → "legalbench_zh"   (LegalBench-zh stub)
    eval_citation.py         → "citation_accuracy" (citation validator)

Track D's specialty templates (e.g. legal.yaml) reference runner IDs by
the exact string. SP-2 uses the same registry to score LoRA candidates.

No I/O. The caller decides how to persist `EvalReport` objects (the
expected location is ~/.tudou_claw/expert/<agent_id>/eval/<ts>.json).
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Protocol, runtime_checkable


# ── data types ──

# A model callable is anything that accepts a prompt str (and optional
# context) and returns the answer str. The exact signature is delegated
# to runners — they document what they pass in. This avoids coupling
# Track C to SP-2's actual model loader.
ModelCallable = Callable[..., str]


@dataclass
class EvalReport:
    """One runner's verdict on one model."""
    runner_id: str
    score: float                          # 0..1; runner-specific meaning
    n_examples: int = 0
    n_correct: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)  # extras
    errors: list[str] = field(default_factory=list)        # per-example errors
    duration_seconds: float = 0.0
    started_at: float = field(default_factory=time.time)
    succeeded: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class EvalRunner(Protocol):
    """Minimum interface every runner must satisfy."""
    runner_id: str

    def run(self, model: ModelCallable, **kwargs: Any) -> EvalReport:
        ...


# ── registry (module-level) ──

_REGISTRY: dict[str, EvalRunner] = {}


def register(runner: EvalRunner) -> None:
    """Register a runner. Re-registration replaces the existing entry,
    which is convenient for tests."""
    rid = getattr(runner, "runner_id", None)
    if not rid or not isinstance(rid, str):
        raise ValueError(
            "runner must expose a non-empty string `runner_id` attribute"
        )
    if not callable(getattr(runner, "run", None)):
        raise ValueError("runner must implement `run(model, **kwargs)`")
    _REGISTRY[rid] = runner


def unregister(runner_id: str) -> None:
    """Remove a runner from the registry (no-op if missing)."""
    _REGISTRY.pop(runner_id, None)


def get(runner_id: str) -> EvalRunner:
    """Look up a registered runner. Raises KeyError if unknown."""
    if runner_id not in _REGISTRY:
        raise KeyError(
            f"unknown eval runner: {runner_id!r}. "
            f"Registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[runner_id]


def list_runners() -> list[str]:
    """Sorted list of registered runner IDs."""
    return sorted(_REGISTRY)


def clear_registry() -> None:
    """Test helper — wipe the registry."""
    _REGISTRY.clear()


# ── suite execution ──

def run_suite(
    model: ModelCallable,
    runner_specs: list[dict],
) -> list[EvalReport]:
    """Run several registered runners against a single model callable.

    Each spec is a dict with shape::

        {"runner_id": "legalbench_zh", "kwargs": {"max_examples": 100}}

    `kwargs` is optional. Reports are returned in spec order.

    A runner that raises is captured: the offending report has
    `succeeded=False` and the exception's str in `errors`. Subsequent
    runners still execute. This matches the design contract — the
    caller (SP-2) needs to keep scoring even if one bench is broken.
    """
    if not isinstance(runner_specs, list):
        raise TypeError("runner_specs must be a list of dicts")

    reports: list[EvalReport] = []
    for spec in runner_specs:
        if not isinstance(spec, dict):
            raise TypeError(f"each spec must be a dict, got {type(spec).__name__}")
        rid = spec.get("runner_id")
        if not rid:
            raise ValueError("spec missing 'runner_id'")
        kwargs = spec.get("kwargs") or {}

        t0 = time.time()
        try:
            runner = get(rid)
            report = runner.run(model, **kwargs)
            if not isinstance(report, EvalReport):
                # Defensive: runner returned wrong type.
                report = EvalReport(
                    runner_id=rid,
                    score=0.0,
                    succeeded=False,
                    errors=[
                        f"runner returned {type(report).__name__}, "
                        "expected EvalReport"
                    ],
                    duration_seconds=time.time() - t0,
                )
        except Exception as exc:                # noqa: BLE001
            report = EvalReport(
                runner_id=rid,
                score=0.0,
                succeeded=False,
                errors=[f"{type(exc).__name__}: {exc}",
                        traceback.format_exc()],
                duration_seconds=time.time() - t0,
            )
        reports.append(report)
    return reports


# ── auto-registration of bundled runners ──
#
# Importing the runner modules below triggers their module-level
# `register(...)` calls. We do this lazily, swallowing ImportError so
# that environments missing optional deps (e.g. `datasets` for
# LegalBench-zh) still get a working eval_suite — the affected runner
# simply isn't registered. Callers can introspect with `list_runners()`.

def _bootstrap_default_runners() -> None:
    # Idempotent — safe to call multiple times. We import the runner
    # *classes* and register fresh instances. Going through `register`
    # ensures bundled runners reappear even if a test cleared the
    # registry (Python's import cache means `from . import eval_X`
    # would otherwise be a no-op on the second call).
    try:
        from .eval_citation import CitationAccuracyRunner
        register(CitationAccuracyRunner())
    except ImportError:
        pass
    try:
        from .eval_legalbench_zh import LegalBenchZhRunner
        register(LegalBenchZhRunner())
    except ImportError:
        pass


_bootstrap_default_runners()


__all__ = [
    "EvalReport",
    "EvalRunner",
    "ModelCallable",
    "register",
    "unregister",
    "get",
    "list_runners",
    "clear_registry",
    "run_suite",
]
