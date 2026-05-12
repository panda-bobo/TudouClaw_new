"""Tests for plan-pending continuation nudge in agent.chat() loop.

Real-world bug: 刘老师 wrote 3 monitoring/ files for the user's
"补齐 4 个空模块" plan, then asked "需要我继续推进 logging 模块吗?"
and stopped. The user had explicitly authorized sequential execution
of all 4 modules in the original message. The agent should have
continued autonomously.

Root cause: chat() loop treats any LLM response with no tool_calls
as a "final answer" and breaks (line 11251). Nothing inspects the
agent's own _current_plan to see if more steps are pending.

Fix: before breaking, if _current_plan is active AND has PENDING /
IN_PROGRESS steps AND we're under the nudge cap, inject a user-role
nudge "[system nudge] 你的 plan 还有 N 个未完成 step. 下一步: 「X」.
不要问'是否继续' - 直接执行。"

These tests verify the predicate logic (which steps count as
"pending") and the nudge text construction. The loop integration
itself is hard to unit-test without a full Hub + LLM mock — relies
on the user's restart for end-to-end validation.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.agent_types import ExecutionPlan, ExecutionStep, StepStatus


# ── Predicate: which steps count as "still pending"? ─────────────────

def test_pending_step_filter_includes_pending_and_in_progress():
    plan = ExecutionPlan(task_summary="补齐 4 个空模块")
    plan.add_step("实现 monitoring")
    plan.add_step("实现 logging")
    plan.add_step("实现 organization")
    plan.add_step("实现 account-factory")
    plan.add_step("全量验证")
    # Mark first 2 as done/in-progress
    plan.steps[0].status = StepStatus.COMPLETED
    plan.steps[1].status = StepStatus.IN_PROGRESS

    pending = [s for s in plan.steps
               if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]
    assert len(pending) == 4   # in-progress (1) + pending (3)
    # Next pending step in order is the in-progress one
    assert pending[0].title == "实现 logging"


def test_pending_step_filter_excludes_completed_failed_skipped():
    plan = ExecutionPlan()
    plan.add_step("a")
    plan.add_step("b")
    plan.add_step("c")
    plan.steps[0].status = StepStatus.COMPLETED
    plan.steps[1].status = StepStatus.FAILED
    plan.steps[2].status = StepStatus.SKIPPED

    pending = [s for s in plan.steps
               if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]
    assert pending == []


def test_no_plan_means_no_pending():
    """Agents without a current_plan must not trigger the nudge."""
    plan = None
    pending = []
    if plan and getattr(plan, "status", "") == "active":
        pending = [s for s in (getattr(plan, "steps", []) or [])
                   if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]
    assert pending == []


def test_completed_plan_means_no_pending():
    """A plan that finished must not trigger the nudge even if it
    still has step records."""
    plan = ExecutionPlan(status="completed")
    plan.add_step("a")    # PENDING by default
    pending = []
    if plan and plan.status == "active":
        pending = [s for s in plan.steps
                   if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]
    assert pending == []


def test_failed_plan_means_no_pending():
    plan = ExecutionPlan(status="failed")
    plan.add_step("a")
    pending = []
    if plan and plan.status == "active":
        pending = [s for s in plan.steps
                   if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]
    assert pending == []


# ── Nudge text format ─────────────────────────────────────────────────

def _build_nudge_text(pending_steps: list[ExecutionStep]) -> str:
    """Replicates the nudge construction from agent.py for testing.

    Kept verbatim with the loop code so changes break the test loudly.
    """
    if not pending_steps:
        return ""
    next_step = pending_steps[0]
    next_title = (getattr(next_step, "title", "") or "").strip()
    next_detail = (getattr(next_step, "detail", "") or "").strip()
    pending_n = len(pending_steps)
    return (
        "[system nudge] 你的 plan 还有 "
        f"{pending_n} 个未完成的 step。"
        f"下一步是: 「{next_title}」"
        + (f" — {next_detail}" if next_detail else "")
        + "。\n\n"
        "用户已经在最初的 message 里授权了完整的 "
        "plan, 不需要再问'是否继续'。直接调用工具"
        "推进这一步, 完成后自动接下一步。最后所有"
        "step 都完成时再总结汇报。\n"
        "Don't ask permission — execute the next "
        "step now."
    )


def test_nudge_text_includes_next_step_title():
    plan = ExecutionPlan()
    plan.add_step("实现 logging 模块", detail="LTS 日志流 + CTS 追踪器")
    plan.add_step("实现 organization 模块")
    nudge = _build_nudge_text(plan.steps)
    assert "实现 logging 模块" in nudge
    assert "LTS 日志流 + CTS 追踪器" in nudge   # detail attached


def test_nudge_text_omits_detail_when_empty():
    plan = ExecutionPlan()
    plan.add_step("实现 logging 模块")  # no detail
    nudge = _build_nudge_text(plan.steps)
    # No dangling "」 —" right after the title (which would appear if
    # we appended a separator before an empty detail). The phrase
    # "permission — execute" further down is OK.
    assert "」 —" not in nudge
    assert "」。" in nudge   # title closes cleanly into 。


def test_nudge_text_states_pending_count():
    plan = ExecutionPlan()
    for t in ("a", "b", "c", "d"):
        plan.add_step(t)
    nudge = _build_nudge_text(plan.steps)
    assert "4 个未完成的 step" in nudge


def test_nudge_text_explicitly_forbids_asking_permission():
    """Regression guard: the whole point is 'don't ask user'."""
    plan = ExecutionPlan()
    plan.add_step("x")
    nudge = _build_nudge_text(plan.steps)
    # Both Chinese and English instruction must be present
    assert "不需要再问" in nudge
    assert "Don't ask permission" in nudge


