"""End-to-end integration tests for the full 6-phase TaskLoop.

These tests exercise the *closed-loop business flow*: a Task submitted
by an AgentV2 must pass through Intake → Plan → Execute → Verify →
Deliver → Report → DONE without external dependencies. The LLM, skill
registry, MCP manager, and SQLite store are all faked.

Three scenarios:
    1. happy_path_conversation — simplest template, no tools, no rules
    2. happy_path_with_tool_and_file — step calls a skill that creates
       a file; verify rule asserts file presence; deliver records receipt
    3. verify_failure_then_soft_fail — verify rule fails twice in a row;
       task.finished_reason='verify', task.status=FAILED, Report still
       runs and writes the final assistant message (invariant G4)
"""
from __future__ import annotations

import json
import types
from typing import Callable

import pytest

from app.v2.core.task import Task, TaskPhase, TaskStatus
from app.v2.core.task_loop import TaskLoop


# ── fakes ─────────────────────────────────────────────────────────────


class FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, task_id, phase, event_type, payload):
        self.events.append({
            "task_id": task_id,
            "phase": phase.value if hasattr(phase, "value") else phase,
            "type": event_type,
            "payload": dict(payload or {}),
        })

    def flush_and_close(self, task_id=None):
        pass

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


class FakeStore:
    def __init__(self):
        self.saves = 0

    def save(self, task):
        self.saves += 1


class FakeAgent:
    def __init__(self):
        self.id = "av2_test"
        self.capabilities = types.SimpleNamespace(llm_tier="default")


# ── LLM router: dispatches based on system-prompt content ─────────────


class LLMRouter:
    """Routes calls by inspecting the system prompt: each TaskLoop /
    TaskExecutor call site uses a distinct system message fingerprint.
    Unknown fingerprint → silent assistant so tests don't hang."""

    SIG_INTAKE  = "任务预处理助手"
    SIG_PLAN    = "任务规划器"
    SIG_REPORT  = "任务汇报助手"
    SIG_STEP    = "当前 step"   # lives in the STEP-framing system msg

    def __init__(
        self,
        *,
        intake: dict | None = None,
        plan: dict | None = None,
        step_responses: list[dict] | None = None,
        report_text: str = "✅ 报告已生成",
    ):
        self.intake = intake or {"filled": {}, "missing": [], "clarification": ""}
        self.plan = plan or {
            "steps": [{"id": "s1", "goal": "reply", "tools_hint": [],
                       "exit_check": {}}],
            "expected_artifact_count": 0,
        }
        self.step_queue = list(step_responses or [
            {"role": "assistant", "content": "done", "tool_calls": []},
        ])
        self.report_text = report_text
        self.call_log: list[str] = []

    def __call__(self, *, messages, tools=None, tier="default", max_tokens=4096):
        sigs = " ".join(
            (m.get("content") or "") for m in messages
            if m.get("role") == "system"
        )
        if self.SIG_INTAKE in sigs:
            self.call_log.append("intake")
            return {
                "role": "assistant",
                "content": "```json\n" + json.dumps(self.intake, ensure_ascii=False) + "\n```",
                "tool_calls": [],
            }
        if self.SIG_PLAN in sigs:
            self.call_log.append("plan")
            return {
                "role": "assistant",
                "content": "```json\n" + json.dumps(self.plan, ensure_ascii=False) + "\n```",
                "tool_calls": [],
            }
        if self.SIG_REPORT in sigs:
            self.call_log.append("report")
            return {"role": "assistant", "content": self.report_text, "tool_calls": []}
        if self.SIG_STEP in sigs:
            self.call_log.append("step")
            if self.step_queue:
                return self.step_queue.pop(0)
            return {"role": "assistant", "content": "fallthrough", "tool_calls": []}
        self.call_log.append("unknown")
        return {"role": "assistant", "content": "(unknown context)", "tool_calls": []}


def _patch_bridges(
    monkeypatch,
    llm: Callable,
    *,
    skill_tools: list[dict] | None = None,
    skill_invoke: Callable | None = None,
):
    """Monkeypatch the three V2 bridges at module level so both TaskLoop
    and TaskExecutor pick up the fakes (TaskLoop imports inside methods,
    TaskExecutor caches module refs at __init__)."""
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb

    monkeypatch.setattr(lb, "call_llm", llm)
    monkeypatch.setattr(sb, "get_skill_tools_for_agent",
                        lambda *_a, **_k: list(skill_tools or []))
    monkeypatch.setattr(sb, "invoke_skill",
                        skill_invoke or (lambda *_a, **_k: "ok"))
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(mb, "invoke_mcp", lambda *_a, **_k: "")


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


