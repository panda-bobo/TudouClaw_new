# Track C: Eval & Cleanup Framework Implementation Plan

> **Independent track.** Forks from `phase-0-complete`. No coordination with Tracks A/B/D until SP-2 vertical (training).
>
> **For agentic workers:** Use superpowers:executing-plans.

**Spec reference:** [§3.7.4 Trace cleaning, §3.1.2 eval_suite, §3.12 LoRA version mgmt](../specs/2026-05-10-agent-specialty-cultivation-design.md)

**Goal:** Pre-build the SP-2 components that don't depend on Track A/B/D outputs. Trace cleaning rules, eval suite framework, citation validator, LegalBench-zh runner stub. All pure logic + pytest.

**Architecture:** Pure backend in `app/domain_expert/training/`. No API, no UI. Each module has clear input/output contracts so SP-2 verticals just compose them.

**Tech Stack:** Python stdlib + `datasets` (for LegalBench-zh load), no heavy ML deps in Track C scope.

**Verification:** pytest only. Each task = unit-tested module.

---

## File Structure

```
app/domain_expert/training/
├── __init__.py                     # exports: trace_cleaner, eval_suite
├── trace_cleaner.py                # Trace cleaning rules (5 layers)
├── eval_suite.py                   # Eval runner registry + framework
├── eval_runners/
│   ├── __init__.py
│   ├── legalbench_zh.py            # LegalBench-zh runner stub
│   └── citation_validator.py       # Citation accuracy runner
└── (synth.py, trainer.py — reserved for SP-2 vertical, not Track C)

app/domain_expert/tests/
├── test_trace_cleaner.py
├── test_eval_suite.py
└── test_citation_validator.py

app/domain_expert/tests/fixtures/
├── traces_dirty.jsonl              # Test fixture: traces with various issues
└── citation_test_cases.json        # Citation validation test cases
```

---

## Task C1: Trace cleaner — 5 cleaning rules

**Goal:** Implement the cleaning rules listed in spec §3.7.4. Pure data-processing module: input list of trace dicts → output cleaned list + stats.

- [ ] **Step 1: Write `app/domain_expert/training/trace_cleaner.py`**

