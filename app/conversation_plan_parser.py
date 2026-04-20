"""Parse plan + step markers out of an agent's free-text replies.

Protocol the agent is asked to follow (see agent._build_static_system_prompt):

    📋 计划
    1. [做什么] — 工具: <tool_name>
    2. ...

    ✓ 第 N 步：<短句>

This module is pure: given assistant content text, return structured
results. No side effects, no DB access. Observer code in
``app.conversation_observer`` owns the ChatTask mutation.

Tolerant parsing
----------------
Weak / quantized open-source models often drift from strict format.
We handle:

  - 中文或英文 header ("📋 计划", "📋 Plan", "Plan:", "Here's my plan:")
  - numbered with "1." / "1、" / "Step 1"
  - "—"、"-"、":" as the tool separator
  - step completion markers written as "✓ 第 N 步" / "✓ step N" /
    "[x] step N" / trailing "(done)" / bullet "✅"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("tudou.conversation_plan_parser")


# ── Plan block detection ──────────────────────────────────────────────

# Header — accepts 📋 计划 / 📋 Plan / Plan: / 计划: / My plan: etc.
_PLAN_HEADER_RE = re.compile(
    r"(?:📋\s*)?(?:计划|Plan|my\s+plan|here(?:'|\u2019)?s\s+(?:the\s+|my\s+)?plan)\s*[:：]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Fenced code block containing a plan (common if agent wraps in ```).
_PLAN_FENCE_RE = re.compile(
    r"```(?:[a-z]*\n)?\s*(📋[^\n]*\n.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Numbered step line. Matches:
#   "1. foo" / "1、 foo" / "1) foo"
#   "Step 1: foo" / "Step 1. foo"
#   "第 1 步. foo" / "第1步: foo" / "第 1 步、 foo"
_STEP_LINE_RE = re.compile(
    r"""
    ^\s*
    (?:(?:Step|第)\s*)?              # optional English/CN step prefix
    (\d+)                             # the number
    \s*步?                            # optional trailing Chinese '步'
    [\.、\)\s:：]+                    # separator(s): . 、 ) whitespace : ：
    (.+?)                             # the goal body
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Extract tool hint: "... — 工具: web_search" / "... - tool: web_search".
_TOOL_HINT_RE = re.compile(
    r"(?:[—\-\u2014]\s*)(?:\u5de5\u5177|tool|tools|\u7528)\s*[:：]\s*`?([\w_]+)`?\s*$",
    re.IGNORECASE,
)


@dataclass
class ExtractedStep:
    id: str
    goal: str
    tool_hint: str = ""


def extract_plan(content: str) -> list[ExtractedStep]:
    """Return steps parsed from a 📋 Plan block, or [] if none found.

    Algorithm:
      1. Try fenced block first (most structured, least ambiguity).
      2. Else, locate a 📋/Plan header line, read the immediately-
         following numbered lines until a blank line or non-numbered
         line breaks the sequence.
    """
    if not content or not isinstance(content, str):
        return []

    text = content

    # Attempt #1: fenced code block
    for m in _PLAN_FENCE_RE.finditer(text):
        steps = _parse_numbered_lines(m.group(1))
        if steps:
            return steps

    # Attempt #2: inline header
    header_match = _PLAN_HEADER_RE.search(text)
    if not header_match:
        return []

    # Start scanning at the next line
    start = header_match.end()
    # Stop at the first blank line (two consecutive newlines) or a
    # markdown-rule-like separator, whichever comes first.
    stop_match = re.search(r"\n\s*\n|^---+\s*$", text[start:], re.MULTILINE)
    end = start + (stop_match.start() if stop_match else len(text))
    region = text[start:end]
    return _parse_numbered_lines(region)


def _parse_numbered_lines(region: str) -> list[ExtractedStep]:
    """Scan ``region`` line-by-line, accept consecutive numbered entries.
    Break on the first non-numbered line so we don't accidentally pick
    up unrelated bullets later in the message."""
    steps: list[ExtractedStep] = []
    expected = 1
    for line in region.splitlines():
        stripped = line.strip()
        if not stripped:
            if steps:  # blank line after steps begin = end of plan
                break
            continue
        m = _STEP_LINE_RE.match(stripped)
        if not m:
            if steps:  # non-numbered interruption ends the plan block
                break
            continue
        num = int(m.group(1))
        body = m.group(2).strip().rstrip(".,;；。").strip()
        # Extract tool hint if present; strip it out of the goal text.
        tool_hint = ""
        tm = _TOOL_HINT_RE.search(body)
        if tm:
            tool_hint = tm.group(1).strip()
            body = body[:tm.start()].rstrip(" —-、,.").strip()
        steps.append(ExtractedStep(
            id=f"s{num}",
            goal=body,
            tool_hint=tool_hint,
        ))
        expected += 1
    return steps


# ── Step completion marker detection ──────────────────────────────────


_STEP_DONE_MARKERS = [
    # "✓ 第 N 步" / "✓ step N" / "✓ step N completed"
    re.compile(
        r"[✓✔✅]\s*(?:第\s*)?(?:step\s*|Step\s*|\u7b2c\s*)?(\d+)\s*(?:\u6b65)?\s*[:：]?",
        re.IGNORECASE,
    ),
    # "[x] step N"
    re.compile(
        r"\[\s*x\s*\]\s*(?:step\s*)?(\d+)",
        re.IGNORECASE,
    ),
    # "Step N done/complete/finished"
    re.compile(
        r"(?:step\s*|\u7b2c\s*)(\d+)\s*(?:\u6b65)?\s*(?:done|complete(?:d)?|finished|\u5b8c\u6210)\b",
        re.IGNORECASE,
    ),
]


def find_completed_step_markers(content: str) -> list[int]:
    """Return the list of 1-based step numbers reported as completed
    in ``content``. Duplicates removed, order preserved."""
    if not content or not isinstance(content, str):
        return []
    seen: list[int] = []
    for pat in _STEP_DONE_MARKERS:
        for m in pat.finditer(content):
            try:
                n = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if n not in seen:
                seen.append(n)
    return seen
