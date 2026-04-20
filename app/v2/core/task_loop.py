"""
TaskLoop — 6-phase state machine (PRD §6.2).

Responsibility: drive a Task from its current phase to DONE. Phase
handlers return ``bool`` indicating whether the phase's exit condition
was met. This is the ONLY contract they must honor; retry / soft-fail
logic lives here, not in handlers.

Implementation status:
    Intake / Plan  — implemented (PRD §8.1, §8.2).
    Execute         — delegates to TaskExecutor (skeleton in §6.3).
    Verify / Deliver — NotImplementedError for now (stages 3-4).
    Report          — fallback summary implemented end-to-end.

A phase handler may set ``self.task.status`` to PAUSED (e.g. Intake
needs a user clarification). The outer ``run()`` loop checks status
after every dispatch and exits cleanly without recording a retry.
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from .task import (
    Task,
    TaskPhase,
    TaskStatus,
    Lesson,
    Plan,
    PlanStep,
)

if TYPE_CHECKING:
    from ..agent.agent_v2 import AgentV2
    from .task_events import TaskEventBus
    from .task_store import TaskStore


_COUNTED_EVENTS: frozenset[str] = frozenset({
    "task_submitted", "task_completed", "task_failed",
    "task_paused", "task_resumed",
    "phase_retry", "phase_error",
    "verify_retry",
})


MAX_RETRIES_PER_PHASE: dict[TaskPhase, int] = {
    TaskPhase.INTAKE:  2,
    TaskPhase.PLAN:    3,
    TaskPhase.EXECUTE: 3,
    TaskPhase.VERIFY:  2,
    TaskPhase.DELIVER: 2,
    TaskPhase.REPORT:  0,   # Report is a sink; never retried.
}

_PHASE_ORDER = [
    TaskPhase.INTAKE,
    TaskPhase.PLAN,
    TaskPhase.EXECUTE,
    TaskPhase.VERIFY,
    TaskPhase.DELIVER,
    TaskPhase.REPORT,
    TaskPhase.DONE,
]


class TaskLoop:
    def __init__(
        self,
        task: Task,
        agent: "AgentV2",
        bus: "TaskEventBus",
        store: "TaskStore",
        template: dict | None = None,
    ):
        self.task = task
        self.agent = agent
        self.bus = bus
        self.store = store
        # Template is a plain dict (loaded from YAML) for the skeleton.
        # A proper TaskTemplate dataclass will land with v2.templates.loader.
        self.template = template or {}

    # ── entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Advance the task until DONE. Blocking; call in a background thread."""
        if self.task.started_at is None:
            self.task.started_at = time.time()
            self.store.save(self.task)

        while (
            self.task.phase != TaskPhase.DONE
            and self.task.status == TaskStatus.RUNNING
        ):
            if self._timed_out():
                self._finalize_timeout()
                break

            phase = self.task.phase
            self._emit("phase_enter", {"phase": phase.value})

            try:
                exit_ok = self._dispatch_phase(phase)
            except NotImplementedError as e:
                # Skeleton phase handlers raise this. Treat as retryable
                # failure so we can still smoke-test the FSM end-to-end.
                self._emit("phase_error", {
                    "phase": phase.value,
                    "error": f"NotImplementedError: {e}",
                })
                exit_ok = False
            except Exception as e:  # noqa: BLE001
                self._emit("phase_error", {
                    "phase": phase.value,
                    "error": f"{type(e).__name__}: {e}",
                })
                exit_ok = False

            # A handler may have set a non-RUNNING status (e.g. Intake
            # set PAUSED to wait for clarification). Honor that without
            # recording a retry; the next while-check exits the loop.
            if self.task.status != TaskStatus.RUNNING:
                self.store.save(self.task)
                continue

            if exit_ok:
                self._emit("phase_exit", {"phase": phase.value, "ok": True})
                self._advance_next(phase)
                self.store.save(self.task)
                continue

            # Exit not met → retry budget check.
            attempt = self.task.record_retry(phase)
            budget = MAX_RETRIES_PER_PHASE.get(phase, 0)
            if attempt <= budget:
                self._emit("phase_retry", {
                    "phase": phase.value,
                    "attempt": attempt,
                    "budget": budget,
                })
                self.store.save(self.task)
                continue

            # Hard-retry exhausted → soft-fail path: jump to Report.
            self._soft_fail(phase)
            self.store.save(self.task)

        self._finalize()

    # ── phase dispatch ─────────────────────────────────────────────────

    def _dispatch_phase(self, phase: TaskPhase) -> bool:
        handler = {
            TaskPhase.INTAKE:  self._intake,
            TaskPhase.PLAN:    self._plan,
            TaskPhase.EXECUTE: self._execute,
            TaskPhase.VERIFY:  self._verify,
            TaskPhase.DELIVER: self._deliver,
            TaskPhase.REPORT:  self._report,
        }.get(phase)
        if handler is None:
            raise RuntimeError(f"no handler for phase {phase!r}")
        return handler()

    # ── phase handlers (skeleton — see PRD §8 for full contracts) ──────

    # ── Intake (PRD §8.1) ──────────────────────────────────────────────

    def _intake(self) -> bool:
        """Extract required slots from ``task.intent``.

        Exit condition:
            All required slots filled → True.
        Otherwise:
            Set status=PAUSED, emit ``intake_clarification`` with the
            question, and return True (phase "ok" in the sense that
            it did what it could; the run loop will exit on status).
        """
        # ── Multimodal gate ──
        # If the task carries image/audio attachments, the resolved
        # provider MUST advertise ``supports_multimodal=True``. If it
        # doesn't, pause immediately with a friendly clarification
        # rather than letting Execute strip the attachment or fail
        # obscurely. PRD §6 / user decision: early, actionable feedback.
        if self.task.context.attachments and not self._multimodal_supported():
            missing_modes = sorted({
                str(a.get("kind") or "media")
                for a in self.task.context.attachments
            })
            tier = self._llm_tier()
            self.task.context.clarification_pending = True
            self.task.status = TaskStatus.PAUSED
            self._emit("intake_clarification", {
                "question": (
                    f"该任务包含 {', '.join(missing_modes)} 附件，"
                    f"但 agent 当前使用的 LLM tier 『{tier}』"
                    f"对应的 provider 未启用多模态支持。\n\n"
                    "请在 V2 Provider 配置中把一个启用多模态的 provider "
                    "绑定到 vision 或 default tier 后重试；或仅提交纯文本任务。"
                ),
                "missing_slots": ["multimodal_provider"],
                "attachment_kinds": missing_modes,
                "tier": tier,
            })
            return True

        template = self._load_template()
        required = [s for s in (template.get("required_slots") or [])
                    if not s.get("optional") and s.get("default") is None]
        # Template may seed defaults for optional slots.
        for s in (template.get("required_slots") or []):
            if s.get("default") is not None:
                self.task.context.filled_slots.setdefault(s["name"], s["default"])

        if not required:
            # Nothing to extract; emit an empty slot-fill so the event
            # stream is still well-formed.
            self._emit("intake_slots_filled", {"slots": dict(self.task.context.filled_slots)})
            return True

        # If every required slot is already filled (e.g. resumed after
        # a /clarify call), skip the LLM round-trip.
        missing_before = [s["name"] for s in required
                          if s["name"] not in self.task.context.filled_slots]
        if not missing_before:
            self._emit("intake_slots_filled", {"slots": dict(self.task.context.filled_slots)})
            return True

        # LLM slot extraction.
        parsed = self._llm_extract_slots(template, required, missing_before)
        if not parsed:
            return False  # outer loop records retry; budget = 2

        filled = parsed.get("filled") or {}
        # Merge — don't overwrite slots the user already supplied via /clarify.
        for k, v in filled.items():
            self.task.context.filled_slots.setdefault(k, v)

        still_missing = [s["name"] for s in required
                         if s["name"] not in self.task.context.filled_slots]

        if not still_missing:
            self._emit("intake_slots_filled", {"slots": dict(self.task.context.filled_slots)})
            self.task.context.clarification_pending = False
            return True

        # Need user input — pause the task.
        question = (parsed.get("clarification")
                    or f"请补充以下信息：{', '.join(still_missing)}")
        self.task.context.clarification_pending = True
        self.task.status = TaskStatus.PAUSED
        self._emit("intake_clarification", {
            "question": question,
            "missing_slots": still_missing,
        })
        # Return value is effectively ignored (run() sees status != RUNNING).
        return True

    # ── Plan (PRD §8.2) ────────────────────────────────────────────────

    def _plan(self) -> bool:
        """Generate a structured ``Plan`` from intent + slots + lessons.

        Emits a ``phase_error`` on every soft-fail path so the UI can
        show WHY planning failed instead of the opaque
        "phase plan exceeded max retries" summary.
        """
        template = self._load_template()
        plan_json = self._llm_generate_plan(template)
        if not plan_json:
            # _llm_generate_plan already emitted a phase_error when the
            # bridge raised. But if it simply returned None because the
            # LLM wrote text that didn't parse as JSON, that surface is
            # silent — make it visible.
            self._emit("phase_error", {
                "phase": TaskPhase.PLAN.value,
                "error": "plan_json empty or not parseable "
                         "(LLM returned text without a valid ```json block)",
            })
            return False

        steps_raw = plan_json.get("steps") or []
        if not isinstance(steps_raw, list) or len(steps_raw) == 0:
            self._emit("phase_error", {
                "phase": TaskPhase.PLAN.value,
                "error": f"plan has no steps: {type(steps_raw).__name__} "
                         f"(length={len(steps_raw) if hasattr(steps_raw,'__len__') else '?'})",
                "plan_sample": str(plan_json)[:400],
            })
            return False

        steps: list[PlanStep] = []
        skipped: list[str] = []
        for i, s in enumerate(steps_raw):
            if not isinstance(s, dict):
                skipped.append(f"#{i}: not a dict ({type(s).__name__})")
                continue
            step_id = str(s.get("id") or f"s{i+1}")
            goal = str(s.get("goal") or "").strip()
            if not goal:
                skipped.append(f"#{i} ({step_id}): empty goal")
                continue
            exit_check = s.get("exit_check") or {}
            if not isinstance(exit_check, dict):
                exit_check = {}
            steps.append(PlanStep(
                id=step_id,
                goal=goal,
                tools_hint=list(s.get("tools_hint") or []),
                exit_check=exit_check,
            ))

        if not steps:
            self._emit("phase_error", {
                "phase": TaskPhase.PLAN.value,
                "error": "plan produced no valid steps after filtering",
                "skipped": skipped[:6],
                "raw_steps": str(steps_raw)[:400],
            })
            return False

        self.task.plan = Plan(
            steps=steps,
            expected_artifact_count=int(plan_json.get("expected_artifact_count") or 0),
        )
        self._emit("plan_draft", {
            "step_count": len(steps),
            "expected_artifact_count": self.task.plan.expected_artifact_count,
        })
        self._emit("plan_approved", {"step_count": len(steps)})
        return True

    def _execute(self) -> bool:
        """Drive every un-completed PlanStep through TaskExecutor.

        Exit condition: every step's ``completed`` flag is True.
        A step that fails its exit_check returns False here, which the
        outer ``run()`` routes into retry → soft-fail → Report, preserving
        the invariant that every task reaches Report (PRD G4).
        """
        from .task_executor import TaskExecutor

        if self.task.plan is None or not self.task.plan.steps:
            return False

        executor = TaskExecutor(self.task, self.agent, self.bus)
        for step in self.task.plan.steps:
            if step.completed:
                continue
            if not executor.run_step(step):
                return False
        return True

    def _verify(self) -> bool:
        """Evaluate ``template.verify_rules`` against task state.

        Happy path: every rule passes → return True → advance to Deliver.

        Failure path (PRD §8.4 "失败时回退到 Execute"):
            - Emit ``verify_check`` for each rule and ``verify_retry``
              listing the failing rule ids.
            - Inject a ``[verify]`` system message with the failure notes so
              the LLM knows what to fix on the next Execute pass.
            - Mark every plan step as incomplete (coarse but safe: we don't
              yet have rule → step provenance, and partial re-runs risk
              leaving the task in an inconsistent state).
            - Reset the Execute retry counter so the re-run gets a fresh
              budget.
            - Rewind ``task.phase`` to EXECUTE and return False.

        Returning False lets the outer ``run()`` record a retry on VERIFY;
        after ``MAX_RETRIES_PER_PHASE[VERIFY]`` rewinds the task soft-fails
        to Report with ``finished_reason='verify'``.
        """
        template = self._load_template()
        rules = template.get("verify_rules") or []
        if not rules:
            # No rules declared: nothing to verify, accept immediately.
            self._emit("verify_check", {
                "rule_id": "_no_rules",
                "passed": True,
                "note": "template has no verify_rules",
            })
            return True

        from .verify import evaluate_rules
        from ..bridges import llm_bridge

        report = evaluate_rules(
            rules,
            task=self.task,
            llm_caller=llm_bridge.call_llm,
        )
        self.task.context.scratch["verify_report"] = report
        for entry in report:
            self._emit("verify_check", entry)

        failing = [r for r in report if not r["passed"]]
        if not failing:
            return True

        # Rewind to Execute.
        self._emit("verify_retry", {
            "failing_rule_ids": [r["rule_id"] for r in failing],
            "reason": "verify rules failed",
        })
        feedback_lines = [
            f"- {r['rule_id']}: {r['note']}" for r in failing
        ]
        self.task.context.messages.append({
            "role": "system",
            "content": (
                "[verify] 以下校验规则未通过，请在本轮重跑中修正后再次产出：\n"
                + "\n".join(feedback_lines)
            ),
        })
        if self.task.plan is not None:
            for step in self.task.plan.steps:
                step.completed = False
        # Fresh Execute retry budget for the re-run.
        self.task.retries[TaskPhase.EXECUTE.value] = 0
        self.task.phase = TaskPhase.EXECUTE
        return False

    def _deliver(self) -> bool:
        """Dispatch each non-receipt artifact by ``kind`` (PRD §8.5).

        For each original artifact, call ``deliver_artifact`` (≤2 retries
        per artifact, all inside the dispatcher). A ``delivery_receipt``
        artifact is appended recording the outcome — its ``handle`` is
        the concrete delivery id on success, or ``"degraded:<artifact_id>"``
        on failure.

        Per PRD: single-artifact failure does NOT block the phase; we
        emit a ``phase_error`` summary if any artifacts degraded but still
        return True so the task reaches Report.

        The only way Deliver returns False is the PRD exit-condition
        violation: an original artifact has an empty ``handle``. That
        would indicate Execute produced a malformed artifact, which is
        worth a retry.
        """
        from .deliver import deliver_artifact
        from .task import Artifact

        originals = [
            a for a in self.task.artifacts if a.kind != "delivery_receipt"
        ]
        if not originals:
            return True

        template = self._load_template() or {}
        degraded = 0
        for artifact in originals:
            # PRD exit check: artifact handle must be non-empty. If it's
            # missing we kick back for retry rather than silently delivering
            # a broken placeholder.
            if not artifact.handle:
                self._emit("phase_error", {
                    "phase": TaskPhase.DELIVER.value,
                    "error": f"artifact {artifact.id} has empty handle",
                })
                return False

            ok, receipt_handle, note = deliver_artifact(
                artifact, self.task, template=template,
            )
            if not ok:
                degraded += 1
                receipt_handle = f"degraded:{artifact.id}"

            receipt = Artifact(
                id=f"R-{len(self.task.artifacts) + 1}",
                kind="delivery_receipt",
                handle=receipt_handle,
                summary=note,
                produced_by_tool=f"deliver/{artifact.kind}",
            )
            self.task.add_artifact(receipt)
            self._emit("artifact_created", {
                "artifact": {
                    "id": receipt.id,
                    "kind": receipt.kind,
                    "handle": receipt.handle,
                    "summary": receipt.summary,
                    "produced_by_tool": receipt.produced_by_tool,
                },
                "for_artifact_id": artifact.id,
                "delivered_ok": ok,
            })

        if degraded:
            self._emit("phase_error", {
                "phase": TaskPhase.DELIVER.value,
                "error": f"{degraded}/{len(originals)} artifact(s) degraded",
            })
        return True

    def _report(self) -> bool:
        """Report is a sink — always True. Emits terminal task event.

        Even when upstream phases soft-failed, Report still runs so the
        user ALWAYS sees a final summary (PRD G4: progressive feedback
        must reach the user).

        ``finished_reason`` is the discriminator: if upstream soft-fail
        set it to a phase name, we mark the task FAILED. Otherwise this
        is the happy path and we mark SUCCEEDED.
        """
        summary = self._compose_report()
        self.task.context.messages.append({
            "role": "assistant",
            "content": summary,
        })
        upstream_failed_phase = (
            self.task.finished_reason
            if self.task.finished_reason and self.task.finished_reason != "completed"
            else ""
        )
        if upstream_failed_phase:
            self.task.status = TaskStatus.FAILED
            self._emit("task_failed", {
                "summary": summary,
                "failed_phase": upstream_failed_phase,
                "reason": "hard_retry_exhausted",
            })
        else:
            self.task.status = TaskStatus.SUCCEEDED
            self.task.finished_reason = "completed"
            self._emit("task_completed", {
                "summary": summary,
                "artifact_count": len(self.task.artifacts),
                "duration_s": self._elapsed_s(),
            })
        # Advance to DONE directly: the outer run() loop's status-based
        # early-exit fires before _advance_next when a handler sets a
        # non-RUNNING status, so the phase wouldn't advance otherwise.
        # We want ``task.phase == DONE`` as a terminal-state invariant
        # independent of status.
        self.task.phase = TaskPhase.DONE
        return True

    # ── control flow helpers ───────────────────────────────────────────

    def _advance_next(self, cur: TaskPhase) -> None:
        idx = _PHASE_ORDER.index(cur)
        self.task.advance_phase(_PHASE_ORDER[idx + 1])

    def _soft_fail(self, phase: TaskPhase) -> None:
        """Hard-retry exhausted: record lesson and jump to Report.

        Status stays RUNNING (not FAILED) so the outer ``while``
        keeps running; Report will flip status to FAILED on entry by
        reading ``finished_reason``. This preserves the invariant
        "every task reaches Report" (PRD G4).
        """
        self.task.finished_reason = phase.value
        self.task.add_lesson(Lesson(
            id=f"L-{len(self.task.lessons) + 1}",
            phase=phase,
            issue=f"phase {phase.value} exceeded max retries",
            fix="human review required",
            created_at=time.time(),
        ))
        self._emit("lesson_recorded", {
            "phase": phase.value,
            "issue": "hard_retry_exhausted",
        })
        # Jump straight to Report so user still gets a final message.
        self.task.phase = TaskPhase.REPORT

    def _finalize(self) -> None:
        self.task.updated_at = time.time()
        if self.task.completed_at is None:
            self.task.completed_at = self.task.updated_at
        self.store.save(self.task)
        try:
            self.bus.flush_and_close(self.task.id)
        except Exception:
            pass
        # The agent is now free — promote the next QUEUED task, if any.
        # Errors here must never crash the finishing loop; the queue
        # will drain on the next finalisation at worst.
        try:
            from .task_controller import dispatch_next_queued
            dispatch_next_queued(self.task.agent_id, self.store, self.bus,
                                 agent=self.agent)
        except Exception:
            pass

    # ── timeout + status helpers ───────────────────────────────────────

    def _elapsed_s(self) -> float:
        if self.task.started_at is None:
            return 0.0
        return time.time() - self.task.started_at

    def _timed_out(self) -> bool:
        if self.task.started_at is None or self.task.timeout_s <= 0:
            return False
        return self._elapsed_s() > self.task.timeout_s

    def _finalize_timeout(self) -> None:
        self.task.status = TaskStatus.FAILED
        self.task.finished_reason = "timeout"
        self.task.phase = TaskPhase.DONE
        self._emit("task_failed", {
            "summary": f"task exceeded timeout ({self.task.timeout_s}s)",
            "failed_phase": "timeout",
            "reason": "wall_clock",
        })

    def _compose_report(self) -> str:
        """Three-tier fallback chain (PRD §8.6 "即使 LLM 失败也用模板化文本兜底"):

            1. LLM summary — full context + artifacts + lessons → assistant msg
            2. Template ``report_template`` interpolated with filled_slots +
               artifact_count / last_assistant_message (common placeholders)
            3. Hardcoded minimal summary

        We detect failure via ``finished_reason`` because ``status`` is
        still RUNNING at this point — ``_report`` flips it AFTER composing
        the summary (so the summary itself is the FIRST place the user
        sees the outcome).
        """
        failed = bool(
            self.task.finished_reason
            and self.task.finished_reason != "completed"
        )

        # Tier 1: LLM. Skip for the conversation template (it uses
        # report_template={last_assistant_message}; no need to re-round-trip).
        template = self._load_template() or {}
        use_llm = not failed and template.get("id") != "conversation"
        if use_llm:
            llm_text = self._llm_compose_report(template, failed=failed)
            if llm_text:
                return llm_text

        # Tier 2: template report_template with safe interpolation.
        tmpl_text = template.get("report_template") or ""
        if tmpl_text:
            interp = self._interpolate_report_template(tmpl_text, failed=failed)
            if interp.strip():
                return interp

        # Tier 3: hardcoded fallback.
        if failed:
            lines = [
                f"❌ 任务未完成：{self.task.intent}",
                f"失败阶段：{self.task.finished_reason}",
            ]
        else:
            lines = [
                f"✅ 任务已完成：{self.task.intent}",
                f"产出数量：{len(self.task.artifacts)}",
            ]
        if self.task.lessons:
            lines.append("记录复盘：")
            for le in self.task.lessons[-3:]:
                lines.append(f"  • [{le.phase.value}] {le.issue}")
        return "\n".join(lines)

    # ── report composition helpers ─────────────────────────────────────

    def _llm_compose_report(self, template: dict, *, failed: bool) -> str:
        """Call LLM to write the final report. Best-effort; any failure
        returns '' so the caller falls through to tier 2."""
        from ..bridges import llm_bridge

        artifact_lines = [
            f"- [{a.kind}] {a.handle or '(no handle)'} — {a.summary or ''}"
            for a in self.task.artifacts
            if a.kind != "delivery_receipt"
        ]
        receipt_lines = [
            f"- {a.handle}" for a in self.task.artifacts
            if a.kind == "delivery_receipt"
        ]
        lesson_lines = [
            f"- [{le.phase.value}] {le.issue} → {le.fix}"
            for le in self.task.lessons[-5:]
        ]
        slots_text = json.dumps(self.task.context.filled_slots, ensure_ascii=False)

        system = (
            "你是任务汇报助手。用中文写一段面向用户的最终汇报，"
            "开头用 ✅ 或 ❌ 标识结果；列出关键产出与交付；如有复盘请简述一句。"
            "不要编造未发生的产出。总长度不超过 300 字。"
        )
        user = (
            f"# 任务意图\n{self.task.intent}\n\n"
            f"# 已填槽位\n{slots_text}\n\n"
            f"# 阶段结果\n"
            f"{'失败' if failed else '完成'}"
            + (f"（失败阶段: {self.task.finished_reason}）\n\n" if failed else "\n\n")
            + f"# 产出 ({len(artifact_lines)})\n"
            + ("\n".join(artifact_lines) if artifact_lines else "（无）")
            + "\n\n# 交付凭证\n"
            + ("\n".join(receipt_lines) if receipt_lines else "（无）")
            + "\n\n# 复盘\n"
            + ("\n".join(lesson_lines) if lesson_lines else "（无）")
        )
        try:
            msg = self._call_llm(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                tools=None,
                max_tokens=600,
            )
            text = (msg.get("content") or "").strip()
            return text
        except Exception as e:  # noqa: BLE001
            self._emit("phase_error", {
                "phase": TaskPhase.REPORT.value,
                "error": f"llm_compose_report failed: {type(e).__name__}: {e}",
            })
            return ""

    def _interpolate_report_template(self, tmpl: str, *, failed: bool) -> str:
        """Interpolate template placeholders with task data. Safe: missing
        keys stay as ``{key}`` rather than raising."""
        last_assistant = ""
        for m in reversed(self.task.context.messages):
            if m.get("role") == "assistant" and m.get("content"):
                last_assistant = m["content"] or ""
                break
        non_receipt = [a for a in self.task.artifacts if a.kind != "delivery_receipt"]
        action_items = 0
        for a in self.task.artifacts:
            # Count action-item checkboxes in last assistant text.
            pass
        ai_matches = re.findall(r"- \[[ x]\]", last_assistant or "")
        action_items = len(ai_matches)

        extras = {
            "intent": self.task.intent,
            "artifact_count": len(non_receipt),
            "action_item_count": action_items,
            "last_assistant_message": last_assistant,
            "failed_phase": self.task.finished_reason,
            "status": ("failed" if failed else "completed"),
        }
        return _safe_format(tmpl, self.task.context.filled_slots, extras)

    def _emit(self, event_type: str, payload: dict) -> None:
        self.bus.publish(self.task.id, self.task.phase, event_type, payload)
        # Counter emission for dashboard metrics. The set of counted
        # types is intentionally small — everything else lives in the
        # event stream at full fidelity.
        if event_type in _COUNTED_EVENTS:
            try:
                from .observability import record as _record_metric
                _record_metric(f"v2.{event_type}")
            except Exception:
                pass

    # ── multimodal support check ───────────────────────────────────────

    def _llm_tier(self) -> str:
        """The tier the agent declared (or 'default')."""
        try:
            return (self.agent.capabilities.llm_tier or "default") if self.agent else "default"
        except Exception:
            return "default"

    def _call_llm(self, *, messages, tools=None, max_tokens=2000, tier=None):
        """Unified llm_bridge.call_llm wrapper that always passes an
        agent-LLM fallback. All phase handlers that need LLM go through
        this — avoids duplicating the agent-fallback plumbing.
        """
        from ..bridges import llm_bridge
        prov, mdl = self._agent_llm_fallback()
        return llm_bridge.call_llm(
            messages=messages,
            tools=tools,
            tier=(tier or self._llm_tier()),
            agent_provider=prov,
            agent_model=mdl,
            max_tokens=max_tokens,
        )

    def _agent_llm_fallback(self) -> tuple[str, str]:
        """Return the V1 agent's (provider, model) for call_llm fallback.

        V2 uses tier-based routing, but if the admin hasn't mapped the
        tier (common for tier='default'), we fall back to the agent's
        own LLM binding rather than V1's global config. Returns
        ("","") if no agent or no binding — caller will surface the
        resulting NO_LLM_CONFIGURED error.
        """
        try:
            if not self.agent:
                return ("", "")
            v1_id = getattr(self.agent, "v1_agent_id", "") or self.agent.id
            import sys as _sys
            mod = _sys.modules.get("app.llm")
            hub = getattr(mod, "_active_hub", None) if mod else None
            if hub is None:
                return ("", "")
            v1_agent = hub.get_agent(v1_id) if hasattr(hub, "get_agent") else None
            if v1_agent is None:
                return ("", "")
            return (getattr(v1_agent, "provider", "") or "",
                    getattr(v1_agent, "model", "") or "")
        except Exception:
            return ("", "")

    def _multimodal_supported(self) -> bool:
        """True iff the provider resolved from the agent's tier has
        multimodal support. Never raises — unknown → False (fail-closed)."""
        try:
            from ..bridges.llm_tier_routing import resolve_tier
            provider_id, _ = resolve_tier(self._llm_tier())
            if not provider_id:
                return False
            from app import llm as _llm
            return bool(_llm.get_registry().provider_supports_multimodal(provider_id))
        except Exception:
            return False

    # ── template + LLM helpers ─────────────────────────────────────────

    def _load_template(self) -> dict:
        """Load the YAML template dict. Cached on first call."""
        if self.template:
            return self.template
        from ..templates import loader
        tmpl = loader.get_template(self.task.template_id) or {}
        self.template = tmpl
        return tmpl

    def _llm_extract_slots(
        self,
        template: dict,
        required: list[dict],
        missing: list[str],
    ) -> dict | None:
        """Ask the LLM to fill required slots from ``task.intent``.

        Returns parsed JSON dict or None on parse failure (outer retry fires).
        """
        from ..bridges import llm_bridge

        slot_lines = []
        for s in (template.get("required_slots") or []):
            flag = " (optional)" if s.get("optional") else " (required)"
            dflt = f" default={s['default']!r}" if s.get("default") is not None else ""
            slot_lines.append(
                f"- {s['name']}{flag}{dflt}: {s.get('description', '')}"
            )

        system = (
            "你是任务预处理助手。从用户请求中抽取结构化槽位。\n"
            "只输出 JSON，用 ```json ... ``` 代码块包裹，不要加任何其他说明文字。"
        )
        user = (
            f"# 任务模板\n"
            f"id: {template.get('id', '')}\n"
            f"name: {template.get('display_name', '')}\n\n"
            f"# 需要的槽位\n"
            + "\n".join(slot_lines) + "\n\n"
            f"# 用户请求\n{self.task.intent}\n\n"
            f"# 已填槽位\n{json.dumps(self.task.context.filled_slots, ensure_ascii=False)}\n\n"
            f"# 输出格式\n"
            '```json\n'
            '{\n'
            '  "filled":        {"<slot_name>": "<value>"},\n'
            '  "missing":       ["<slot_name>", ...],\n'
            '  "clarification": "向用户反问的一句话（当有 missing 时）"\n'
            '}\n'
            '```\n'
            f"只抽能明确从用户请求中看出来的槽位。不要猜测。\n"
            f"当前仍未填的 required 槽位：{missing}\n"
        )

        try:
            msg = self._call_llm(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                tools=None,
                max_tokens=800,
            )
        except Exception as e:
            self._emit("phase_error", {
                "phase": TaskPhase.INTAKE.value,
                "error": f"llm_bridge failed: {type(e).__name__}: {e}",
            })
            return None
        return _extract_json(msg.get("content", ""))

    def _llm_generate_plan(self, template: dict) -> dict | None:
        """Ask the LLM to produce a structured Plan JSON."""
        from ..bridges import llm_bridge

        allowed = template.get("allowed_tools") or []
        plan_prompt_tmpl = template.get("plan_prompt") or \
            "请将用户的请求拆解为 1-6 个 step。"
        slots = self.task.context.filled_slots
        # Safe .format — missing keys become empty string, don't crash.
        plan_prompt = _safe_format(plan_prompt_tmpl, slots, intent=self.task.intent)

        # Include last 5 lessons (most recent first) to avoid repeated mistakes.
        lesson_snips: list[str] = []
        for le in sorted(self.task.lessons, key=lambda x: x.last_seen_at, reverse=True)[:5]:
            lesson_snips.append(
                f"- [{le.phase.value}] {le.issue} → {le.fix}"
            )
        lessons_block = "\n".join(lesson_snips) if lesson_snips else "（无）"

        system = (
            "你是一个任务规划器。基于用户意图和模板指引，产出结构化 Plan JSON。\n"
            "只输出 JSON，用 ```json ... ``` 代码块包裹，不要其他文字。"
        )
        user = (
            f"# 意图\n{self.task.intent}\n\n"
            f"# 已填槽位\n{json.dumps(slots, ensure_ascii=False)}\n\n"
            f"# 模板 plan_prompt\n{plan_prompt}\n\n"
            f"# 可用工具\n{allowed if allowed else '不限（由 agent 能力决定）'}\n\n"
            f"# 历史复盘（请避免重犯）\n{lessons_block}\n\n"
            "# 输出格式\n"
            '```json\n'
            '{\n'
            '  "steps": [\n'
            '    {\n'
            '      "id": "s1",\n'
            '      "goal": "用一句话说明该 step 要达成什么",\n'
            '      "tools_hint": ["tool_a"],\n'
            '      "exit_check": {\n'
            '        "type": "tool_used | contains_section | regex | json_schema | artifact_created",\n'
            '        "spec": {"...": "..."}\n'
            '      }\n'
            '    }\n'
            '  ],\n'
            '  "expected_artifact_count": 1\n'
            '}\n'
            '```\n'
            "要求：至少 1 个 step，至多 6 个；每个 step 的 exit_check 必须可机器判断。"
        )

        try:
            msg = self._call_llm(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                tools=None,
                max_tokens=2000,
            )
        except Exception as e:
            self._emit("phase_error", {
                "phase": TaskPhase.PLAN.value,
                "error": f"llm_bridge failed: {type(e).__name__}: {e}",
            })
            return None
        content = msg.get("content", "") or ""
        result = _extract_json(content)

        # Tool-calls fallback: if content is empty but the LLM emitted
        # the plan as a tool_call's arguments (common with Qwen-family
        # models that reflexively wrap structured output in tool_call
        # markers), try to lift the plan out of there.
        if result is None and not content:
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") if isinstance(tc, dict) else None
                if not isinstance(fn, dict):
                    continue
                raw_args = fn.get("arguments")
                if isinstance(raw_args, dict) and "steps" in raw_args:
                    result = raw_args
                    break
                if isinstance(raw_args, str):
                    parsed = _extract_json(raw_args) or None
                    try:
                        if parsed is None:
                            parsed = json.loads(raw_args)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and "steps" in parsed:
                        result = parsed
                        break

        if result is None:
            # Last-resort diagnostic: dump the full msg shape so the
            # user can see exactly what came back.
            tool_calls = msg.get("tool_calls") or []
            other_keys = [k for k in msg.keys()
                          if k not in ("role", "content", "tool_calls")]
            details = {
                "phase": TaskPhase.PLAN.value,
                "error": "could not extract JSON from LLM response",
                "raw_content": content[:600],
                "content_len": len(content),
                "tool_calls_count": len(tool_calls),
                "tool_calls_sample": str(tool_calls)[:400] if tool_calls else "",
                "other_msg_keys": other_keys,
                "msg_sample": str(msg)[:600],
                "hint": ("LLM returned empty content. Likely causes: "
                         "(1) parser stripped a <tool_call> block — check "
                         "tool_parsers.yaml matches this model; (2) context "
                         "window exceeded (40k+ tokens) — see prompt bloat; "
                         "(3) model silently hit max_tokens."
                         if not content and not tool_calls else
                         "LLM emitted tool_calls but their arguments don't "
                         "contain a plan-shaped dict. Check raw below."
                         if not content else
                         "Check that the agent's LLM reliably emits "
                         "```json blocks in response to plan prompts."),
            }
            self._emit("phase_error", details)
        return result


# ── module helpers ─────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)


def _extract_json(text: str) -> dict | None:
    """Tolerant JSON extractor: first try a ```json fenced block, then bare."""
    if not text:
        return None
    m = _FENCE_RE.search(text)
    candidate = m.group(1) if m else None
    if candidate is None:
        # Bare object in the whole text.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _safe_format(tmpl: str, *dicts: dict, **kw) -> str:
    """Format ``{slot}`` placeholders without KeyError on missing keys."""
    merged: dict = {}
    for d in dicts:
        if d:
            merged.update(d)
    merged.update(kw)
    try:
        return tmpl.format_map(_SafeDict(merged))
    except Exception:
        return tmpl


__all__ = ["TaskLoop", "MAX_RETRIES_PER_PHASE"]
