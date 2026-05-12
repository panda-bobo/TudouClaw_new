"""Tests for the per-tool guardrail halt fix (2026-05-12).

Real-world bug user reported: agent's mcp_call dispatch failed 8 times
this turn (legitimate underlying terraform errors at the time). The
`same_tool_halt` guardrail tripped → set `self._halt = decision`.

Then agent switched to a different tool (bash) — the guardrail's
``before_call`` returned the SAME halt because `self._halt is not None`
applied unconditionally. Result: bash tool_result said
"Halted: mcp_call has failed 8 times" — confusing, since bash itself
hadn't failed at all.

Fix: `self._halt` is now a per-tool dict. Only the tool that actually
tripped a halt is blocked; sibling tools remain free.
"""
from __future__ import annotations

import pytest

from app.agent_guardrails import (
    ToolCallGuardrailController,
    GuardrailConfig,
    GuardrailDecision,
)


# ── Halt is per-tool, not cross-tool ─────────────────────────────────

def test_mcp_call_halt_doesnt_block_bash():
    """The reported bug. mcp_call halts → bash should NOT inherit.

    Args vary each call so we trip Signal 2 (same_tool_halt) not
    Signal 1 (exact_failure_block) — Signal 2 is what the real
    incident triggered."""
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=4,   # tighter for test speed
        exact_failure_block_after=999,    # disable Signal 1 for this test
    ))
    # Drive mcp_call to the halt threshold (4 failures, varying args)
    for i in range(4):
        g.after_call("mcp_call", {"x": i}, "error: terraform failed",
                     failed=True)
    # Next mcp_call → halted
    mcp_decision = g.before_call("mcp_call", {"x": 99})
    assert mcp_decision.action == "halt"
    assert mcp_decision.code == "same_tool_halt"
    assert mcp_decision.tool_name == "mcp_call"

    # bash should be UNAFFECTED by mcp_call's halt
    bash_decision = g.before_call("bash", {"command": "ls"})
    assert bash_decision.action == "allow", (
        f"bash got blocked by mcp_call's halt: {bash_decision}")


def test_two_tools_can_halt_independently():
    """If two tools both hit their halt thresholds, each retains its
    own halt — accessing one doesn't return the other."""
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=3,
        exact_failure_block_after=999,
    ))
    # Halt mcp_call with varying args
    for i in range(3):
        g.after_call("mcp_call", {"x": i}, "error", failed=True)
    # Halt bash with varying args
    for i in range(3):
        g.after_call("bash", {"command": f"cmd{i}"}, "error", failed=True)

    d_mcp = g.before_call("mcp_call", {"x": 99})
    d_bash = g.before_call("bash", {"command": "new"})

    assert d_mcp.tool_name == "mcp_call"
    assert d_mcp.action == "halt"
    assert d_bash.tool_name == "bash"
    assert d_bash.action == "halt"
    assert d_mcp is not d_bash   # independent decision objects


def test_halt_for_returns_per_tool():
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=3,
        exact_failure_block_after=999,
    ))
    for i in range(3):
        g.after_call("read_file", {"path": f"/a{i}"}, "error", failed=True)
    # before_call must run to latch the halt
    g.before_call("read_file", {"path": "/x"})
    assert g.halt_for("read_file") is not None
    assert g.halt_for("bash") is None
    assert g.halt_for("never_called_tool") is None


def test_halt_decision_legacy_accessor_returns_any():
    """The legacy ``halt_decision`` property: returns SOME halt if any
    tool has one, None otherwise. Used by code that only wants to know
    "is anything halted at all" without specifying which tool."""
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=3,
    ))
    # Before any halts
    assert g.halt_decision is None

    # Halt one tool (varying args to skip Signal 1)
    for i in range(3):
        g.after_call("read_file", {"path": f"/a{i}"}, "error", failed=True)
    g.before_call("read_file", {"path": "/x"})   # triggers halt set
    d = g.halt_decision
    assert d is not None
    assert d.tool_name == "read_file"


# ── Existing single-tool behaviour still works ──────────────────────

def test_same_tool_halt_still_fires_on_repeat_failures():
    """Regression: 4 failures of one tool still trips the halt."""
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=4,
        exact_failure_block_after=999,
    ))
    for i in range(4):
        g.after_call("mcp_call", {"x": i}, "Error: failed", failed=True)
    d = g.before_call("mcp_call", {"x": 99})
    assert d.action == "halt"
    assert "mcp_call has failed 4 times" in d.message


def test_warnings_still_fire():
    """4-warn threshold (default same_tool_failure_warn_after=4):
    after 4 failures the warn precedes the halt at 8. Varying args
    so we hit Signal 2 (same_tool_warn) not Signal 1
    (exact_failure_warn at 2)."""
    g = ToolCallGuardrailController()  # use defaults
    for i in range(4):
        g.after_call("mcp_call", {"x": i}, "Error", failed=True)
    d = g.before_call("mcp_call", {"x": 99})
    assert d.action == "warn"
    assert d.code == "same_tool_warn"


def test_exact_failure_still_blocks():
    """Signal 1 (exact args repeat) still fires per-tool."""
    g = ToolCallGuardrailController(GuardrailConfig(
        exact_failure_block_after=3,
    ))
    args = {"command": "false"}
    for _ in range(3):
        g.after_call("bash", args, "Error: exit 1", failed=True)
    d = g.before_call("bash", args)
    assert d.action == "block"
    assert d.code == "exact_failure_block"
    # Bash NOT blocked for different args
    d2 = g.before_call("bash", {"command": "ls"})
    assert d2.action == "allow"


def test_reset_clears_all_halts():
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=3,
        exact_failure_block_after=999,
    ))
    for i in range(3):
        g.after_call("mcp_call", {"x": i}, "Error", failed=True)
    g.before_call("mcp_call", {"x": 99})   # latch halt via Signal 2
    assert g.halt_for("mcp_call") is not None
    g.reset()
    assert g.halt_for("mcp_call") is None
    assert g.halt_decision is None


# ── Successful calls don't trigger halt ─────────────────────────────

def test_successes_dont_count_toward_halt():
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=4,
    ))
    # 10 successful calls
    for _ in range(10):
        g.after_call("mcp_call", {"x": 1}, "ok result", failed=False)
    d = g.before_call("mcp_call", {"x": 1})
    assert d.action == "allow"


def test_mixed_success_and_failure():
    """4 failures + 4 successes — only failures count, halt at 4."""
    g = ToolCallGuardrailController(GuardrailConfig(
        same_tool_failure_halt_after=4,
    ))
    for i in range(8):
        result = "Error" if i % 2 == 0 else "ok"
        failed = (i % 2 == 0)
        g.after_call("mcp_call", {"x": i}, result, failed=failed)
    # 4 failures total → halt should fire on next before_call
    d = g.before_call("mcp_call", {"x": 99})
    assert d.action == "halt"
    assert d.count == 4
