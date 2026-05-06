"""Tool-call guardrail controller — TudouClaw port of Hermes' design.

Day 2 PM (2026-05-05).

Detects three loop signals that the inline signature_count in
``agent.py:8870`` doesn't catch:

  1. ``exact_failure``    — same (tool, args) failed N times
  2. ``same_tool_failure`` — same tool failed N times with DIFFERENT args
  3. ``no_progress``      — idempotent tool called N times without
                            mutating state in between

Returns ``ToolGuardrailDecision`` objects (action: allow / warn /
block / halt) — separation of detection from enforcement, so the
runtime decides whether to fail the call, inject a system message, or
halt the turn entirely.

Layered AFTER the existing inline signature_count guard in agent.py
— it catches what the older guard misses (esp. variant-args loops on
idempotent tools and "many fails on same tool but different params").
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger("tudouclaw.agent_guardrails")


# Tools whose result is purely a function of args + filesystem state.
# Calling them repeatedly with same args = wasted budget. Calling them
# many times with VARIANT args without writing in between = no progress.
IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file",
    "glob_files",
    "list_dir",
    "search_files",
    "grep_files",
    "knowledge_lookup",
    "memory_recall",
    "wiki_lookup",
    "web_search",
    "web_fetch",
    "browser_screenshot",
    "browser_get_text",
    "session_search",
})

# Tools that change state (write/exec/send). Repeated FAILS on these
# usually mean the agent doesn't know how to fix the problem.
MUTATING_TOOL_NAMES = frozenset({
    "write_file",
    "edit_file",
    "create_file",
    "delete_file",
    "patch",
    "bash",
    "shell",
    "execute_code",
    "run_python",
    "send_message",
    "send_email",
    "browser_click",
    "browser_type",
    "browser_navigate",
    "delegate_task",
    "todo_write",
    "skill_invoke",
})


@dataclass(frozen=True)
class GuardrailConfig:
    """Thresholds. Default values are 'conservative warn, opt-in block'
    so existing behavior doesn't regress when this is wired in."""
    warnings_enabled: bool = True
    hard_stop_enabled: bool = True
    # exact_failure: same (tool, args_hash) returned an error
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 4
    # same_tool_failure: tool name failed (any args) N times
    same_tool_failure_warn_after: int = 4
    same_tool_failure_halt_after: int = 8
    # no_progress: idempotent tool called N times without any mutating
    # tool succeeding in between
    no_progress_warn_after: int = 4
    no_progress_block_after: int = 7


@dataclass(frozen=True)
class GuardrailDecision:
    """Decision returned by the controller. Runtime owns enforcement."""
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"     # machine-readable reason code
    message: str = ""       # human-readable detail
    tool_name: str = ""
    count: int = 0

    @property
    def allows_execution(self) -> bool:
        return self.action in ("allow", "warn")

    @property
    def should_halt(self) -> bool:
        return self.action in ("block", "halt")


def _canonical_args(args: Mapping[str, Any] | None) -> str:
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except Exception:
        return str(args)