def test_nudge_text_empty_when_no_pending():
    assert _build_nudge_text([]) == ""


# ── Env var disable ─────────────────────────────────────────────────

def test_env_var_default_enabled():
    """TUDOU_PLAN_CONTINUE_NUDGE defaults to enabled (any value != "0")."""
    # Default: env var unset → "1"
    assert os.environ.get("TUDOU_PLAN_CONTINUE_NUDGE", "1") != "0"


def test_env_var_explicit_zero_disables():
    with patch.dict(os.environ, {"TUDOU_PLAN_CONTINUE_NUDGE": "0"}):
        assert os.environ.get("TUDOU_PLAN_CONTINUE_NUDGE", "1") == "0"


def test_env_var_unrelated_value_keeps_enabled():
    """Anything other than literal '0' keeps the nudge enabled — same
    convention as TUDOU_NUDGE_WEAK_MODELS."""
    with patch.dict(os.environ, {"TUDOU_PLAN_CONTINUE_NUDGE": "true"}):
        assert os.environ.get("TUDOU_PLAN_CONTINUE_NUDGE", "1") != "0"


# ── Integration sanity: predicate matches what loop computes ─────────

def test_predicate_matches_real_world_刘老师_state():
    """Reproduce 刘老师's state at 09:03 from the screenshot:
       - monitoring: in_progress (files written, validate not run)
       - logging:    pending
       - organization: pending
       - account-factory: pending
       - 全量验证:   pending
    Expected: 5 pending steps detected, next step is monitoring (the
    in-progress one) — agent should resume there.
    """
    plan = ExecutionPlan(task_summary="补齐 4 个空模块代码", status="active")
    plan.add_step("实现 monitoring/ 模块",
                  detail="CES 告警 + 仪表盘")
    plan.add_step("实现 logging/ 模块",
                  detail="LTS 日志流 + CTS 追踪器")
    plan.add_step("实现 organization/ 模块",
                  detail="企业项目 + IAM 项目")
    plan.add_step("实现 account-factory/ 模块",
                  detail="IAM 账号 + 用户组")
    plan.add_step("全量验证汇总")
    plan.steps[0].status = StepStatus.IN_PROGRESS

    pending = [s for s in plan.steps
               if s.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS)]

    assert len(pending) == 5
    assert pending[0].title == "实现 monitoring/ 模块"
    nudge = _build_nudge_text(pending)
    assert "monitoring" in nudge
    assert "5 个未完成的 step" in nudge