```python
"""Trace cleaning rules per spec §3.7.4.

Rules (applied in order):
  1. Dedup by Q-text similarity (cosine ≥ 0.95)
  2. Length filter: Q < 5 chars OR A < 20 chars → drop
  3. Garbage filter: test patterns / single-char dominance > 60%
  4. Low-quality flag: 👎 OR distill self-score < 0.5 → mark, don't drop
  5. (Monthly cron — see SP-2 scheduler — re-runs all rules on full pool)
"""
from __future__ import annotations
import logging
import re
import hashlib
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger("tudouclaw.expert.training.trace_cleaner")


@dataclass
class CleanReport:
    total_input: int = 0
    total_output: int = 0
    dropped_dedup: int = 0
    dropped_short: int = 0
    dropped_garbage: int = 0
    flagged_low_quality: int = 0
    notes: list[str] = field(default_factory=list)


# ── Rule 1: dedup ──
# We use a cheap hash-based first pass + optional embedding similarity if a
# function is supplied. Default = hash-only (Track A's embedder is optional dep).

def _hash_q(q: str) -> str:
    return hashlib.md5(q.strip().lower().encode("utf-8")).hexdigest()


def dedup_traces(
    traces: list[dict],
    similarity_fn=None,
    similarity_threshold: float = 0.95,
) -> tuple[list[dict], int]:
    """Remove near-duplicate traces by Q text. Returns (kept, dropped_count).

    similarity_fn: optional callable (q1, q2) -> float in [0,1]. If None, uses
                   exact-hash dedup (faster, less accurate).
    """
    seen_hashes: set[str] = set()
    seen_qs: list[str] = []  # for similarity comparison
    kept: list[dict] = []
    dropped = 0
    # Sort by quality preference: feedback=👍 first, then imported, then organic
    ordered = sorted(traces, key=lambda t: (
        -1 if t.get("feedback") == "thumbs_up" else 0,
        -1 if t.get("origin") == "import" else 0,
        -t.get("created_at", 0),
    ))
    for t in ordered:
        q = (t.get("Q") or "").strip()
        if not q:
            continue
        h = _hash_q(q)
        if h in seen_hashes:
            dropped += 1
            continue
        # Optional similarity check
        if similarity_fn:
            is_dup = False
            for prev_q in seen_qs[-100:]:  # bound at 100 for speed
                try:
                    if similarity_fn(q, prev_q) >= similarity_threshold:
                        is_dup = True
                        break
                except Exception:
                    pass
            if is_dup:
                dropped += 1
                continue
        seen_hashes.add(h)
        seen_qs.append(q)
        kept.append(t)
    return kept, dropped


# ── Rule 2: length filter ──

def filter_short(
    traces: list[dict],
    min_q_chars: int = 5,
    min_a_chars: int = 20,
) -> tuple[list[dict], int]:
    kept, dropped = [], 0
    for t in traces:
        q = (t.get("Q") or "").strip()
        a = (t.get("A") or "").strip()
        if len(q) < min_q_chars or len(a) < min_a_chars:
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


# ── Rule 3: garbage filter ──

_TEST_TOKENS = {"test", "testing", "asdf", "qwer", "zxcv", "123", "1234",
                "测试", "test test", "你好你好"}


def _is_garbage(q: str, a: str) -> bool:
    norm = q.lower().strip()
    if norm in _TEST_TOKENS:
        return True
    # single-char dominance > 60%
    stripped = re.sub(r"[\s\W]", "", norm)
    if len(stripped) >= 4:
        counts = Counter(stripped)
        max_freq = counts.most_common(1)[0][1]
        if max_freq / len(stripped) > 0.6:
            return True
    # answer too repetitive
    a_norm = re.sub(r"[\s\W]", "", a.lower())
    if len(a_norm) >= 6:
        a_counts = Counter(a_norm)
        if a_counts.most_common(1)[0][1] / len(a_norm) > 0.7:
            return True
    return False


def filter_garbage(traces: list[dict]) -> tuple[list[dict], int]:
    kept, dropped = [], 0
    for t in traces:
        q = (t.get("Q") or "")
        a = (t.get("A") or "")
        if _is_garbage(q, a):
            dropped += 1
            continue
        kept.append(t)
    return kept, dropped


# ── Rule 4: low-quality flag ──

def flag_low_quality(
    traces: list[dict],
    distill_score_threshold: float = 0.5,
) -> tuple[list[dict], int]:
    """Doesn't drop — sets `low_quality: True` on traces matching criteria."""
    flagged_count = 0
    for t in traces:
        is_low = False
        if t.get("feedback") == "thumbs_down":
            is_low = True
        if t.get("distill_score", 1.0) < distill_score_threshold:
            is_low = True
        if is_low:
            t["low_quality"] = True
            flagged_count += 1
    return traces, flagged_count


# ── Orchestrator ──

def clean(
    traces: list[dict],
    similarity_fn=None,
    similarity_threshold: float = 0.95,
    min_q_chars: int = 5,
    min_a_chars: int = 20,
    distill_score_threshold: float = 0.5,
) -> tuple[list[dict], CleanReport]:
    """Apply all 4 cleaning rules in order, return (cleaned, report)."""
    rep = CleanReport(total_input=len(traces))
    # Rule 2 first (cheap)
    traces, dropped = filter_short(traces, min_q_chars, min_a_chars)
    rep.dropped_short = dropped
    # Rule 3
    traces, dropped = filter_garbage(traces)
    rep.dropped_garbage = dropped
    # Rule 1 (dedup, more expensive)
    traces, dropped = dedup_traces(traces, similarity_fn, similarity_threshold)
    rep.dropped_dedup = dropped
    # Rule 4 (flag, doesn't drop)
    traces, flagged = flag_low_quality(traces, distill_score_threshold)
    rep.flagged_low_quality = flagged
    rep.total_output = len(traces)
    return traces, rep
```

- [ ] **Step 2: Test fixtures `app/domain_expert/tests/fixtures/traces_dirty.jsonl`**

Create file with these test cases (one per line):

