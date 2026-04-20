"""Stage 3 smoke tests for ``app.v2.core.task_executor.TaskExecutor``.

Six exit-path scenarios are driven by a fake LLM bridge so the tests
never hit the real model / skill registry / MCP layer:

    1. exit_check=tool_used          — first tool call satisfies exit
    2. exit_check=contains_section   — assistant text includes ``## Summary``
    3. exit_check=artifact_created   — tool returns a real file path
    4. exit_check=json_schema        — assistant emits valid JSON
    5. max_tool_turns exhausted      — returns False (→ outer retry)
    6. LLM omits tool_calls          — nudge fires; next turn succeeds

There is also a compaction test for ``on_context_pressure`` asserting
that a ``tool_calls`` message stays immediately adjacent to its
``role="tool"`` result after compaction — the OpenAI function-calling
spec requires this.
"""
from __future__ import annotations

import types
from typing import Iterable

import pytest

from app.v2.core.task import Task, Plan, PlanStep, TaskPhase
from app.v2.core.task_executor import TaskExecutor


# ── fakes ─────────────────────────────────────────────────────────────


class FakeBus:
    """Captures events in a list; no thread, no store."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, task_id, phase, event_type, payload):
        self.events.append({
            "task_id": task_id,
            "phase": phase.value if hasattr(phase, "value") else phase,
            "type": event_type,
            "payload": dict(payload or {}),
        })


class FakeAgent:
    def __init__(self, agent_id: str = "av2_test"):
        self.id = agent_id
        self.capabilities = types.SimpleNamespace(llm_tier="default")


class FakeLLMBridge:
    """Scripted response iterator."""

    def __init__(self, responses: Iterable[dict]):
        self._responses = list(responses)
        self._i = 0
        self.call_count = 0
        self.last_messages: list[dict] | None = None
        self.last_tools: list[dict] | None = None

    def call_llm(self, *, messages, tools=None, tier="default", max_tokens=4096, **_ignored):
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_tools = list(tools) if tools else None
        if self._i < len(self._responses):
            r = self._responses[self._i]
            self._i += 1
            return r
        # Fallback: silent assistant so tests don't hang forever.
        return {"role": "assistant", "content": "(no more scripted responses)", "tool_calls": []}


class FakeSkillBridge:
    def __init__(
        self,
        tools: list[dict] | None = None,
        results: dict | None = None,
    ):
        self.tools = tools or []
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    def get_skill_tools_for_agent(self, agent_id):
        return list(self.tools)

    def invoke_skill(self, agent_id, tool_name, args):
        self.calls.append((tool_name, dict(args or {})))
        v = self.results.get(tool_name, f"skill {tool_name} ran")
        return v(args) if callable(v) else v


class FakeMCPBridge:
    def get_mcp_tools_for_agent(self, agent_id):
        return []

    def invoke_mcp(self, agent_id, tool_name, args):  # pragma: no cover
        raise RuntimeError("fake MCP never invoked in these tests")


# ── helpers ───────────────────────────────────────────────────────────


def _make_exec(
    llm: FakeLLMBridge,
    skill: FakeSkillBridge | None = None,
    *,
    intent: str = "test",
) -> tuple[TaskExecutor, Task, FakeBus]:
    task = Task(
        id="t_test",
        agent_id="av2_test",
        template_id="conversation",
        intent=intent,
        phase=TaskPhase.EXECUTE,
    )
    bus = FakeBus()
    agent = FakeAgent()
    exe = TaskExecutor(
        task=task,
        agent=agent,
        bus=bus,
        llm_bridge=llm,
        skill_bridge=skill or FakeSkillBridge(),
        mcp_bridge=FakeMCPBridge(),
    )
    return exe, task, bus


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} (test)",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_call(cid: str, name: str, args_json: str = "{}") -> dict:
    return {"id": cid, "function": {"name": name, "arguments": args_json}}


# ── 1. tool_used ──────────────────────────────────────────────────────


def test_exit_tool_used():
    skill = FakeSkillBridge(
        tools=[_tool_schema("search")],
        results={"search": "search results"},
    )
    llm = FakeLLMBridge([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "search")]},
    ])
    exe, task, bus = _make_exec(llm, skill)
    step = PlanStep(
        id="s1", goal="run a search",
        exit_check={"type": "tool_used", "spec": {"tool": "search"}},
    )
    assert exe.run_step(step) is True
    assert step.completed is True
    assert skill.calls == [("search", {})]
    # Verify ordering: tool_calls message immediately precedes tool result.
    msgs = task.context.messages
    tc_positions = [i for i, m in enumerate(msgs) if m.get("tool_calls")]
    assert len(tc_positions) == 1
    i = tc_positions[0]
    assert msgs[i + 1].get("role") == "tool"
    assert msgs[i + 1].get("tool_call_id") == "c1"


# ── 2. contains_section ───────────────────────────────────────────────


def test_exit_contains_section():
    llm = FakeLLMBridge([
        {"role": "assistant", "content": "intro text\n\n## Summary\ndone.", "tool_calls": []},
    ])
    exe, task, bus = _make_exec(llm)
    step = PlanStep(
        id="s1", goal="write summary",
        exit_check={"type": "contains_section", "spec": {"section": "Summary"}},
    )
    assert exe.run_step(step) is True
    assert step.completed is True


# ── 3. artifact_created ───────────────────────────────────────────────


def test_exit_artifact_created(tmp_path):
    real_file = tmp_path / "out.txt"
    real_file.write_text("hello")
    skill = FakeSkillBridge(
        tools=[_tool_schema("write_file")],
        results={"write_file": str(real_file)},
    )
    llm = FakeLLMBridge([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "write_file")]},
    ])
    exe, task, bus = _make_exec(llm, skill)
    step = PlanStep(
        id="s1", goal="write a file",
        exit_check={"type": "artifact_created", "spec": {"kind": "file", "min_count": 1}},
    )
    assert exe.run_step(step) is True
    assert len(task.artifacts) == 1
    assert task.artifacts[0].kind == "file"
    assert task.artifacts[0].handle == str(real_file)


# ── 4. json_schema ────────────────────────────────────────────────────


def test_exit_json_schema():
    llm = FakeLLMBridge([
        {
            "role": "assistant",
            "content": '```json\n{"title": "x", "count": 2}\n```',
            "tool_calls": [],
        },
    ])
    exe, task, bus = _make_exec(llm)
    step = PlanStep(
        id="s1", goal="emit json",
        exit_check={"type": "json_schema", "spec": {"required": ["title", "count"]}},
    )
    assert exe.run_step(step) is True


# ── 5. max_tool_turns exhausted ───────────────────────────────────────


def test_max_tool_turns_exhausted():
    # LLM keeps calling "foo" but step asks for "bar" → exit never met.
    skill = FakeSkillBridge(
        tools=[_tool_schema("foo")],
        results={"foo": "ok"},
    )
    responses = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call(f"c{i}", "foo")]}
        for i in range(TaskExecutor.MAX_TOOL_TURNS_PER_STEP + 2)
    ]
    llm = FakeLLMBridge(responses)
    exe, task, bus = _make_exec(llm, skill)
    step = PlanStep(
        id="s1", goal="call bar",
        exit_check={"type": "tool_used", "spec": {"tool": "bar"}},
    )
    assert exe.run_step(step) is False
    assert step.completed is False
    # step_exit event carries reason=max_tool_turns so outer TaskLoop can retry.
    step_exits = [e for e in bus.events if e["type"] == "step_exit"]
    assert any(e["payload"].get("reason") == "max_tool_turns" for e in step_exits)
    # Guardrail: LLM was called exactly MAX_TOOL_TURNS_PER_STEP times.
    assert llm.call_count == TaskExecutor.MAX_TOOL_TURNS_PER_STEP


# ── 6. nudge: no tool call this turn, then tool call next turn ────────


def test_nudge_then_tool_call_succeeds():
    skill = FakeSkillBridge(
        tools=[_tool_schema("search")],
        results={"search": "ok"},
    )
    llm = FakeLLMBridge([
        # Turn 1: assistant narrates without calling a tool → nudge injected.
        {"role": "assistant", "content": "I will search soon.", "tool_calls": []},
        # Turn 2: assistant finally calls the tool.
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("c1", "search")]},
    ])
    exe, task, bus = _make_exec(llm, skill)
    step = PlanStep(
        id="s1", goal="search",
        exit_check={"type": "tool_used", "spec": {"tool": "search"}},
    )
    assert exe.run_step(step) is True
    nudges = [
        m for m in task.context.messages
        if m.get("role") == "system" and "没有调用任何工具" in (m.get("content") or "")
    ]
    assert len(nudges) == 1, "exactly one nudge should have fired"


# ── 7. on_context_pressure preserves tool_call/tool_result adjacency ──


def test_context_pressure_preserves_tool_call_adjacency():
    """The OpenAI function-calling spec requires a ``tool_calls`` message
    to be IMMEDIATELY followed by its matching ``role="tool"`` results.
    Compaction must never insert narrative between them.
    """
    llm = FakeLLMBridge([])  # never used
    exe, task, bus = _make_exec(llm)

    msgs: list[dict] = [{"role": "system", "content": "initial system"}]
    # Lots of narrative first.
    for i in range(80):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    # A tool_call + its result nested in the middle.
    msgs.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [_tool_call("tc_mid", "search")],
    })
    msgs.append({
        "role": "tool",
        "tool_call_id": "tc_mid",
        "name": "search",
        "content": "search result payload",
    })
    # More narrative after.
    for i in range(80, 160):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    task.context.messages = msgs
    before_len = len(task.context.messages)
    assert before_len > TaskExecutor.CONTEXT_MESSAGES_HARD_CUT

    exe.on_context_pressure()

    new = task.context.messages
    assert len(new) < before_len, "compaction should shrink message list"

    # The tool_calls message must still be present AND immediately
    # followed by the matching role=tool result.
    tc_positions = [i for i, m in enumerate(new) if m.get("tool_calls")]
    assert len(tc_positions) == 1, "exactly one tool_calls message should survive"
    i = tc_positions[0]
    assert new[i + 1].get("role") == "tool"
    assert new[i + 1].get("tool_call_id") == "tc_mid"
    assert new[i + 1].get("content") == "search result payload"

    # First kept message is the initial system; a [compacted] summary
    # should exist (because we had >20 older narrative units).
    assert new[0].get("role") == "system"
    assert new[0].get("content") == "initial system"
    compacted = [m for m in new if (m.get("content") or "").startswith("[compacted]")]
    assert len(compacted) == 1