# ── 1. happy path: conversation template ──────────────────────────────


def test_e2e_conversation_happy_path(monkeypatch):
    """Simplest viable flow: conversation template, no required slots,
    no tools, no verify rules, no artifacts. Expect the LLM summary to
    land in ``task.context.messages`` and final status=SUCCEEDED."""
    router = LLMRouter(
        intake={"filled": {}, "missing": [], "clarification": ""},
        plan={"steps": [{"id": "s1", "goal": "reply", "tools_hint": [],
                         "exit_check": {}}],
              "expected_artifact_count": 0},
        step_responses=[
            {"role": "assistant", "content": "Hello world!", "tool_calls": []},
        ],
        report_text="✅ 对话已完成",
    )
    _patch_bridges(monkeypatch, router)

    task = Task(
        id="t_conv",
        agent_id="av2_test",
        template_id="conversation",
        intent="hi",
        phase=TaskPhase.INTAKE,
        status=TaskStatus.RUNNING,
    )
    bus = FakeBus()
    template = {
        "id": "conversation",
        "required_slots": [],
        "allowed_tools": [],
        "verify_rules": [],
        "report_template": "{last_assistant_message}",
        "plan_prompt": "reply to the user",
    }
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(), template=template)
    loop.run()

    # Terminal state.
    assert task.phase == TaskPhase.DONE
    assert task.status == TaskStatus.SUCCEEDED
    assert task.finished_reason == "completed"

    # All 6 phases observed in order (INTAKE → PLAN → EXECUTE → VERIFY
    # → DELIVER → REPORT).
    phase_enters = [e["payload"]["phase"] for e in bus.events if e["type"] == "phase_enter"]
    assert phase_enters == ["intake", "plan", "execute", "verify", "deliver", "report"]

    # Final user-facing assistant message exists.
    final_assistant = [m for m in task.context.messages if m.get("role") == "assistant"]
    assert any(m.get("content") for m in final_assistant)

    # task_completed emitted exactly once.
    assert bus.types().count("task_completed") == 1
    assert bus.types().count("task_failed") == 0


# ── 2. happy path with tool + verify + deliver ────────────────────────


def test_e2e_with_tool_call_and_file_artifact(monkeypatch, tmp_path):
    """Plan has one step whose exit_check is ``tool_used=write_file``. The
    fake skill creates a real file; Execute records an artifact; Verify
    rule (``contains_section`` on last assistant text) passes because
    the final message has a ``## Summary``; Deliver emits a receipt."""
    out = tmp_path / "report.md"
    out.write_text("content")

    def fake_invoke_skill(agent_id, tool_name, args):
        if tool_name == "write_file":
            return str(out)
        return "ok"

    plan = {
        "steps": [{
            "id": "s1",
            "goal": "write report",
            "tools_hint": ["write_file"],
            "exit_check": {"type": "tool_used", "spec": {"tool": "write_file"}},
        }],
        "expected_artifact_count": 1,
    }
    step_responses = [
        # Turn 1: call write_file
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function":
                         {"name": "write_file", "arguments": "{}"}}]},
        # Turn 2 (only reached if exit check hadn't been met): idle
        {"role": "assistant", "content": "## Summary\nAll done.", "tool_calls": []},
    ]

    router = LLMRouter(
        intake={"filled": {"topic": "x"}, "missing": [], "clarification": ""},
        plan=plan,
        step_responses=step_responses,
        report_text="✅ 报告已完成",
    )
    _patch_bridges(
        monkeypatch, router,
        skill_tools=[_tool_schema("write_file")],
        skill_invoke=fake_invoke_skill,
    )

    task = Task(
        id="t_file",
        agent_id="av2_test",
        template_id="research_report",
        intent="produce a report",
        phase=TaskPhase.INTAKE,
    )
    bus = FakeBus()
    template = {
        "id": "research_report",
        "required_slots": [{"name": "topic", "description": "subject"}],
        "allowed_tools": ["write_file"],
        "verify_rules": [
            # tool_used scans entire message history for the tool call.
            {"id": "used_tool", "kind": "tool_used",
             "spec": {"tool": "write_file"}},
        ],
        "report_template": "✅ {topic} — {artifact_count} artifacts",
    }
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(), template=template)
    loop.run()

    assert task.status == TaskStatus.SUCCEEDED
    assert task.phase == TaskPhase.DONE

    # Exactly one file artifact + one delivery_receipt.
    file_arts = [a for a in task.artifacts if a.kind == "file"]
    receipts = [a for a in task.artifacts if a.kind == "delivery_receipt"]
    assert len(file_arts) == 1
    assert file_arts[0].handle == str(out)
    assert len(receipts) == 1
    assert receipts[0].handle == str(out)   # deliver ok: receipt handle echoes path

    # Verify emitted a verify_check, and it passed.
    vc = [e for e in bus.events if e["type"] == "verify_check"]
    assert len(vc) == 1
    assert vc[0]["payload"]["passed"] is True

    # artifact_created fired twice: once in Execute, once in Deliver.
    assert bus.types().count("artifact_created") == 2