```json
{"Q": "合同违约金过高怎么办?", "A": "依《民法典》第585条,可请求适当减少。", "origin": "organic", "feedback": "thumbs_up"}
{"Q": "合同违约金过高怎么办?", "A": "另一个回答,但 Q 重复", "origin": "organic"}
{"Q": "test", "A": "irrelevant test answer", "origin": "organic"}
{"Q": "asdf", "A": "garbage", "origin": "organic"}
{"Q": "短", "A": "this Q is too short", "origin": "organic"}
{"Q": "Long enough question here?", "A": "no", "origin": "organic"}
{"Q": "我我我我我我我我我我", "A": "single-char-dominant Q", "origin": "organic"}
{"Q": "Real question about contracts", "A": "Real legal answer that is long enough", "origin": "organic", "feedback": "thumbs_down"}
{"Q": "Distillation low-quality test", "A": "Some answer here with enough length", "origin": "organic", "distill_score": 0.3}
{"Q": "Imported high-quality Q", "A": "Imported answer with full content", "origin": "import"}
```

- [ ] **Step 3: Test `app/domain_expert/tests/test_trace_cleaner.py`**

```python
import os
import json
import pytest
from app.domain_expert.training import trace_cleaner

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "traces_dirty.jsonl")


def load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_filter_short_drops_short_q():
    traces = [
        {"Q": "a", "A": "long enough answer text"},
        {"Q": "long enough question", "A": "long enough answer text"},
        {"Q": "long enough question 2", "A": "no"},
    ]
    kept, dropped = trace_cleaner.filter_short(traces, min_q_chars=5, min_a_chars=10)
    assert dropped == 2
    assert len(kept) == 1


def test_filter_garbage_drops_test_tokens():
    traces = [
        {"Q": "test", "A": "long answer here please"},
        {"Q": "asdf", "A": "long answer here please"},
        {"Q": "real question", "A": "long answer here please"},
    ]
    kept, dropped = trace_cleaner.filter_garbage(traces)
    assert dropped == 2
    assert kept[0]["Q"] == "real question"


def test_filter_garbage_drops_single_char_dominance():
    traces = [
        {"Q": "我我我我我我我我", "A": "x" * 30},
        {"Q": "normal question", "A": "x" * 30},
    ]
    kept, dropped = trace_cleaner.filter_garbage(traces)
    assert dropped == 1


def test_dedup_keeps_one():
    traces = [
        {"Q": "same Q", "A": "first answer", "feedback": "thumbs_up"},
        {"Q": "same Q", "A": "second answer"},
    ]
    kept, dropped = trace_cleaner.dedup_traces(traces)
    assert dropped == 1
    assert len(kept) == 1
    # Higher-priority kept (thumbs_up wins)
    assert kept[0]["A"] == "first answer"


def test_flag_low_quality_doesnt_drop():
    traces = [
        {"Q": "Q1", "A": "A1", "feedback": "thumbs_down"},
        {"Q": "Q2", "A": "A2", "distill_score": 0.3},
        {"Q": "Q3", "A": "A3"},
    ]
    out, flagged = trace_cleaner.flag_low_quality(traces)
    assert flagged == 2
    assert len(out) == 3  # all kept
    assert out[0].get("low_quality") is True
    assert out[1].get("low_quality") is True
    assert out[2].get("low_quality") is None


def test_clean_full_pipeline():
    traces = load_fixture()
    out, rep = trace_cleaner.clean(traces)
    assert rep.total_input == 10
    # Expected drops:
    #   - "test" garbage
    #   - "asdf" garbage
    #   - "短" too-short Q
    #   - "Long enough question here?" too-short A
    #   - "我我我..." single-char dominance
    #   - one of the 2 dedup duplicates
    assert rep.dropped_short >= 2
    assert rep.dropped_garbage >= 2
    assert rep.dropped_dedup >= 1
    # Expected flags: thumbs_down (1) + low distill (1)
    assert rep.flagged_low_quality == 2
    assert rep.total_output >= 3
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_trace_cleaner.py -v
# expect: 6 passed
git add app/domain_expert/training/trace_cleaner.py \
  app/domain_expert/tests/test_trace_cleaner.py \
  app/domain_expert/tests/fixtures/traces_dirty.jsonl
git commit -m "Track C task 1: trace cleaner with 4 cleaning rules + 6 unit tests"
```

