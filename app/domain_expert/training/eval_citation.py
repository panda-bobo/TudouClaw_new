"""Citation accuracy runner — runner_id = "citation_accuracy".

Domain experts must cite their sources. This runner takes a list of
"citation examples", asks the model each prompt with its associated
context bundle, and scores how accurately the model cites the supplied
sources.

A citation example dict::

    {
        "id":       str,
        "question": str,
        "context":  list[{"doc_id": str, "text": str}],
        # Either provide gold list of doc_ids the answer must cite ...
        "expected_citations": list[str],   # subset of context doc_ids
        # ... or accept any citation that exists in `context`.
    }

Scoring per example (each in [0, 1], averaged into the final score):

    - precision: of the doc_ids the model cited, fraction that appear
      in the supplied context (i.e. real, not hallucinated)
    - recall: of `expected_citations`, fraction the model actually
      produced
    - f1: harmonic mean of the two

The runner reports the macro-averaged F1 as `score` and the macro
precision/recall under `metrics`.

If `expected_citations` is missing for an example, recall is treated as
1.0 and only precision contributes (use case: open-domain QA where any
in-context cite counts).

Citation tokens recognised in model replies::

    [Doc#3]    [doc#3]    [Doc 3]    [#3]    (Doc#3)

Citation IDs are matched case-insensitively against `doc_id` either
verbatim or by extracting the trailing number ("Doc#3" matches doc_id
"3" or "Doc#3" or "doc-3").

No I/O, pure stdlib. Caller supplies the example list — typically built
from `corpus._manifest.json` plus the agent's eval template.
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

from .eval_suite import EvalReport, ModelCallable, register


RUNNER_ID = "citation_accuracy"


# A built-in default example set so the runner is exercisable even
# without Track A's manifest. The doc texts are intentionally short.
_DEFAULT_EXAMPLES: list[dict] = [
    {
        "id": "default_1",
        "question": "民事诉讼的举证责任如何分配？",
        "context": [
            {"doc_id": "1",
             "text": "民事诉讼证据规则第二条：当事人对自己提出的主张应当提供证据。"},
            {"doc_id": "2",
             "text": "刑事诉讼证据规则与民事诉讼不同，公诉机关承担举证责任。"},
        ],
        "expected_citations": ["1"],
    },
    {
        "id": "default_2",
        "question": "合同法中关于违约责任的规定？",
        "context": [
            {"doc_id": "3",
             "text": "合同法第一百零七条：当事人一方不履行合同义务的，应当承担违约责任。"},
            {"doc_id": "4",
             "text": "侵权责任法第二条：侵害民事权益，应当依照本法承担侵权责任。"},
        ],
        "expected_citations": ["3"],
    },
]


# ── citation extraction ──

# Captures the number/identifier portion of common citation tokens.
# Matches: [Doc#3], [doc#3], [Doc 3], [#3], (Doc#3), [DOC-3]
_CITATION_RE = re.compile(
    r"[\[\(]\s*(?:doc[\s\-#]*)?#?\s*([A-Za-z0-9_\-]+)\s*[\]\)]",
    re.IGNORECASE,
)


def _extract_citations(reply: str) -> list[str]:
    """Pull citation tokens out of a model reply, deduped + lower-cased."""
    if not isinstance(reply, str) or not reply:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _CITATION_RE.finditer(reply):
        tok = m.group(1).strip().lower()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _normalize_doc_id(doc_id: str) -> set[str]:
    """All forms a doc_id might appear as in a citation."""
    if not isinstance(doc_id, str):
        doc_id = str(doc_id)
    s = doc_id.strip().lower()
    forms = {s}
    # Strip non-alphanumerics for a "naked" form.
    naked = re.sub(r"[^a-z0-9]+", "", s)
    if naked:
        forms.add(naked)
    # Trailing-number form ("doc-3" → "3", "doc#3" → "3").
    num = re.search(r"\d+$", s)
    if num:
        forms.add(num.group(0))
    return forms


def _matches_doc_id(citation: str, doc_id: str) -> bool:
    """True if a citation token refers to the given doc_id."""
    cite_forms = _normalize_doc_id(citation)
    doc_forms = _normalize_doc_id(doc_id)
    return bool(cite_forms & doc_forms)


def _score_example(
    cited: list[str],
    context_doc_ids: list[str],
    expected: list[str] | None,
) -> tuple[float, float, float]:
    """Return (precision, recall, f1) for one example."""
    n_cited = len(cited)
    n_real = sum(
        1 for c in cited
        if any(_matches_doc_id(c, did) for did in context_doc_ids)
    )
    if n_cited == 0:
        # No citations made at all → precision is undefined; we treat
        # it as 0.0 if recall was expected, else 1.0 (vacuously OK).
        precision = 0.0 if expected else 1.0
    else:
        precision = n_real / n_cited

    if expected:
        n_expected = len(expected)
        n_hit = sum(
            1 for e in expected
            if any(_matches_doc_id(c, e) for c in cited)
        )
        recall = n_hit / n_expected if n_expected else 1.0
    else:
        recall = 1.0

    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _build_prompt(example: dict) -> str:
    """Build a prompt that asks the model to cite from the given context."""
    q = (example.get("question") or "").strip()
    ctx = example.get("context") or []
    lines = ["请根据下列资料回答问题，并在引用来源时使用 [Doc#<id>] 格式。", ""]
    for chunk in ctx:
        did = chunk.get("doc_id", "")
        text = (chunk.get("text") or "").strip()
        lines.append(f"[Doc#{did}] {text}")
    lines.append("")
    lines.append(f"问题: {q}")
    return "\n".join(lines)


# ── runner ──

class CitationAccuracyRunner:
    """Scores how accurately a model cites supplied context."""

    runner_id = RUNNER_ID

    def __init__(self, examples: Iterable[dict] | None = None):
        # Store explicit examples (used by callers who want to score
        # against a domain-specific set). Default to bundled examples.
        self._explicit = list(examples) if examples is not None else None

    def run(
        self,
        model: ModelCallable,
        *,
        examples: Iterable[dict] | None = None,
        max_examples: int | None = None,
        **_: Any,
    ) -> EvalReport:
        """Score `model` on citation accuracy.

        Args:
            model: callable taking a prompt string and returning a reply.
            examples: optional override list of citation examples. Falls
                      back to the runner's stored set, then to the
                      module default.
            max_examples: cap.

        Returns:
            EvalReport with score = macro F1 in [0, 1] and metrics
            containing macro precision/recall.
        """
        t0 = time.time()
        ex_source: list[dict] = list(
            examples if examples is not None
            else (self._explicit if self._explicit is not None else _DEFAULT_EXAMPLES)
        )
        if max_examples is not None:
            ex_source = ex_source[:max_examples]
        n = len(ex_source)
        if n == 0:
            return EvalReport(
                runner_id=RUNNER_ID,
                score=0.0,
                n_examples=0,
                n_correct=0,
                duration_seconds=time.time() - t0,
                started_at=t0,
                succeeded=False,
                errors=["no examples provided"],
            )

        precisions: list[float] = []
        recalls: list[float] = []
        f1s: list[float] = []
        n_correct = 0  # examples with f1 == 1.0
        errors: list[str] = []

        for ex in ex_source:
            prompt = _build_prompt(ex)
            try:
                reply = model(prompt)
            except Exception as exc:                # noqa: BLE001
                errors.append(f"{ex.get('id')}: {type(exc).__name__}: {exc}")
                precisions.append(0.0)
                recalls.append(0.0)
                f1s.append(0.0)
                continue

            cited = _extract_citations(reply)
            ctx_ids = [c.get("doc_id", "") for c in (ex.get("context") or [])]
            expected = ex.get("expected_citations")
            p, r, f1 = _score_example(cited, ctx_ids, expected)
            precisions.append(p)
            recalls.append(r)
            f1s.append(f1)
            if f1 == 1.0:
                n_correct += 1

        macro_p = sum(precisions) / n
        macro_r = sum(recalls) / n
        macro_f1 = sum(f1s) / n

        return EvalReport(
            runner_id=RUNNER_ID,
            score=macro_f1,
            n_examples=n,
            n_correct=n_correct,
            metrics={
                "macro_precision": macro_p,
                "macro_recall": macro_r,
                "macro_f1": macro_f1,
            },
            errors=errors,
            duration_seconds=time.time() - t0,
            started_at=t0,
            succeeded=True,
        )


# Auto-register on import.
register(CitationAccuracyRunner())


__all__ = [
    "RUNNER_ID",
    "CitationAccuracyRunner",
]