# ── 3. verify failure → soft-fail → Report still runs ─────────────────


def test_e2e_verify_fails_twice_soft_fails_to_report(monkeypatch):
    """Verify rule never passes (asks for a section the LLM never produces).
    After 2 rewinds the Verify retry budget is exhausted; run() soft-fails
    to Report with ``finished_reason='verify'`` and status=FAILED. The
    Report summary is still written — invariant G4: every task reaches
    Report with a user-facing message."""
    plan = {
        "steps": [{
            "id": "s1", "goal": "write",
            "tools_hint": [], "exit_check": {},
        }],
        "expected_artifact_count": 0,
    }

    # Every step response is text WITHOUT the required section.
    bad_text = "Here's some text, but no summary header."

    router = LLMRouter(
        intake={"filled": {}, "missing": [], "clarification": ""},
        plan=plan,
        step_responses=[
            {"role": "assistant", "content": bad_text, "tool_calls": []}
            for _ in range(20)  # plenty for the re-runs
        ],
        report_text="❌ 任务失败 (模拟)",
    )
    _patch_bridges(monkeypatch, router)

    task = Task(
        id="t_fail",
        agent_id="av2_test",
        template_id="conversation",
        intent="broken verify",
        phase=TaskPhase.INTAKE,
    )
    bus = FakeBus()
    template = {
        "id": "meeting_summary",   # pick a non-conversation id so LLM report runs
        "required_slots": [],
        "allowed_tools": [],
        "verify_rules": [
            {"id": "must_have_summary", "kind": "contains_section",
             "spec": {"section": "## Summary"}},
        ],
        "report_template": "failed: {failed_phase}",
    }
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(), template=template)
    loop.run()

    # Terminal state: FAILED but still DONE (not stuck).
    assert task.phase == TaskPhase.DONE
    assert task.status == TaskStatus.FAILED
    assert task.finished_reason == "verify"

    # verify_retry emitted at least twice (two rewinds).
    vr = [e for e in bus.events if e["type"] == "verify_retry"]
    assert len(vr) >= 2, f"expected ≥2 verify_retry, got {len(vr)}"

    # Invariant G4: Report still ran; task_failed event fired; final
    # assistant message exists in context.
    assert "task_failed" in bus.types()
    assert "task_completed" not in bus.types()
    final_assistant = [m for m in task.context.messages if m.get("role") == "assistant"]
    assert any(m.get("content") for m in final_assistant), \
        "Report must write a user-facing assistant message even on failure"


# ── 4. Intake PAUSED when slots missing ───────────────────────────────


def test_e2e_intake_pauses_for_clarification(monkeypatch):
    """If Intake cannot fill a required slot, task.status=PAUSED and
    an ``intake_clarification`` event is emitted. Run exits cleanly
    without advancing to Plan (no retry recorded)."""
    router = LLMRouter(
        intake={
            "filled": {},
            "missing": ["topic"],
            "clarification": "请告诉我报告的主题。",
        },
    )
    _patch_bridges(monkeypatch, router)

    task = Task(
        id="t_pause",
        agent_id="av2_test",
        template_id="research_report",
        intent="help me",
        phase=TaskPhase.INTAKE,
    )
    bus = FakeBus()
    template = {
        "id": "research_report",
        "required_slots": [{"name": "topic", "description": "report subject"}],
        "allowed_tools": [],
        "verify_rules": [],
    }
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(), template=template)
    loop.run()

    assert task.status == TaskStatus.PAUSED
    assert task.phase == TaskPhase.INTAKE  # did NOT advance
    clar = [e for e in bus.events if e["type"] == "intake_clarification"]
    assert len(clar) == 1
    assert clar[0]["payload"]["missing_slots"] == ["topic"]
    assert "请告诉我" in clar[0]["payload"]["question"]

    # No phase_retry fired (pause is not a retry).
    assert not any(e["type"] == "phase_retry" for e in bus.events)