---

## Task C2: Eval suite framework + runner registry

**Goal:** Generic infrastructure for running named eval suites against an inference function. Each `eval_runner` registers a name and exposes a `run(model_callable, dataset) -> score`. Specialty YAMLs reference runner names.

- [ ] **Step 1: Write `app/domain_expert/training/eval_suite.py`**

```python
"""Eval suite registry + base class.

Per spec §3.1.2 (eval_suite YAML schema). Each specialty YAML lists its
benchmarks; this module dispatches them to registered runners.

Runner contract:
    runner.run(callable, **kwargs) -> EvalReport
where `callable(query: str, retrieved_docs: list[str] = None) -> str` is
the model under test.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class EvalReport:
    runner_id: str
    metric_name: str
    score: float
    n_examples: int
    per_example: list[dict] = field(default_factory=list)
    notes: str = ""


class EvalRunner(ABC):
    """Base for all eval runners."""

    runner_id: str = ""  # e.g. "legalbench_zh"
    metric_name: str = ""  # e.g. "accuracy"

    @abstractmethod
    def run(
        self,
        model_callable: Callable[..., str],
        max_examples: int | None = None,
        **kwargs,
    ) -> EvalReport:
        """Run the eval. `model_callable` takes query + optional context, returns answer."""
        ...


# ── Registry ──
_REGISTRY: dict[str, type[EvalRunner]] = {}


def register(runner_id: str):
    def deco(cls):
        cls.runner_id = runner_id
        if runner_id in _REGISTRY:
            raise ValueError(f"runner {runner_id!r} already registered")
        _REGISTRY[runner_id] = cls
        return cls
    return deco


def get(runner_id: str, **config) -> EvalRunner:
    if runner_id not in _REGISTRY:
        raise KeyError(f"unknown eval runner {runner_id!r}; "
                       f"registered: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[runner_id](**config)


def list_runners() -> list[str]:
    return sorted(_REGISTRY.keys())


def run_suite(
    model_callable: Callable[..., str],
    runner_specs: list[dict],
) -> dict[str, EvalReport]:
    """Run multiple eval runners. Each spec = {runner_id, max_examples?, **kwargs}."""
    reports = {}
    for spec in runner_specs:
        rid = spec["runner_id"]
        kwargs = {k: v for k, v in spec.items() if k != "runner_id"}
        runner = get(rid)
        rep = runner.run(model_callable, **kwargs)
        reports[rid] = rep
    return reports
```

- [ ] **Step 2: Test**

```python
# app/domain_expert/tests/test_eval_suite.py
import pytest
from app.domain_expert.training import eval_suite as es


def test_registry_basic():
    @es.register("dummy_runner")
    class DummyRunner(es.EvalRunner):
        metric_name = "accuracy"

        def run(self, model_callable, max_examples=None, **kw):
            return es.EvalReport(
                runner_id="dummy_runner",
                metric_name="accuracy",
                score=0.5,
                n_examples=2,
            )

    assert "dummy_runner" in es.list_runners()
    runner = es.get("dummy_runner")
    rep = runner.run(lambda q, **kw: "x")
    assert rep.score == 0.5


def test_unknown_runner_raises():
    with pytest.raises(KeyError):
        es.get("nonexistent_runner_xyz")


def test_run_suite_combines():
    @es.register("a_runner")
    class A(es.EvalRunner):
        def run(self, fn, **kw):
            return es.EvalReport(runner_id="a_runner", metric_name="a", score=0.7, n_examples=1)

    @es.register("b_runner")
    class B(es.EvalRunner):
        def run(self, fn, **kw):
            return es.EvalReport(runner_id="b_runner", metric_name="b", score=0.9, n_examples=1)

    reports = es.run_suite(
        lambda q, **kw: "x",
        runner_specs=[{"runner_id": "a_runner"}, {"runner_id": "b_runner"}],
    )
    assert "a_runner" in reports and "b_runner" in reports
    assert reports["a_runner"].score == 0.7
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_eval_suite.py -v
# expect: 3 passed
git add app/domain_expert/training/eval_suite.py app/domain_expert/tests/test_eval_suite.py
git commit -m "Track C task 2: eval suite registry + base class + 3 tests"
```

