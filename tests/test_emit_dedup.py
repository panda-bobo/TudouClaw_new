"""Regression tests for HANDOFF [B] — assistant-message dedup.

Locks in the behavior of ``app._emit_dedup.EmitDedupState`` so the
front-end ring-buffer at portal_bundle.js:4285 can eventually be
removed without regressing the 4×-bubble symptom from agent 小刚 on
2026-05-01.

Each test simulates a sequence of assistant-message emits as the chat
loop would generate them, and asserts how many actually pass through
to ``on_event``.
"""
from __future__ import annotations

import pytest

from app._emit_dedup import EmitDedupState, fingerprint


def test_fingerprint_strips_and_normalizes():
    head, full = fingerprint("  hello\n\nworld  ")
    assert full == "hello world"
    assert head == "hello world"


def test_fingerprint_empty():
    assert fingerprint("") == ("", "")
    assert fingerprint("   ") == ("", "")
    assert fingerprint(None) == ("", "")


def test_fingerprint_truncates_head_at_300():
    long = "x" * 500
    head, full = fingerprint(long)
    assert len(head) == 300
    assert len(full) == 500


def test_first_emit_passes():
    s = EmitDedupState()
    allow, age = s.should_emit_assistant("hello")
    assert allow is True
    assert age is None


def test_exact_repeat_suppressed():
    s = EmitDedupState()
    s.should_emit_assistant("任务已完成")
    allow, age = s.should_emit_assistant("任务已完成")
    assert allow is False
    assert age is not None and age >= 0


def test_streaming_chunk_then_final_suppressed():
    """The streamed text bubble appears first as 'X' (delta-built);
    backend later emits the final 'X plus more' message. Both should
    be considered the same logical reply."""
    s = EmitDedupState()
    s.should_emit_assistant("任务已完成")
    # Final-text upgrade — superset of the streamed prefix
    allow, _ = s.should_emit_assistant("任务已完成,流程图已就绪")
    assert allow is False


def test_final_then_streamed_chunk_also_suppressed():
    """Order-symmetric: a shorter prefix arriving AFTER a longer final
    is also a duplicate."""
    s = EmitDedupState()
    s.should_emit_assistant("任务已完成,流程图已就绪")
    allow, _ = s.should_emit_assistant("任务已完成")
    assert allow is False


def test_genuinely_different_passes():
    s = EmitDedupState()
    s.should_emit_assistant("好的,我先列计划")
    allow, _ = s.should_emit_assistant("接下来调用 drawio-skill")
    assert allow is True


def test_four_consecutive_identical_emits_one_passes():
    """The actual 2026-05-01 symptom — 4× of the same final text.
    Old single-slot dedup was vulnerable to non-consecutive repeats;
    the new ring catches all 4."""
    s = EmitDedupState()
    text = "任务已完成,流程图的源文件和预览图均已就绪。"
    results = [s.should_emit_assistant(text)[0] for _ in range(4)]
    # Exactly one pass, three suppressed
    assert results.count(True) == 1
    assert results.count(False) == 3


def test_non_consecutive_repeat_suppressed():
    """Reproduces the multi-iteration tool-loop pattern: agent emits
    the same wrap-up text in iter 1 and iter 3, with a tool-only
    iter 2 in between. The OLD single-slot dedup updated its key on
    iter 1 — but iter 2 didn't change the slot — and iter 3 matched.
    HOWEVER the user reported 4× duplicates which suggests something
    else cleared the slot. The new ring keeps 5 entries so iter 3,
    4, 5 all match iter 1."""
    s = EmitDedupState()
    s.should_emit_assistant("任务进行中,先调工具")
    # Simulate: tool_call fires, tool_result fires (these don't go
    # through should_emit_assistant — they're different event kinds).
    # Now iter 3 emits the same wrap-up text:
    allow, _ = s.should_emit_assistant("任务进行中,先调工具")
    assert allow is False


def test_empty_assistant_message_always_passes():
    """Empty assistant messages are sometimes used as turn markers —
    don't dedup them or downstream may miss the turn boundary."""
    s = EmitDedupState()
    s.should_emit_assistant("")
    allow, _ = s.should_emit_assistant("")
    assert allow is True   # both pass
    assert s.should_emit_assistant("real content")[0] is True


def test_ring_size_bounded_at_5():
    """6th unique message should pass; 1st should be evictable."""
    s = EmitDedupState(ring_size=5)
    for i in range(6):
        allow, _ = s.should_emit_assistant(f"unique message #{i}")
        assert allow is True, f"message {i} should pass"
    # Now ring contains messages 1..5. Message 0 is evicted → would pass again.
    allow, _ = s.should_emit_assistant("unique message #0")
    assert allow is True