def _args_hash(args: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(_canonical_args(args).encode("utf-8", errors="replace")).hexdigest()[:16]


def _result_failed(tool_name: str, result: str | None) -> bool:
    """Heuristic: did this tool result indicate failure? Mirrors the
    classification used elsewhere in agent.py audit logging."""
    if result is None:
        return False
    if not isinstance(result, str):
        return False
    head = result.lstrip()[:200].lower()
    if head.startswith("error") or head.startswith("[error]"):
        return True
    if "[loop guard]" in head or "[read-valve-tripped]" in head:
        return True
    if '"error"' in head or '"failed"' in head:
        return True
    # Bash exit code != 0 surfaces as "[exit code: N]" in result
    if "exit code:" in head and "exit code: 0" not in head:
        return True
    return False


class ToolCallGuardrailController:
    """Per-turn loop detection. Reset per agent.chat() turn entry."""

    def __init__(self, config: GuardrailConfig | None = None):
        self.config = config or GuardrailConfig()
        self.reset()

    def reset(self) -> None:
        # (tool_name, args_hash) → fail count
        self._exact_fail: dict[tuple[str, str], int] = {}
        # tool_name → fail count (any args)
        self._tool_fail: dict[str, int] = {}
        # tool_name → call count since last successful mutation
        self._idempotent_runs: dict[str, int] = {}
        # latched halt (once tripped, every before_call returns it)
        self._halt: GuardrailDecision | None = None

    @property
    def halt_decision(self) -> GuardrailDecision | None:
        return self._halt

    def before_call(self, tool_name: str,
                    args: Mapping[str, Any] | None) -> GuardrailDecision:
        """Check whether to allow this tool call. Always called with the
        latest counters; never mutates them (that's after_call's job)."""
        if self._halt is not None:
            return self._halt

        ah = _args_hash(args)
        cfg = self.config

        # Signal 1: exact_failure
        ec = self._exact_fail.get((tool_name, ah), 0)
        if cfg.hard_stop_enabled and ec >= cfg.exact_failure_block_after:
            d = GuardrailDecision(
                action="block", code="exact_failure_block",
                tool_name=tool_name, count=ec,
                message=(f"Blocked {tool_name}: same (tool, args) failed "
                         f"{ec} times. Different args needed, or accept "
                         f"that this won't work and try a different "
                         f"approach."),
            )
            self._halt = d
            return d
        if cfg.warnings_enabled and ec >= cfg.exact_failure_warn_after:
            return GuardrailDecision(
                action="warn", code="exact_failure_warn",
                tool_name=tool_name, count=ec,
                message=(f"Warning: {tool_name} with same args has failed "
                         f"{ec} times. Next failure will be blocked."),
            )

        # Signal 2: same_tool_failure (any args)
        tc = self._tool_fail.get(tool_name, 0)
        if cfg.hard_stop_enabled and tc >= cfg.same_tool_failure_halt_after:
            d = GuardrailDecision(
                action="halt", code="same_tool_halt",
                tool_name=tool_name, count=tc,
                message=(f"Halted: {tool_name} has failed {tc} times this "
                         f"turn (with various args). Stop trying — fix "
                         f"the underlying issue or report you're stuck."),
            )
            self._halt = d
            return d
        if cfg.warnings_enabled and tc >= cfg.same_tool_failure_warn_after:
            return GuardrailDecision(
                action="warn", code="same_tool_warn",
                tool_name=tool_name, count=tc,
                message=(f"Warning: {tool_name} has failed {tc} times this "
                         f"turn. Reconsider the approach."),
            )

        # Signal 3: no_progress (idempotent without mutation between)
        if tool_name in IDEMPOTENT_TOOL_NAMES:
            np = self._idempotent_runs.get(tool_name, 0)
            if cfg.hard_stop_enabled and np >= cfg.no_progress_block_after:
                d = GuardrailDecision(
                    action="block", code="no_progress_block",
                    tool_name=tool_name, count=np,
                    message=(f"Blocked {tool_name}: called {np} times since "
                             f"last successful mutating action. You're "
                             f"stuck in research mode — call write_file, "
                             f"bash, or a status response now."),
                )
                self._halt = d
                return d
            if cfg.warnings_enabled and np >= cfg.no_progress_warn_after:
                return GuardrailDecision(
                    action="warn", code="no_progress_warn",
                    tool_name=tool_name, count=np,
                    message=(f"Warning: {tool_name} called {np}× without "
                             f"any mutation. Move from research to action."),
                )

        return GuardrailDecision(action="allow", tool_name=tool_name)

    def after_call(self, tool_name: str,
                   args: Mapping[str, Any] | None,
                   result: str | None,
                   failed: bool | None = None) -> None:
        """Update counters after a tool runs. Call EXACTLY once per
        actual tool invocation. ``failed`` overrides the heuristic if
        the runtime already classified the result."""
        if failed is None:
            failed = _result_failed(tool_name, result)
        ah = _args_hash(args)

        if failed:
            self._exact_fail[(tool_name, ah)] = self._exact_fail.get((tool_name, ah), 0) + 1
            self._tool_fail[tool_name] = self._tool_fail.get(tool_name, 0) + 1
        else:
            # Successful mutation resets the no_progress counter for ALL
            # idempotent tools — once you've written something, future
            # reads aren't suspect for a while.
            if tool_name in MUTATING_TOOL_NAMES:
                self._idempotent_runs.clear()

        if tool_name in IDEMPOTENT_TOOL_NAMES:
            self._idempotent_runs[tool_name] = self._idempotent_runs.get(tool_name, 0) + 1