---

## Task C3: LegalBench-zh runner stub

**Goal:** Concrete eval runner for LegalBench-zh. Loads the dataset (from HF or local), runs model on each question, computes accuracy.

- [ ] **Step 1: Write `app/domain_expert/training/eval_runners/__init__.py`**

```python
"""Eval runner implementations. Importing this package registers all runners."""
# Registers via @register decorator at module-level
from . import legalbench_zh  # noqa: F401
from . import citation_validator  # noqa: F401
```

- [ ] **Step 2: Write `app/domain_expert/training/eval_runners/legalbench_zh.py`**

```python
"""LegalBench-zh eval runner.

Phase 1 stub: loads dataset (or mock), runs model on each Q, computes accuracy
by string match or substring. SP-2 vertical can replace with proper grading.
"""
from __future__ import annotations
import logging
from typing import Callable
from ..eval_suite import EvalRunner, EvalReport, register

logger = logging.getLogger("tudouclaw.expert.eval.legalbench_zh")


@register("legalbench_zh")
class LegalBenchZhRunner(EvalRunner):
    """LegalBench-zh accuracy on holdout set."""

    metric_name = "accuracy"

    def __init__(self, dataset_id: str = "yongzx/LegalBench-zh", split: str = "test"):
        self.dataset_id = dataset_id
        self.split = split

    def _load_dataset(self, max_examples: int | None = None):
        """Load from HF or fall back to mock fixture."""
        try:
            from datasets import load_dataset
            ds = load_dataset(self.dataset_id, split=self.split, streaming=False)
            if max_examples:
                ds = ds.select(range(min(max_examples, len(ds))))
            return list(ds)
        except Exception as e:
            logger.warning("LegalBench-zh dataset load failed: %s, using mock", e)
            # Fallback mock — 3 questions for sanity
            return [
                {"question": "违约金过高时,法院可以怎么处理?",
                 "answer": "适当减少", "options": ["适当减少", "完全取消", "加倍处罚"]},
                {"question": "民法典属于哪一编规定合同?",
                 "answer": "第三编 合同", "options": ["第一编 总则", "第二编 物权", "第三编 合同"]},
                {"question": "婚姻关系存续期间财产属于?",
                 "answer": "共同财产", "options": ["个人财产", "共同财产", "无主财产"]},
            ]

    def run(
        self,
        model_callable: Callable[..., str],
        max_examples: int | None = 100,
        **kwargs,
    ) -> EvalReport:
        examples = self._load_dataset(max_examples=max_examples)
        n = len(examples)
        if n == 0:
            return EvalReport(
                runner_id=self.runner_id, metric_name=self.metric_name,
                score=0.0, n_examples=0, notes="empty dataset",
            )
        correct = 0
        per_ex = []
        for ex in examples:
            q = ex.get("question", "")
            expected = ex.get("answer", "")
            try:
                got = model_callable(q)
            except Exception as e:
                got = ""
                logger.warning("model_callable failed on Q=%r: %s", q[:50], e)
            is_correct = self._matches(got, expected)
            if is_correct:
                correct += 1
            per_ex.append({"question": q, "expected": expected, "got": got, "correct": is_correct})
        score = correct / n if n else 0.0
        return EvalReport(
            runner_id=self.runner_id,
            metric_name=self.metric_name,
            score=score,
            n_examples=n,
            per_example=per_ex,
            notes=f"correct {correct}/{n}",
        )

    @staticmethod
    def _matches(got: str, expected: str) -> bool:
        """Phase 1 lenient string match. SP-2 can replace with LLM judge."""
        if not got or not expected:
            return False
        return expected.strip() in got.strip()
```

- [ ] **Step 3: Test**

