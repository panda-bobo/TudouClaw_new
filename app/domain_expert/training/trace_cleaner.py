"""Trace cleaner — sanitize raw Q/A traces before they become training data.

A "trace" is a dict captured by the agent reply pipeline (Phase 0 task 6 hook).
Minimum schema (all keys optional but typical):

    {
        "id":        str,            # unique trace id
        "agent_id":  str,
        "question":  str,            # user prompt
        "answer":    str,            # model reply
        "context":   list[dict],     # retrieved chunks (Track A)
        "citations": list[str],      # citation tokens like "[Doc#3]"
        "ts":        float,          # epoch seconds
        "score":     float,          # optional self/judge score 0..1
        "flags":     list[str],      # arbitrary qualitative tags
    }

Five cleaning rules, applied in this order by `clean()`:

    1. dedup            — drop traces whose (question, answer) pair already seen
    2. length_filter    — drop traces with too-short / too-long Q or A
    3. garbage_filter   — drop traces with garbage content (control chars,
                          obvious model placeholder strings)
    4. low_quality_flag — non-destructive: tag suspicious traces with
                          "low_quality" in `flags` so RAFT synth can downweight
    5. clean (orchestrator) — runs the four steps above, returns a tuple of
                              (cleaned_traces, report_dict)

The report dict counts how many traces each step removed/flagged so the
caller (and SP-2) can see what happened.

Pure stdlib. No I/O. Caller is responsible for persistence.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


# Tunable thresholds — kept conservative. SP-2 / Track D may override later.
DEFAULT_MIN_QUESTION_LEN = 4
DEFAULT_MIN_ANSWER_LEN = 8
DEFAULT_MAX_QUESTION_LEN = 4000
DEFAULT_MAX_ANSWER_LEN = 12000

# Common model-failure / placeholder fragments. Keep it small and
# unambiguous so we don't drop legitimate answers that happen to mention
# these phrases. All matched case-insensitively.
_GARBAGE_PATTERNS: tuple[str, ...] = (
    "[insert here]",
    "<your answer here>",
    "lorem ipsum",
    "todo:",
    "tk tk tk",
)

# Low-quality heuristics — non-destructive. Used by `low_quality_flag`.
_HEDGE_PHRASES: tuple[str, ...] = (
    "i don't know",
    "i'm not sure",
    "as an ai",
    "i cannot help",
    "我不知道",
    "无法回答",
    "作为ai",
)


# ── helpers ──

def _text(t: Any) -> str:
    """Coerce arbitrary trace field to a stripped string."""
    return (t or "").strip() if isinstance(t, str) else str(t or "").strip()


def _has_control_chars(s: str) -> bool:
    """True if the string contains control chars beyond \\t/\\n/\\r."""
    for ch in s:
        if ch in ("\t", "\n", "\r"):
            continue
        if unicodedata.category(ch).startswith("C"):
            return True
    return False


def _qa_fingerprint(trace: dict) -> str:
    """Stable hash of (normalized question, normalized answer)."""
    q = re.sub(r"\s+", " ", _text(trace.get("question"))).lower()
    a = re.sub(r"\s+", " ", _text(trace.get("answer"))).lower()
    return hashlib.sha1(f"{q}\x1f{a}".encode("utf-8")).hexdigest()


# ── rule 1: dedup ──

def dedup(traces: list[dict]) -> list[dict]:
    """Drop later occurrences of identical (question, answer) pairs.

    Identity = sha1 of normalized (lowercased + whitespace-collapsed) Q+A.
    Empty Q or A is *not* deduplicated here — length_filter handles that.
    Order is preserved; first occurrence wins.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        fp = _qa_fingerprint(t)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(t)
    return out


# ── rule 2: length filter ──

def length_filter(
    traces: list[dict],
    *,
    min_q: int = DEFAULT_MIN_QUESTION_LEN,
    min_a: int = DEFAULT_MIN_ANSWER_LEN,
    max_q: int = DEFAULT_MAX_QUESTION_LEN,
    max_a: int = DEFAULT_MAX_ANSWER_LEN,
) -> list[dict]:
    """Keep traces whose Q and A lengths are within bounds (inclusive).

    Length is character count after stripping. Missing/empty Q or A → drop.
    """
    out: list[dict] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        q = _text(t.get("question"))
        a = _text(t.get("answer"))
        if not q or not a:
            continue
        if len(q) < min_q or len(q) > max_q:
            continue
        if len(a) < min_a or len(a) > max_a:
            continue
        out.append(t)
    return out


