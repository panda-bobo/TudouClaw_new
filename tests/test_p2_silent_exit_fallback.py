"""P2 — silent-exit fallback (BaiLongma's send_message backstop pattern,
2026-05-13).

Today's session had 5+ "agent 没继续推进 / 没执行啥就直接结束了"
frustrations. Root cause: agent.chat() can return empty
final_content if the model emits no final reply (hits cap / errors /
exits silently). User sees nothing.

BaiLongma fixes this by making send_message a TOOL — model emits
the reply via send_message(content, ...), and runtime checks at
exit "did model call send_message? If not, synthesize one."

Our pattern (similar): right before chat() returns final_content,
if it's empty, call _synthesize_silent_exit_reply() to craft a
short status message based on what we know:
  1. Plan-pending: "did N steps, X pending, next: Y. 回复继续 to resume"
  2. Recent tool error: "hit error: <quote>. 告诉我下一步"
  3. Default: "本轮处理结束 — 没有新内容回复"

The synthesized reply is appended as a runtime_fallback assistant
message + emitted via on_event so SSE clients see it.
"""
from __future__ import annotations

import pytest

from app.agent import Agent
from app.agent_types import (
    ExecutionPlan, ExecutionStep, StepStatus,
)


@pytest.fixture
def agent():
    return Agent(id="t-p2", name="t")


# ── Default fallback ─────────────────────────────────────────────

def test_no_plan_no_errors_returns_generic(agent):
    """No plan + no recent errors → generic 'nothing to add' message."""
    result = agent._synthesize_silent_exit_reply()
    assert result
    assert "本轮处理结束" in result or "没有新内容" in result


# ── Plan-pending fallback ────────────────────────────────────────

def test_active_plan_with_pending_steps_synthesizes_progress(agent):
    plan = ExecutionPlan(task_summary="补齐 4 个空模块", status="active")
    plan.add_step("实现 monitoring 模块")
    plan.add_step("实现 logging 模块")
    plan.add_step("实现 organization 模块")
    plan.steps[0].status = StepStatus.COMPLETED
    plan.steps[1].status = StepStatus.IN_PROGRESS
    agent._current_plan = plan

    result = agent._synthesize_silent_exit_reply()

    # Mentions progress (1/3 done)
    assert "1/3" in result or "1 步" in result.replace("/", " ")
    # Mentions next step
    assert "logging" in result
    # Suggests resume
    assert "继续" in result


def test_completed_plan_does_not_trigger_progress_msg(agent):
    """All steps done → fall through to generic fallback."""
    plan = ExecutionPlan(status="completed")
    plan.add_step("a")
    plan.steps[0].status = StepStatus.COMPLETED
    agent._current_plan = plan
    result = agent._synthesize_silent_exit_reply()
    # No structured "**下一步**" header (only the generic mention is OK)
    assert "**下一步**" not in result
    # Falls to generic
    assert "本轮处理结束" in result or "没有新内容" in result


def test_inactive_plan_status_skipped(agent):
    """plan.status='interrupted' or 'failed' → not treated as actionable."""
    plan = ExecutionPlan(status="interrupted")
    plan.add_step("a")
    agent._current_plan = plan
    result = agent._synthesize_silent_exit_reply()
    assert "**下一步**" not in result


def test_single_pending_no_done_skipped(agent):
    """1 pending step + 0 done + only 1 step total → not enough
    progress to surface; falls through. Avoids over-claiming
    'progress' on tiny plans."""
    plan = ExecutionPlan(status="active")
    plan.add_step("only step")
    plan.steps[0].status = StepStatus.PENDING
    agent._current_plan = plan
    result = agent._synthesize_silent_exit_reply()
    # Either generic or progress is OK as long as something coherent
    assert result.strip()


# ── Tool-error fallback ──────────────────────────────────────────

def test_recent_tool_error_surfaces_in_fallback(agent):
    """Last tool message in messages was an error → quote it."""
    agent.messages = [
        {"role": "user", "content": "do X"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1",
         "content": "Error: command not found: terraform"},
    ]
    result = agent._synthesize_silent_exit_reply()
    assert "工具错误" in result or "error" in result.lower()
    assert "terraform" in result


def test_successful_tool_does_not_trigger_error_path(agent):
    agent.messages = [
        {"role": "user", "content": "do X"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1",
         "content": "ok output here"},
    ]
    result = agent._synthesize_silent_exit_reply()
    # Should NOT mention error
    assert "工具错误" not in result
    # Falls to generic
    assert "本轮处理结束" in result or "没有新内容" in result


# ── Priority order: plan beats error ─────────────────────────────

def test_plan_pending_takes_priority_over_recent_error(agent):
    """When BOTH plan-pending AND recent error are present, plan wins
    (more actionable than just 'we hit an error')."""
    plan = ExecutionPlan(status="active")
    plan.add_step("step 1")
    plan.add_step("step 2")
    plan.steps[0].status = StepStatus.COMPLETED
    plan.steps[1].status = StepStatus.PENDING
    agent._current_plan = plan
    agent.messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "tc1", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1",
         "content": "Error: oops"},
    ]
    result = agent._synthesize_silent_exit_reply()
    # Plan-progress wording present (priority 1)
    assert "step 2" in result or "下一步" in result
    # Error wording absent (de-prioritized)
    assert "工具错误" not in result


# ── Robustness ────────────────────────────────────────────────────

def test_no_messages_no_plan_returns_something(agent):
    """Brand new agent with no state → still returns a message."""
    agent.messages = []
    result = agent._synthesize_silent_exit_reply()
    assert result.strip()


def test_corrupted_plan_does_not_crash(agent):
    """Bad plan attribute won't take down the fallback."""
    class BrokenPlan:
        status = "active"
        # Missing .steps
    agent._current_plan = BrokenPlan()
    result = agent._synthesize_silent_exit_reply()
    assert result.strip()   # falls through to generic