```python
# app/domain_expert/tests/test_legalbench_zh.py
from app.domain_expert.training import eval_suite as es
from app.domain_expert.training.eval_runners import legalbench_zh  # noqa: F401


def test_legalbench_zh_runner_with_mock():
    runner = es.get("legalbench_zh")

    def perfect_model(q, **kw):
        # Returns the expected answer exactly
        if "违约金" in q:
            return "应当适当减少违约金"
        if "民法典" in q:
            return "第三编 合同"
        if "婚姻" in q:
            return "婚姻期间是共同财产"
        return ""

    report = runner.run(perfect_model, max_examples=3)
    assert report.n_examples == 3
    assert report.score == 1.0


def test_legalbench_zh_with_wrong_model():
    runner = es.get("legalbench_zh")

    def dumb_model(q, **kw):
        return "I don't know"

    report = runner.run(dumb_model, max_examples=3)
    assert report.score == 0.0


def test_legalbench_zh_partial():
    runner = es.get("legalbench_zh")
    n_correct = 0

    def half_right(q, **kw):
        nonlocal n_correct
        n_correct += 1
        return "适当减少" if n_correct == 1 else "wrong"

    report = runner.run(half_right, max_examples=3)
    assert 0 < report.score < 1
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_legalbench_zh.py -v
# expect: 3 passed (uses mock dataset, no live HF needed)
mkdir -p app/domain_expert/training/eval_runners
git add app/domain_expert/training/eval_runners/{__init__,legalbench_zh}.py \
  app/domain_expert/tests/test_legalbench_zh.py
git commit -m "Track C task 3: LegalBench-zh runner with mock fallback"
```

---

## Task C4: Citation accuracy validator

**Goal:** Validate that all `[Doc N]` citations in a model's answer correspond to real entries in the retrieval set. Returns ratio of valid citations.

- [ ] **Step 1: Write `app/domain_expert/training/eval_runners/citation_validator.py`**

```python
"""Citation accuracy runner.

Per spec §3.1.2 — answers MUST cite [Doc N] referring to real entries in
the retrieval context. This runner runs the model with controlled context
and verifies every citation is valid.

Score = valid_citations / total_citations. 1.0 = perfect; 0 = all
fabricated; if no citations at all, that's also 0 (model failing to cite).
"""
from __future__ import annotations
import re
from typing import Callable
from ..eval_suite import EvalRunner, EvalReport, register

CITE_PATTERN = re.compile(r"\[Doc\s*(\d+)\]", re.IGNORECASE)


@register("citation_accuracy")
class CitationValidator(EvalRunner):
    """Test that model cites real Doc indices, not fabricated ones."""

    metric_name = "ratio"

    def __init__(self, test_cases: list[dict] | None = None):
        # Each test case: {"query": str, "context_docs": [str], "expected_min_cites": int}
        self.test_cases = test_cases or self._default_cases()

    @staticmethod
    def _default_cases():
        return [
            {
                "query": "违约金过高怎么办?",
                "context_docs": [
                    "民法典第585条:当事人可以约定违约金...",
                    "民法典第586条:当事人可以约定定金...",
                    "无关条款 — 关于物权",
                ],
                "expected_min_cites": 1,
            },
            {
                "query": "合同的订立?",
                "context_docs": [
                    "民法典第464条 合同是民事主体之间...",
                    "民法典第469条 合同的形式...",
                ],
                "expected_min_cites": 1,
            },
        ]

    def run(
        self,
        model_callable: Callable[..., str],
        max_examples: int | None = None,
        **kwargs,
    ) -> EvalReport:
        cases = self.test_cases[:max_examples] if max_examples else self.test_cases
        total_cites = 0
        valid_cites = 0
        per_ex = []
        for case in cases:
            ctx = case["context_docs"]
            try:
                got = model_callable(case["query"], retrieved_docs=ctx)
            except TypeError:
                got = model_callable(case["query"])
            cites_found = CITE_PATTERN.findall(got or "")
            ex_total = len(cites_found)
            ex_valid = sum(1 for n in cites_found if 1 <= int(n) <= len(ctx))
            total_cites += ex_total
            valid_cites += ex_valid
            per_ex.append({
                "query": case["query"],
                "answer": got,
                "n_citations": ex_total,
                "n_valid": ex_valid,
                "min_required": case.get("expected_min_cites", 0),
            })
        score = valid_cites / total_cites if total_cites > 0 else 0.0
        return EvalReport(
            runner_id=self.runner_id,
            metric_name=self.metric_name,
            score=score,
            n_examples=len(cases),
            per_example=per_ex,
            notes=f"{valid_cites}/{total_cites} citations valid",
        )
```

