"""C: app/agent_runtime/ scaffold tests.

Locks the contract that:
  - The package imports CLEANLY without openai-agents installed
    (lazy SDK import — necessary so legacy A path stays runnable
    in environments that haven't installed the SDK yet)
  - SDKAgentRunner construction never touches the SDK (no I/O)
  - SDKAgentRunner.run() raises SDKNotInstalledError with a clear
    "pip install" hint when SDK is missing
  - All submodules import without side effects

Once openai-agents is actually installed (Phase 0 of migration),
additional tests will cover the actual SDK round-trip. For now,
PoC scaffold tests only.
"""
from __future__ import annotations

import pytest


def test_package_imports_without_sdk():
    """Bare ``import app.agent_runtime`` must not fail even when
    openai-agents is not installed."""
    import app.agent_runtime  # noqa: F401


def test_submodules_import():
    """All adapter submodules must be importable individually."""
    from app.agent_runtime import sdk_adapter  # noqa
    from app.agent_runtime import instructions_builder  # noqa
    from app.agent_runtime import tool_registry  # noqa
    from app.agent_runtime import event_bridge  # noqa
    from app.agent_runtime import hooks  # noqa


def test_is_sdk_available_returns_bool():
    """Must return False when SDK isn't installed (most CI/dev
    environments) — not raise."""
    from app.agent_runtime import is_sdk_available
    result = is_sdk_available()
    assert isinstance(result, bool)


def test_runner_construction_no_sdk_call():
    """Constructing SDKAgentRunner must not require SDK — only
    .run() does."""
    from app.agent_runtime import SDKAgentRunner

    class _FakeAgent:
        id = "x"
        name = "test"

    # Construction should never fail or import SDK
    runner = SDKAgentRunner(_FakeAgent())
    assert runner.tudou_agent.id == "x"


def test_run_raises_clear_error_when_sdk_missing():
    """When SDK isn't installed, .run() must raise
    SDKNotInstalledError with a pip-install hint, NOT a generic
    ImportError mid-execution."""
    from app.agent_runtime import (
        SDKAgentRunner, is_sdk_available, SDKNotInstalledError
    )

    if is_sdk_available():
        pytest.skip("SDK is installed — this test only runs without it")

    class _FakeAgent:
        id = "x"
        name = "test"

    runner = SDKAgentRunner(_FakeAgent())
    with pytest.raises(SDKNotInstalledError) as ei:
        runner.run("hello")
    # Error message must include the install instruction so users
    # know what to do.
    assert "pip install openai-agents" in str(ei.value)


# ── Nudge evaluator integration (B → C share path) ──

def test_evaluate_nudge_must_verify_path():
    """The nudge evaluator (B) is what the SDK adapter (C) calls in
    its on_llm_end hook. Lock the must-verify path that mimo's most
    common stall pattern triggers."""
    from app.runtime import evaluate_nudge
    import json

    nudge = evaluate_nudge(
        user_text="继续 terraform validate",
        agent_reply="修复完成。现在验证所有模块：",
        messages=[
            {"role": "user", "content": "继续 terraform validate"},
            {"role": "assistant", "tool_calls": [{"function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": "sed -i 's/x/y/' main.tf"})}}]},
            {"role": "tool", "content": "ok"},
        ],
        has_tools=True,
        iteration=0,
        max_iterations=10,
        nudge_count=0,
        max_nudges_per_turn=3,
    )
    assert nudge is not None
    assert nudge.kind == "must_verify"
    assert "验证" in nudge.text or "validate" in nudge.text.lower()


def test_evaluate_nudge_no_op_when_clean():
    """When the agent has no tools, evaluator returns None
    (universal gate)."""
    from app.runtime import evaluate_nudge

    nudge = evaluate_nudge(
        user_text="hello",
        agent_reply="hi there",
        messages=[],
        has_tools=False,  # universal gate
        iteration=0,
        max_iterations=10,
        nudge_count=0,
        max_nudges_per_turn=3,
    )
    assert nudge is None


def test_evaluate_nudge_respects_nudge_cap():
    """Past the per-turn nudge cap, evaluator returns None even if
    a stall would otherwise fire."""
    from app.runtime import evaluate_nudge

    nudge = evaluate_nudge(
        user_text="hi",
        agent_reply="Let me check:",  # would normally trigger stall
        messages=[],
        has_tools=True,
        iteration=0,
        max_iterations=10,
        nudge_count=99,  # over cap
        max_nudges_per_turn=3,
    )
    assert nudge is None


def test_evaluate_nudge_respects_iteration_cap():
    """On the last iteration, no nudge fires (no point — agent
    won't get another turn)."""
    from app.runtime import evaluate_nudge

    nudge = evaluate_nudge(
        user_text="hi",
        agent_reply="Let me check:",
        messages=[],
        has_tools=True,
        iteration=9,  # last iteration
        max_iterations=10,
        nudge_count=0,
        max_nudges_per_turn=3,
    )
    assert nudge is None


def test_evaluate_nudge_tool_error_path():
    """Tool errored + no continuation → tool_error_no_continuation
    nudge."""
    from app.runtime import evaluate_nudge

    nudge = evaluate_nudge(
        user_text="run x",
        agent_reply="I see there's an error",
        messages=[
            {"role": "user", "content": "run x"},
            {"role": "tool", "content": "Error: file not found"},
        ],
        has_tools=True,
        iteration=0,
        max_iterations=10,
        nudge_count=0,
        max_nudges_per_turn=3,
    )
    assert nudge is not None
    assert nudge.kind == "tool_error_no_continuation"


def test_evaluate_nudge_narrator_stall_path():
    """'Let me X:' style with no tool action → narrator_stall."""
    from app.runtime import evaluate_nudge

    nudge = evaluate_nudge(
        user_text="please help",
        agent_reply="Let me check the file:",
        messages=[],
        has_tools=True,
        iteration=0,
        max_iterations=10,
        nudge_count=0,
        max_nudges_per_turn=3,
    )
    assert nudge is not None
    assert nudge.kind == "narrator_stall"
