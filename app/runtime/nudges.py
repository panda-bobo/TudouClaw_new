"""Nudge-related pure helpers.

Distinct from the nudge INJECTION (which lives in the chat loop and
needs Agent state). These are the **detection** / **classification**
helpers that BOTH the legacy chat loop AND the future SDK adapter
call to decide whether a nudge should fire:

    agent_claimed_completion(text) -> bool
    agent_ran_verification_this_turn(messages) -> bool
    detect_recent_tool_error(messages) -> str | None

Conservative-by-design: False / None means "don't nudge" — always
the safer default than a false positive that loops the agent.

History: extracted from app/agent.py:1981-2114 on 2026-05-15. The
constants (_TOOL_ERROR_MARKERS, _COMPLETION_CLAIM_PATTERNS,
_VERIFY_TOOL_HINTS) move with the functions; behavior unchanged.
"""
from __future__ import annotations

import json as _json


# ── Tool-error detection ────────────────────────────────────────────

TOOL_ERROR_MARKERS = (
    "Error:", "ERROR:", "error:", "Failed", "FAILED",
    "Traceback", "Exception:", "❌",
    "exit code: 1", "exit code: 2", "exit code: 3",
    "non-zero exit", "non zero exit",
    "Permission denied", "command not found",
    "Validation failed", "validation failed",
    "Errno", "[ERROR]",
    # Terraform-specific
    "Error: ", "│ Error:",
    # Python-specific
    "ModuleNotFoundError", "AttributeError", "TypeError", "NameError",
)


def detect_recent_tool_error(messages: list[dict]) -> str | None:
    """Scan ``messages`` backward. If the most recent ``role=='tool'``
    message in the current turn (since the last user msg) carries an
    error signal in its first ~1500 chars, return a brief description
    line; else None.

    Used by chat() loop / SDK RunHooks to decide whether to inject a
    "tool errored, you stopped — keep going or explicitly bail" nudge
    for weak planners that don't loop on their own.
    """
    for m in reversed(messages):
        role = m.get("role")
        if role == "user":
            return None  # crossed turn boundary, no recent tool error
        if role != "tool":
            continue
        c = m.get("content") or ""
        if not isinstance(c, str):
            continue
        head = c[:1500]
        for marker in TOOL_ERROR_MARKERS:
            if marker in head:
                # Return the line containing the marker for the nudge.
                for line in head.splitlines():
                    if marker in line:
                        return line.strip()[:200]
                return marker
    return None


# ── Completion-claim + verification-run detection (must-verify nudge) ─

COMPLETION_CLAIM_PATTERNS = (
    "修复完成", "全部修好", "全部修复", "已完成", "已修好", "已修复",
    "全部完成", "搞定了", "弄好了", "修好了", "改完了",
    "all fixed", "all done", "all set", "completed", "finished",
    "done!", "done.", "fixed all", "all good",
)

VERIFY_TOOL_HINTS = (
    "validate", "verify", "test", "lint", "check",
    "tflint", "terraform plan", "terraform apply",
    "pytest", "npm test", "jest", "mypy", "go test", "cargo test",
)


def agent_claimed_completion(agent_text: str) -> bool:
    """True iff agent's reply contains a completion-claim phrase."""
    if not agent_text or not isinstance(agent_text, str):
        return False
    txt_lower = agent_text.lower()
    return any(p in agent_text for p in COMPLETION_CLAIM_PATTERNS) \
        or any(p in txt_lower for p in COMPLETION_CLAIM_PATTERNS)


def agent_ran_verification_this_turn(messages: list[dict]) -> bool:
    """Walk messages backward from end. Stop at last user message
    (turn boundary). Look for any tool_call whose name OR bash command
    matches a verification hint (validate / test / lint / etc.).
    """
    for m in reversed(messages):
        if m.get("role") == "user":
            return False  # crossed turn boundary, no verify call
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            n = (fn.get("name") or "").lower()
            # Direct verify tool
            if any(h in n for h in VERIFY_TOOL_HINTS):
                return True
            # bash command containing verify pattern
            if n == "bash":
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = (_json.loads(args_raw)
                            if isinstance(args_raw, str) else args_raw)
                    cmd = str((args or {}).get("command") or "").lower()
                    if any(h in cmd for h in VERIFY_TOOL_HINTS):
                        return True
                except Exception:
                    pass
    return False