- [ ] **Step 2: Test**

```python
# app/domain_expert/tests/test_citation_validator.py
from app.domain_expert.training import eval_suite as es
from app.domain_expert.training.eval_runners import citation_validator  # noqa: F401


def test_perfect_citations():
    runner = es.get("citation_accuracy")

    def good_model(q, retrieved_docs=None, **kw):
        # Always cites Doc 1 — within range
        return f"依据 [Doc 1] 的规定,答案是..."

    report = runner.run(good_model)
    assert report.score == 1.0


def test_fabricated_citation():
    runner = es.get("citation_accuracy")

    def bad_model(q, retrieved_docs=None, **kw):
        # Cites Doc 99 which doesn't exist
        return "依据 [Doc 99] 的规定,答案是..."

    report = runner.run(bad_model)
    assert report.score == 0.0


def test_no_citations():
    runner = es.get("citation_accuracy")

    def silent_model(q, retrieved_docs=None, **kw):
        return "I think so"

    report = runner.run(silent_model)
    assert report.score == 0.0
    # n_citations should be 0 across all
    for ex in report.per_example:
        assert ex["n_citations"] == 0


def test_mixed_citations():
    runner = es.get("citation_accuracy")

    def half_model(q, retrieved_docs=None, **kw):
        # Cites Doc 1 (valid) AND Doc 99 (fabricated)
        return "[Doc 1] is real but [Doc 99] is fake"

    report = runner.run(half_model)
    assert 0 < report.score < 1
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_citation_validator.py -v
# expect: 4 passed
git add app/domain_expert/training/eval_runners/citation_validator.py \
  app/domain_expert/tests/test_citation_validator.py
git commit -m "Track C task 4: citation accuracy validator with 4 test cases"
```

---

## Task C5: Wire up __init__.py + integration test

**Goal:** Module's `__init__.py` properly exports + importing `app.domain_expert.training` registers all runners.

- [ ] **Step 1: Write `app/domain_expert/training/__init__.py`**

```python
"""Training & eval framework.

Importing this package auto-registers all eval runners and chunkers.
Public exports:
    trace_cleaner — clean(traces) → cleaned + report
    eval_suite    — get(runner_id), run_suite(model, specs)
"""
from . import trace_cleaner       # noqa: F401
from . import eval_suite          # noqa: F401
from . import eval_runners        # noqa: F401  (registers via side-effect)
```

- [ ] **Step 2: Integration test**

```python
# app/domain_expert/tests/test_training_init.py
def test_import_registers_runners():
    from app.domain_expert.training import eval_suite as es
    runners = es.list_runners()
    assert "legalbench_zh" in runners
    assert "citation_accuracy" in runners
```

- [ ] **Step 3: Run all Track C tests**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/ -v -k "trace_cleaner or eval or citation or training"
# expect: all green
```

- [ ] **Step 4: Commit**

```bash
git add app/domain_expert/training/__init__.py app/domain_expert/tests/test_training_init.py
git commit -m "Track C task 5: training package __init__ + integration test"
```

---

## Self-Review

- ☑ All 5 tasks have unit tests; total ~16 test cases
- ☑ No HTTP / UI / DB schema touched
- ☑ Pure logic, no Track A/B/D dependencies
- ☑ TUDOU_EXPERT_DISABLED still works (this module just doesn't get imported)
- ☑ Each task ends with commit; reversible

## Handoff

After Track C merges, SP-2 vertical (training pipeline) consumes:

- `app.domain_expert.training.trace_cleaner.clean(traces)` for nightly cron
- `app.domain_expert.training.eval_suite.run_suite(model, specs)` for post-training eval
- `app.domain_expert.training.eval_suite.get("legalbench_zh")` and `get("citation_accuracy")` for level rule gates

Track D's `legal.yaml` will reference `runner_id: legalbench_zh` and `runner_id: citation_accuracy` — matches Track C's registry.
