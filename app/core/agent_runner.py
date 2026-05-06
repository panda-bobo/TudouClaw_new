"""Agent invocation helpers — Phase 2 (2026-05-06).

Shared logic for running ``agent.chat()`` in non-interactive contexts
(cron jobs, channel handlers, project workflow steps) where the
Day 2 AM per-response cap may force the agent to emit a status JSON
mid-task. These contexts want EITHER:

  (a) "auto-continue" — feed a "continue" prompt back in as long as
      the agent reports unfinished work, OR
  (b) "show final only" — hide intermediate status JSONs from the user
      and only deliver the final natural-language response.

This module centralizes both. Without it each adapter would have to
re-implement status parsing.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger("tudouclaw.core.agent_runner")


# Status JSON shape produced by the Day 2 AM per-response cap.
# Tolerant parser — model output may be wrapped in code fences or
# prefixed by extra text.
_STATUS_KEYS = ("done", "current", "next", "budget_remaining", "blocked_by")


def parse_status(text: str) -> Optional[dict]:
    """Try to extract the per-response status JSON from an agent
    response. Returns the parsed dict, or None if the response is a
    normal final answer (no status block).
    """
    if not text or not isinstance(text, str):
        return None
    # Strip ``` code fences
    cleaned = text.strip()
    fence = re.match(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    # Find the first {...} JSON-looking blob on the leading lines
    first_brace = cleaned.find("{")
    if first_brace < 0:
        return None
    snippet = cleaned[first_brace:first_brace + 4096]  # cap scan
    # Greedy-match a balanced object — cheap heuristic
    depth = 0
    end = -1
    for i, ch in enumerate(snippet):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= 0:
        return None
    try:
        obj = json.loads(snippet[:end])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    # Must have at least 2 of the canonical keys to count
    hits = sum(1 for k in _STATUS_KEYS if k in obj)
    if hits < 2:
        return None
    return obj


def is_final_response(text: str) -> bool:
    """True when ``text`` is a normal natural-language answer (NOT an
    intermediate per-response-cap status JSON)."""
    return parse_status(text) is None


def render_status_for_user(text: str) -> str:
    """Strip the leading status JSON from a response so the human-readable
    summary is what the user sees. If there's no status, returns text
    unchanged. If there's only a status (no human text), returns a
    one-line digest."""
    status = parse_status(text)
    if status is None:
        return text
    cleaned = text.strip()
    # Skip past the JSON block
    fence = re.match(r"(```(?:json)?\s*)(.+?)(\s*```)", cleaned, re.DOTALL)
    if fence:
        rest = cleaned[fence.end():].strip()
    else:
        first_brace = cleaned.find("{")
        depth = 0
        end = -1
        for i in range(first_brace, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        rest = cleaned[end:].strip() if end > 0 else ""
    if rest:
        return rest
    # No human summary — synthesize from status fields.
    cur = status.get("current") or "(unspecified)"
    nxt = status.get("next") or "(none)"
    blocked = status.get("blocked_by") or ""
    if blocked:
        return f"⏸ Paused: {blocked}. (was: {cur})"
    return f"… still working on: {cur} → next: {nxt}"


def run_with_auto_continue(
    chat_fn: Callable[[str], str],
    initial_prompt: str,
    *,
    max_rounds: int = 6,
    on_status: Optional[Callable[[dict, int], None]] = None,
    log_label: str = "agent",
) -> str:
    """Call ``chat_fn(prompt)`` repeatedly when it returns a
    per-response-cap status JSON with no blocker, until the agent
    produces a final natural-language response, ``blocked_by`` is
    set, or ``max_rounds`` is reached.

    Returns the final response (always natural language; intermediate
    status JSONs are stripped). Total round count surfaced via
    ``on_status`` callback if provided.
    """
    prompt = initial_prompt
    last_response = ""
    for round_idx in range(max_rounds):
        try:
            resp = chat_fn(prompt)
        except Exception as e:
            logger.warning("%s auto-continue round %d failed: %s",
                           log_label, round_idx, e)
            raise
        if not isinstance(resp, str):
            return str(resp)
        last_response = resp
        status = parse_status(resp)
        if status is None:
            # Final answer
            return resp
        if on_status:
            try:
                on_status(status, round_idx)
            except Exception:
                pass
        blocked = status.get("blocked_by") or ""
        if blocked:
            logger.info("%s auto-continue stopped — blocked_by=%s",
                        log_label, blocked)
            return render_status_for_user(resp)
        budget = status.get("budget_remaining")
        if isinstance(budget, int) and budget <= 0:
            logger.info("%s auto-continue stopped — budget exhausted",
                        log_label)
            return render_status_for_user(resp)
        # Issue continuation prompt
        nxt = status.get("next") or ""
        prompt = (
            "[auto-continue] Previous response capped at the per-response "
            f"tool budget. You said next step is: {nxt!r}. "
            "Continue working — same task, no need to re-describe context."
        )
    logger.warning("%s auto-continue hit max_rounds=%d, returning last "
                   "response as-is", log_label, max_rounds)
    return render_status_for_user(last_response)