# ── rule 3: garbage filter ──

def garbage_filter(traces: list[dict]) -> list[dict]:
    """Drop traces with control chars or known placeholder fragments."""
    out: list[dict] = []
    for t in traces:
        if not isinstance(t, dict):
            continue
        q = _text(t.get("question"))
        a = _text(t.get("answer"))
        if _has_control_chars(q) or _has_control_chars(a):
            continue
        haystack = (q + "\n" + a).lower()
        if any(p in haystack for p in _GARBAGE_PATTERNS):
            continue
        out.append(t)
    return out


# ── rule 4: low-quality flag (non-destructive) ──

def low_quality_flag(traces: list[dict]) -> list[dict]:
    """Tag traces as 'low_quality' in `flags` without removing them.

    Heuristics (any one triggers the flag):
        - Answer contains a hedge phrase ("i don't know", "as an ai", ...)
        - Answer is shorter than question (suspiciously curt)
        - score field is present and < 0.3
        - citations expected but answer references "[Doc#" yet no `citations` key

    Mutates a *copy* of each trace; the input list is not mutated.
    """
    out: list[dict] = []
    for t in traces:
        if not isinstance(t, dict):
            out.append(t)
            continue
        clone = dict(t)
        flags = list(clone.get("flags") or [])
        q = _text(clone.get("question"))
        a = _text(clone.get("answer"))
        a_lower = a.lower()

        suspicious = False
        if any(p in a_lower for p in _HEDGE_PHRASES):
            suspicious = True
        elif len(a) < len(q) and len(q) > 0:
            suspicious = True
        else:
            score = clone.get("score")
            if isinstance(score, (int, float)) and score < 0.3:
                suspicious = True
            elif "[Doc#" in a and not clone.get("citations"):
                suspicious = True

        if suspicious and "low_quality" not in flags:
            flags.append("low_quality")
        clone["flags"] = flags
        out.append(clone)
    return out


# ── rule 5: orchestrator ──

def clean(
    traces: list[dict],
    *,
    min_q: int = DEFAULT_MIN_QUESTION_LEN,
    min_a: int = DEFAULT_MIN_ANSWER_LEN,
    max_q: int = DEFAULT_MAX_QUESTION_LEN,
    max_a: int = DEFAULT_MAX_ANSWER_LEN,
) -> tuple[list[dict], dict]:
    """Run all 4 cleaning rules and return (cleaned_traces, report).

    Order matters:
        1. dedup            — cheap, drops obvious duplicates first
        2. length_filter    — drops empty/oversize before pattern matching
        3. garbage_filter   — drops obvious bad content
        4. low_quality_flag — tags remaining suspicious traces

    Report fields::
        {
            "input":             N,
            "after_dedup":       N,
            "after_length":      N,
            "after_garbage":     N,
            "output":            N,                # final count
            "dropped_dedup":     N,
            "dropped_length":    N,
            "dropped_garbage":   N,
            "flagged_low_quality": N,
        }
    """
    if not isinstance(traces, list):
        raise TypeError("clean() expects a list of trace dicts")

    n0 = len(traces)
    step1 = dedup(traces)
    n1 = len(step1)
    step2 = length_filter(step1, min_q=min_q, min_a=min_a,
                          max_q=max_q, max_a=max_a)
    n2 = len(step2)
    step3 = garbage_filter(step2)
    n3 = len(step3)
    step4 = low_quality_flag(step3)
    flagged = sum(
        1 for t in step4
        if isinstance(t, dict) and "low_quality" in (t.get("flags") or [])
    )

    report = {
        "input": n0,
        "after_dedup": n1,
        "after_length": n2,
        "after_garbage": n3,
        "output": len(step4),
        "dropped_dedup": n0 - n1,
        "dropped_length": n1 - n2,
        "dropped_garbage": n2 - n3,
        "flagged_low_quality": flagged,
    }
    return step4, report


__all__ = [
    "dedup",
    "length_filter",
    "garbage_filter",
    "low_quality_flag",
    "clean",
]