def test_ttl_expiry_lets_old_repeats_pass():
    """Entries older than ttl_seconds are evicted on next call."""
    s = EmitDedupState(ttl_seconds=1.0)
    s.should_emit_assistant("hello", now=100.0)
    # 2s later, the entry has expired — same content passes again
    allow, _ = s.should_emit_assistant("hello", now=102.0)
    assert allow is True


def test_whitespace_collapsing_matches_extra_spaces():
    """Different internal whitespace runs should normalize to same
    fingerprint. Catches the 'reformat between iterations' case."""
    s = EmitDedupState()
    s.should_emit_assistant("hello world")
    allow, _ = s.should_emit_assistant("hello   world")
    assert allow is False
    allow, _ = s.should_emit_assistant("hello\n\nworld")
    assert allow is False


def test_4x_bubble_symptom_reproduction():
    """The exact 2026-05-01 reproduction. Mock the chat loop emit
    sequence: same final text emitted in 4 separate iterations
    (with hypothetical tool_call interleaving that doesn't reach
    this dedup helper).

    Acceptance: front-end receives EXACTLY one assistant bubble.
    """
    s = EmitDedupState()
    final = "任务已完成,流程图的源文件和预览图均已就绪,可以查看附件。"
    delivered_to_frontend = []
    for iteration in range(4):
        allow, _ = s.should_emit_assistant(final)
        if allow:
            delivered_to_frontend.append(final)
    assert len(delivered_to_frontend) == 1, (
        f"Expected 1 delivered bubble, got {len(delivered_to_frontend)}. "
        "If this fails, the 4x-bubble symptom is back — check the "
        "EmitDedupState ring/TTL settings in app/_emit_dedup.py."
    )


# ── HANDOFF [C] hook 3 — completion-claim detection ──────────────────


from app.qa_gate import (
    detect_completion_claim, validate_completion_claim, OK as _GATE_OK,
)
from app.agent_types import StepStatus, ExecutionStep, ExecutionPlan


def _make_plan(*step_statuses):
    """Helper: build a minimal ExecutionPlan with the given step statuses."""
    steps = []
    for i, s in enumerate(step_statuses):
        st = ExecutionStep(id=f"s{i}", title=f"step {i}")
        st.status = s
        steps.append(st)
    plan = ExecutionPlan(id="p1", task_summary="test plan")
    plan.steps = steps
    plan.status = "active"
    return plan


def test_detect_completion_claim_zh():
    assert detect_completion_claim("任务已完成,准备交付")
    assert detect_completion_claim("好的,搞定了。")
    assert detect_completion_claim("我已经完成了所有步骤")


def test_detect_completion_claim_en():
    assert detect_completion_claim("Task is complete. ready for review")
    assert detect_completion_claim("All done — see attached")
    assert detect_completion_claim("I have completed the analysis")


def test_detect_completion_claim_negative():
    assert not detect_completion_claim("")
    assert not detect_completion_claim("ok")  # too short
    assert not detect_completion_claim("Let me start by exploring the code")
    assert not detect_completion_claim("I need to think about this more")


def test_validate_completion_claim_no_plan():
    """No plan → claim is fine (we can't second-guess it)."""
    r = validate_completion_claim("任务已完成", None)
    assert r.ok


def test_validate_completion_claim_plan_done():
    """Plan exists but all steps already done → claim is consistent."""
    plan = _make_plan(StepStatus.COMPLETED, StepStatus.COMPLETED)
    r = validate_completion_claim("All done!", plan)
    assert r.ok


def test_validate_completion_claim_no_claim():
    """No claim made → always passes regardless of plan state."""
    plan = _make_plan(StepStatus.PENDING, StepStatus.IN_PROGRESS)
    r = validate_completion_claim("Let me work on the next step", plan)
    assert r.ok


def test_validate_completion_claim_mismatch():
    """Claim made but plan has open steps → flagged."""
    plan = _make_plan(StepStatus.COMPLETED, StepStatus.PENDING)
    r = validate_completion_claim("任务已完成,可以交付了", plan)
    assert not r.ok
    assert "1 open step" in r.reason


def test_validate_completion_claim_truncates_step_list():
    """Many open steps → reason mentions only first 3 + '...'"""
    plan = _make_plan(*([StepStatus.PENDING] * 5))
    r = validate_completion_claim("All done!", plan)
    assert not r.ok
    assert "5 open step" in r.reason
    assert r.reason.endswith("...")
