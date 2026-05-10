"""LegalBench-zh eval runner — runner_id = "legalbench_zh".

Runs a Chinese legal-reasoning benchmark against a model callable. The
benchmark itself is multiple-choice — for each example we ask the model
the question, parse a single-letter / single-token answer, and compare
to the gold label.

The real dataset lives on Hugging Face (`datasets` package). We try to
load it lazily; if `datasets` is missing or the load fails for any
reason (offline, gated repo, etc.), we fall back to a tiny *built-in*
mock dataset so unit tests and offline CI can still run end-to-end.

Track D's `legal.yaml` references the runner_id literal "legalbench_zh"
— do not rename without coordinating.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .eval_suite import EvalReport, ModelCallable, register


RUNNER_ID = "legalbench_zh"

# Default HF dataset path — kept here so it's easy to swap.
HF_DATASET_PATH = "ZhuJD-China/LegalBench-zh"
HF_DATASET_SPLIT = "test"


# Tiny built-in mock so tests don't need network. These mirror the
# expected schema produced by `_load_examples` (question + choices +
# answer letter). The exact texts are intentionally simple — the runner
# is being tested, not legal reasoning.
_MOCK_EXAMPLES: list[dict] = [
    {
        "id": "mock_1",
        "question": "下列哪一项属于民法的基本原则？",
        "choices": {
            "A": "罪刑法定",
            "B": "诚实信用",
            "C": "无罪推定",
            "D": "三审终审",
        },
        "answer": "B",
    },
    {
        "id": "mock_2",
        "question": "刑事诉讼中举证责任主要由谁承担？",
        "choices": {
            "A": "被告人",
            "B": "辩护人",
            "C": "公诉机关",
            "D": "证人",
        },
        "answer": "C",
    },
    {
        "id": "mock_3",
        "question": "下列合同中属于无效合同的是？",
        "choices": {
            "A": "口头买卖合同",
            "B": "限制民事行为能力人订立的所有合同",
            "C": "违反法律强制性规定的合同",
            "D": "标的额低于一百元的合同",
        },
        "answer": "C",
    },
    {
        "id": "mock_4",
        "question": "我国宪法规定的国家根本制度是？",
        "choices": {
            "A": "人民代表大会制度",
            "B": "社会主义制度",
            "C": "民族区域自治制度",
            "D": "基层群众自治制度",
        },
        "answer": "B",
    },
    {
        "id": "mock_5",
        "question": "下列哪种行为不构成正当防卫？",
        "choices": {
            "A": "对正在进行的不法侵害实施防卫",
            "B": "防卫行为造成轻微损害",
            "C": "对已经结束的不法侵害实施事后报复",
            "D": "为保护他人合法权益实施的防卫",
        },
        "answer": "C",
    },
]


# ── helpers ──

def _format_prompt(example: dict) -> str:
    """Build a deterministic Chinese prompt: question + choice list."""
    q = (example.get("question") or "").strip()
    choices = example.get("choices") or {}
    lines = [q, ""]
    for letter in ("A", "B", "C", "D"):
        text = choices.get(letter, "")
        lines.append(f"{letter}. {text}")
    lines.append("")
    lines.append("请只回答字母选项 (A/B/C/D)。")
    return "\n".join(lines)


# A letter counts as "standalone" only if it's at start-of-string or
# preceded by whitespace, and followed by end-of-string, whitespace, or
# common punctuation. Apostrophes are excluded so contractions like
# "I'd" don't accidentally yield a 'D' answer.
_LETTER_RE = re.compile(r"(?:^|(?<=\s))([ABCD])(?=$|[\s.,;:!?。，！？、])")


def _parse_answer(reply: str) -> str:
    """Extract the model's predicted letter (A/B/C/D) from its reply.

    We accept the first standalone letter; failing that we look at the
    first uppercased character. Returns "" if nothing matches.
    """
    if not isinstance(reply, str):
        return ""
    s = reply.strip().upper()
    m = _LETTER_RE.search(s)
    if m:
        return m.group(1)
    # Fallback: very first char if it's a valid letter
    if s and s[0] in ("A", "B", "C", "D"):
        return s[0]
    return ""


def _load_examples(max_examples: int | None) -> tuple[list[dict], str]:
    """Try Hugging Face → fall back to mock. Returns (examples, source)."""
    # Honor TUDOU_EXPERT_OFFLINE for a deterministic test path.
    import os
    if os.environ.get("TUDOU_EXPERT_OFFLINE", "0") == "1":
        return list(_MOCK_EXAMPLES)[:max_examples or len(_MOCK_EXAMPLES)], "mock"

    try:
        from datasets import load_dataset            # type: ignore
    except ImportError:
        return list(_MOCK_EXAMPLES)[:max_examples or len(_MOCK_EXAMPLES)], "mock"
    try:
        ds = load_dataset(HF_DATASET_PATH, split=HF_DATASET_SPLIT)
    except Exception:                                # noqa: BLE001
        # Network failure, gated dataset, schema mismatch, etc.
        return list(_MOCK_EXAMPLES)[:max_examples or len(_MOCK_EXAMPLES)], "mock"

    if max_examples is not None:
        try:
            ds = ds.select(range(min(max_examples, len(ds))))
        except Exception:                            # noqa: BLE001
            ds = ds[:max_examples]                   # type: ignore[index]

    examples: list[dict] = []
    for row in ds:
        # Best-effort schema normalization. Real LegalBench-zh shape varies
        # across uploads; the mock is the canonical shape.
        choices = row.get("choices")
        if isinstance(choices, list):
            choices = {ltr: txt for ltr, txt in zip("ABCD", choices)}
        if not isinstance(choices, dict):
            continue
        q = row.get("question") or row.get("prompt") or ""
        ans = (row.get("answer") or row.get("label") or "").strip().upper()
        if not q or ans not in ("A", "B", "C", "D"):
            continue
        examples.append({
            "id": str(row.get("id", len(examples))),
            "question": q,
            "choices": choices,
            "answer": ans,
        })
    if not examples:
        return list(_MOCK_EXAMPLES)[:max_examples or len(_MOCK_EXAMPLES)], "mock"
    return examples, "huggingface"


# ── runner ──

class LegalBenchZhRunner:
    """Multiple-choice runner over LegalBench-zh."""

    runner_id = RUNNER_ID

    def run(
        self,
        model: ModelCallable,
        *,
        max_examples: int | None = None,
        **_: Any,
    ) -> EvalReport:
        """Score `model` on LegalBench-zh.

        Args:
            model: callable taking a prompt string (and optional kwargs)
                   and returning a string answer.
            max_examples: cap the number of examples scored. None = all.

        Returns:
            EvalReport with score = accuracy in [0, 1] and
            metrics["source"] indicating whether real or mock data was
            used.
        """
        t0 = time.time()
        examples, source = _load_examples(max_examples)
        n = len(examples)
        if n == 0:
            return EvalReport(
                runner_id=RUNNER_ID,
                score=0.0,
                n_examples=0,
                n_correct=0,
                metrics={"source": source},
                duration_seconds=time.time() - t0,
                started_at=t0,
                succeeded=False,
                errors=["no examples available"],
            )

        correct = 0
        errors: list[str] = []
        for ex in examples:
            prompt = _format_prompt(ex)
            try:
                reply = model(prompt)
            except Exception as exc:                # noqa: BLE001
                errors.append(f"{ex.get('id')}: {type(exc).__name__}: {exc}")
                continue
            predicted = _parse_answer(reply)
            if predicted == ex["answer"]:
                correct += 1

        score = correct / n if n else 0.0
        return EvalReport(
            runner_id=RUNNER_ID,
            score=score,
            n_examples=n,
            n_correct=correct,
            metrics={"source": source},
            errors=errors,
            duration_seconds=time.time() - t0,
            started_at=t0,
            succeeded=True,
        )


# Auto-register when imported.
register(LegalBenchZhRunner())


__all__ = [
    "RUNNER_ID",
    "LegalBenchZhRunner",
]
