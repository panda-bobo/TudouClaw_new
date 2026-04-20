"""Stage 4 smoke tests for the Verify phase (PRD §8.4).

Two layers of testing:

* Unit tests on ``app.v2.core.verify.evaluate_rules`` — exercise every
  supported ``kind`` (regex, contains_section, json_schema, tool_used,
  llm_judge) plus the ``when`` conditional.
* Handler-level test on ``TaskLoop._verify`` — assert that a failing
  rule rewinds the task to EXECUTE, injects ``[verify]`` feedback into
  context, marks steps incomplete, and emits ``verify_retry``. A passing
  rule set returns True so the outer loop advances to Deliver.
"""
from __future__ import annotations

import types
from typing import Iterable

import pytest

from app.v2.core.task import (
    Task, Plan, PlanStep, Artifact, TaskPhase, TaskStatus, TaskContext,
)
from app.v2.core.verify import evaluate_rules
from app.v2.core.task_loop import TaskLoop, MAX_RETRIES_PER_PHASE


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


class FakeStore:
    """Captures save() calls; no DB."""

    def __init__(self):
        self.saves = 0

    def save(self, task):
        self.saves += 1


class FakeAgent:
    def __init__(self, agent_id="av2_test"):
        self.id = agent_id
        self.capabilities = types.SimpleNamespace(llm_tier="default")


# ── helpers ───────────────────────────────────────────────────────────


def _make_task(
    messages: list[dict] | None = None,
    artifacts: list[Artifact] | None = None,
    filled_slots: dict | None = None,
    steps: list[PlanStep] | None = None,
) -> Task:
    ctx = TaskContext(
        messages=list(messages or []),
        filled_slots=dict(filled_slots or {}),
    )
    task = Task(
        id="t_v",
        agent_id="av2_test",
        template_id="research_report",
        intent="test",
        phase=TaskPhase.VERIFY,
        context=ctx,
        plan=Plan(steps=list(steps or [])),
    )
    for a in artifacts or []:
        task.artifacts.append(a)
    return task


def _assistant(text: str, tool_calls: list[dict] | None = None) -> dict:
    return {"role": "assistant", "content": text, "tool_calls": tool_calls or []}


# ── evaluate_rules: kind coverage ─────────────────────────────────────


def test_contains_section_passes():
    task = _make_task(messages=[_assistant("intro\n\n## Summary\ntext\n\n## Action Items\n- [ ] do x")])
    rules = [
        {"id": "r1", "kind": "contains_section", "spec": {"section": "## Summary"}},
        {"id": "r2", "kind": "section_exists",   "spec": {"section": "Action Items"}},
    ]
    rep = evaluate_rules(rules, task=task)
    assert all(r["passed"] for r in rep), rep


def test_contains_section_fails_when_missing():
    task = _make_task(messages=[_assistant("plain text, no heading")])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "contains_section", "spec": {"section": "## Summary"}}],
        task=task,
    )
    assert rep[0]["passed"] is False
    assert "missing" in rep[0]["note"]


def test_regex_min_matches():
    task = _make_task(messages=[_assistant("- [ ] a\n- [x] b\n- [ ] c")])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "regex",
          "spec": {"pattern": r"- \[[ x]\]", "min_matches": 3}}],
        task=task,
    )
    assert rep[0]["passed"] is True
    assert "matches=3" in rep[0]["note"]


def test_regex_min_matches_fails():
    task = _make_task(messages=[_assistant("- [ ] only one")])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "regex",
          "spec": {"pattern": r"- \[[ x]\]", "min_matches": 2}}],
        task=task,
    )
    assert rep[0]["passed"] is False


def test_json_schema_required_keys():
    task = _make_task(messages=[_assistant('```json\n{"title":"x","count":2}\n```')])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "json_schema",
          "spec": {"required": ["title", "count"]}}],
        task=task,
    )
    assert rep[0]["passed"] is True


def test_json_schema_missing_key():
    task = _make_task(messages=[_assistant('{"title":"x"}')])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "json_schema",
          "spec": {"required": ["title", "count"]}}],
        task=task,
    )
    assert rep[0]["passed"] is False
    assert "missing" in rep[0]["note"]


def test_json_schema_path_min_words():
    """Template form: artifact.summary word count."""
    task = _make_task(artifacts=[
        Artifact(id="a1", kind="file", handle="/tmp/x", summary="hello world foo bar"),
        Artifact(id="a2", kind="file", handle="/tmp/y", summary="baz qux"),
    ])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "json_schema",
          "spec": {"path": "artifacts[*].summary", "min_words": 5}}],
        task=task,
    )
    assert rep[0]["passed"] is True  # 4 + 2 = 6 words
    rep2 = evaluate_rules(
        [{"id": "r1", "kind": "json_schema",
          "spec": {"path": "artifacts[*].summary", "min_words": 10}}],
        task=task,
    )
    assert rep2[0]["passed"] is False


def test_tool_used_scans_message_history():
    task = _make_task(messages=[
        _assistant("", tool_calls=[{"id": "c1", "function": {"name": "send_email", "arguments": "{}"}}]),
        {"role": "tool", "tool_call_id": "c1", "name": "send_email", "content": "ok"},
    ])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "tool_used", "spec": {"tool": "send_email"}}],
        task=task,
    )
    assert rep[0]["passed"] is True


def test_tool_used_not_called():
    task = _make_task(messages=[_assistant("just text")])
    rep = evaluate_rules(
        [{"id": "r1", "kind": "tool_used", "spec": {"tool": "send_email"}}],
        task=task,
    )
    assert rep[0]["passed"] is False


