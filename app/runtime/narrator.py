"""Narrator-stall detector.

The LLM emits a "let me X:" / "我先 X：" sentence and stops, without
calling any tool. This pattern wastes a turn and frustrates users;
the chat loop nudges the agent ("you promised, now call the tool")
when this fires.

Pure function. Both legacy chat loop and SDK adapter call this on
the agent's reply text post-stream-end.

History: extracted from app/agent.py:1970-2136 on 2026-05-15.
"""
from __future__ import annotations


_NARRATOR_STALL_PATTERNS = (
    # English
    "let me ", "let's ", "i'll ", "i will ", "i am going to",
    "i'm going to", "now let me", "first, let me", "first let me",
    "next, i'll", "next i'll", "i am about to", "i'm about to",
    # Chinese
    "让我", "我来", "我将", "我会", "我要", "接下来", "马上", "现在我",
    "下面我", "我准备",
)


def looks_like_narrator_stall(text: str) -> bool:
    """True iff `text` looks like a promise-without-action ("Let me
    X:" style).

    Heuristic — both conditions must hold to keep false-positives low:
      1. Non-empty text that ends with ``:`` or ``：`` (the
         "commitment colon")
      2. The trailing line contains an intent phrase ("let me",
         "让我"...)

    A genuine answer that happens to end with a colon before a code
    block won't match unless it ALSO announces future work.
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if not (t.endswith(":") or t.endswith("：")):
        return False
    last_line = t.rsplit("\n", 1)[-1].lower()
    return any(p in last_line for p in _NARRATOR_STALL_PATTERNS)