def test_when_conditional_skips_rule():
    """Rule with ``when: filled_slots.delivery == 'email'`` is skipped
    when the slot is ``'slack'`` and reported as passed+skipped."""
    task = _make_task(filled_slots={"delivery": "slack"})
    rep = evaluate_rules(
        [{"id": "r1", "kind": "tool_used",
          "spec": {"tool": "send_email", "when": "filled_slots.delivery == 'email'"}}],
        task=task,
    )
    assert rep[0]["passed"] is True
    assert "skipped" in rep[0]["note"]


def test_when_conditional_runs_when_true():
    task = _make_task(filled_slots={"delivery": "email"})
    rep = evaluate_rules(
        [{"id": "r1", "kind": "tool_used",
          "spec": {"tool": "send_email", "when": "filled_slots.delivery == 'email'"}}],
        task=task,
    )
    # Rule runs and fails because tool was never called.
    assert rep[0]["passed"] is False
    assert "skipped" not in rep[0]["note"]


def test_llm_judge_pass():
    task = _make_task(messages=[_assistant("final report looks good")])

    def fake_llm(messages, tools=None, max_tokens=4096):
        return {"role": "assistant", "content": "PASS — quality acceptable", "tool_calls": []}

    rep = evaluate_rules(
        [{"id": "r1", "kind": "llm_judge",
          "spec": {"prompt": "is this good?", "pass_token": "PASS"}}],
        task=task,
        llm_caller=fake_llm,
    )
    assert rep[0]["passed"] is True


def test_llm_judge_fail():
    task = _make_task(messages=[_assistant("sloppy text")])

    def fake_llm(messages, tools=None, max_tokens=4096):
        return {"role": "assistant", "content": "FAIL — needs rewrite", "tool_calls": []}

    rep = evaluate_rules(
        [{"id": "r1", "kind": "llm_judge",
          "spec": {"prompt": "is this good?", "pass_token": "PASS"}}],
        task=task,
        llm_caller=fake_llm,
    )
    assert rep[0]["passed"] is False


def test_unknown_kind_fails():
    task = _make_task()
    rep = evaluate_rules(
        [{"id": "r1", "kind": "totally_made_up", "spec": {}}],
        task=task,
    )
    assert rep[0]["passed"] is False
    assert "unknown kind" in rep[0]["note"]


# ── TaskLoop._verify handler ──────────────────────────────────────────


def _make_loop(task: Task, template: dict) -> tuple[TaskLoop, FakeBus, FakeStore]:
    bus = FakeBus()
    store = FakeStore()
    loop = TaskLoop(task=task, agent=FakeAgent(), bus=bus, store=store, template=template)
    return loop, bus, store


def test_verify_handler_no_rules_returns_true():
    task = _make_task()
    loop, bus, _ = _make_loop(task, template={"id": "conversation", "verify_rules": []})
    assert loop._verify() is True
    types_emitted = [e["type"] for e in bus.events]
    assert "verify_check" in types_emitted


def test_verify_handler_all_pass_returns_true():
    task = _make_task(messages=[_assistant("## Summary\nall done")])
    tmpl = {
        "id": "t1",
        "verify_rules": [
            {"id": "has_summary", "kind": "contains_section",
             "spec": {"section": "## Summary"}},
        ],
    }
    loop, bus, _ = _make_loop(task, tmpl)
    assert loop._verify() is True
    checks = [e for e in bus.events if e["type"] == "verify_check"]
    assert len(checks) == 1
    assert checks[0]["payload"]["passed"] is True
    # Phase must NOT have been rewound to EXECUTE.
    assert task.phase == TaskPhase.VERIFY


def test_verify_handler_failure_rewinds_to_execute():
    step = PlanStep(id="s1", goal="write", exit_check={}, completed=True)
    task = _make_task(
        messages=[_assistant("no heading here")],
        steps=[step],
    )
    tmpl = {
        "id": "t1",
        "verify_rules": [
            {"id": "has_summary", "kind": "contains_section",
             "spec": {"section": "## Summary"}},
        ],
    }
    loop, bus, _ = _make_loop(task, tmpl)

    # Seed an execute-retry counter so we can assert it was reset.
    task.retries[TaskPhase.EXECUTE.value] = 2

    ok = loop._verify()
    assert ok is False

    # Phase manually rewound to EXECUTE so outer loop will re-dispatch it.
    assert task.phase == TaskPhase.EXECUTE

    # All previously-completed steps reset to incomplete.
    assert task.plan.steps[0].completed is False

    # Execute retry counter reset for the re-run.
    assert task.retries.get(TaskPhase.EXECUTE.value, 0) == 0

    # Verify feedback injected as [verify] system message.
    feedback = [
        m for m in task.context.messages
        if m.get("role") == "system" and (m.get("content") or "").startswith("[verify]")
    ]
    assert len(feedback) == 1
    assert "has_summary" in feedback[0]["content"]

    # Events: one verify_check (failed) + one verify_retry.
    checks = [e for e in bus.events if e["type"] == "verify_check"]
    retries = [e for e in bus.events if e["type"] == "verify_retry"]
    assert len(checks) == 1 and checks[0]["payload"]["passed"] is False
    assert len(retries) == 1
    assert retries[0]["payload"]["failing_rule_ids"] == ["has_summary"]


def test_verify_budget_matches_prd():
    """Per PRD §8.4: up to 2 verify retries. That corresponds to
    ``MAX_RETRIES_PER_PHASE[VERIFY] == 2`` — so after 2 failed rewinds
    the next failure soft-fails to Report."""
    assert MAX_RETRIES_PER_PHASE[TaskPhase.VERIFY] == 2
