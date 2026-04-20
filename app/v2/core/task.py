"""
Task dataclasses — PRD §6.1.

Design contract (PRD §7.0 铁律 D2):
    Runtime state (messages / artifacts / lessons / plan) lives on the
    Task, not on the Agent. An Agent is a thin capabilities shell; each
    submission creates a fresh Task with its own context.

Methods here are deliberately thin: assignment, append, JSON
serialization. Business logic lives in TaskLoop / TaskExecutor.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class TaskPhase(str, Enum):
    INTAKE = "intake"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"
    DELIVER = "deliver"
    REPORT = "report"
    DONE = "done"


class TaskStatus(str, Enum):
    RUNNING = "running"
    QUEUED = "queued"           # Waiting for the agent to be free
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    ABANDONED = "abandoned"


_PHASE_ORDER = [
    TaskPhase.INTAKE,
    TaskPhase.PLAN,
    TaskPhase.EXECUTE,
    TaskPhase.VERIFY,
    TaskPhase.DELIVER,
    TaskPhase.REPORT,
    TaskPhase.DONE,
]


@dataclass
class PlanStep:
    id: str
    goal: str
    tools_hint: list[str] = field(default_factory=list)
    # exit_check: {"type": "regex|contains_section|json_schema|tool_used|artifact_created",
    #              "spec": {...}}
    exit_check: dict = field(default_factory=dict)
    completed: bool = False
    result_summary: str = ""


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    expected_artifact_count: int = 0
    schema_version: int = 1


@dataclass
class Artifact:
    id: str
    kind: str              # "file" | "email" | "rag_entry" | "api_call" | "message"
    handle: str            # path / email id / rag id / opaque string
    summary: str = ""
    created_at: float = 0.0
    produced_by_tool: str = ""


@dataclass
class Lesson:
    """Failure-reflection record. De-duplicated by dedup_key (PRD §6.1)."""
    id: str
    phase: TaskPhase
    issue: str
    fix: str = ""
    created_at: float = 0.0
    dedup_key: str = ""
    occurrence_count: int = 1
    last_seen_at: float = 0.0


def _compute_lesson_dedup_key(phase: TaskPhase, issue: str) -> str:
    """Stable key for lesson de-dup: sha1(phase + normalized issue prefix)."""
    norm = (issue or "").strip().lower()[:200]
    raw = f"{phase.value}|{norm}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


@dataclass
class TaskContext:
    """Mutable runtime context owned by the Task (replaces V1 agent.messages)."""
    messages: list[dict] = field(default_factory=list)
    filled_slots: dict = field(default_factory=dict)
    clarification_pending: bool = False
    scratch: dict = field(default_factory=dict)
    # Capabilities frozen at Task start (PRD §7.3). Immutable after Intake.
    capabilities_snapshot: dict = field(default_factory=dict)
    # ── Multimodal inputs ──
    # Each attachment is a dict with at least {"kind": "image"|"audio",
    #  "handle": "<path or url>", "mime": "image/png"}. Populated by the
    # REST submit_task endpoint; consumed by Intake (multimodal gate) and
    # later by Executor when it composes messages. Kept empty for pure
    # text tasks so there's no serialisation overhead.
    attachments: list[dict] = field(default_factory=list)


@dataclass
class Task:
    # Identity
    id: str
    agent_id: str
    parent_task_id: str = ""
    template_id: str = ""

    # User-facing
    intent: str = ""

    # State machine
    phase: TaskPhase = TaskPhase.INTAKE
    status: TaskStatus = TaskStatus.RUNNING

    # Runtime controls (PRD §6.1)
    priority: int = 5          # 1 highest .. 10 lowest
    timeout_s: int = 1800      # wall-clock timeout from started_at
    finished_reason: str = ""  # completed | failed | timeout | cancelled | abandoned

    # Runtime state
    plan: Plan = field(default_factory=Plan)
    context: TaskContext = field(default_factory=TaskContext)
    artifacts: list[Artifact] = field(default_factory=list)
    lessons: list[Lesson] = field(default_factory=list)
    retries: dict = field(default_factory=dict)  # {phase_value: int}

    # Timestamps
    created_at: float = 0.0
    started_at: Optional[float] = None
    updated_at: float = 0.0
    completed_at: Optional[float] = None

    # ── thin methods ────────────────────────────────────────────────────

    def advance_phase(self, next_phase: TaskPhase) -> None:
        self.phase = next_phase
        self.updated_at = time.time()

    def record_retry(self, phase: TaskPhase) -> int:
        n = int(self.retries.get(phase.value, 0)) + 1
        self.retries[phase.value] = n
        self.updated_at = time.time()
        return n

    def add_artifact(self, artifact: Artifact) -> None:
        if not artifact.created_at:
            artifact.created_at = time.time()
        self.artifacts.append(artifact)
        self.updated_at = time.time()

    # ── state transitions (PRD §10.2.7) ────────────────────────────────

    def pause(self) -> bool:
        """RUNNING → PAUSED. Returns False if transition illegal."""
        if self.status != TaskStatus.RUNNING:
            return False
        self.status = TaskStatus.PAUSED
        self.updated_at = time.time()
        return True

    def resume(self) -> bool:
        """PAUSED → RUNNING. Caller must restart a TaskLoop thread."""
        if self.status != TaskStatus.PAUSED:
            return False
        self.status = TaskStatus.RUNNING
        self.updated_at = time.time()
        return True

    def cancel(self) -> bool:
        """RUNNING / PAUSED / QUEUED → ABANDONED. Terminal; Report is skipped.

        Cancelling a QUEUED task is cheap (no loop to kill) — the task
        just never runs. Cancelling a RUNNING task lets the current
        TaskLoop iteration finish its current phase handler then exit
        cleanly via the status-check in ``TaskLoop.run``.
        """
        if self.status not in (
            TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.QUEUED,
        ):
            return False
        self.status = TaskStatus.ABANDONED
        self.finished_reason = "cancelled"
        self.phase = TaskPhase.DONE
        self.updated_at = time.time()
        if self.completed_at is None:
            self.completed_at = self.updated_at
        return True

    def accept_clarification(self, answer: str) -> bool:
        """User answered Intake's question. Merge answer into intent and
        resume Intake. Only valid when ``clarification_pending`` is set."""
        if not self.context.clarification_pending:
            return False
        self.context.scratch["clarification_answer"] = answer
        # Append to intent so Intake's slot extraction has more context.
        self.intent = (self.intent + "\n[补充] " + answer).strip()
        self.context.clarification_pending = False
        self.status = TaskStatus.RUNNING
        self.phase = TaskPhase.INTAKE
        self.updated_at = time.time()
        return True

    def add_lesson(self, lesson: Lesson) -> None:
        """De-duplicate by dedup_key (PRD §6.1).

        - Empty dedup_key → computed from phase + issue prefix.
        - Existing key → occurrence_count += 1; last_seen_at refreshed;
          no new list entry.
        - New key → appended.
        """
        if not lesson.dedup_key:
            lesson.dedup_key = _compute_lesson_dedup_key(lesson.phase, lesson.issue)
        now = time.time()
        if not lesson.created_at:
            lesson.created_at = now
        if not lesson.last_seen_at:
            lesson.last_seen_at = now

        for existing in self.lessons:
            if existing.dedup_key == lesson.dedup_key:
                existing.occurrence_count += 1
                existing.last_seen_at = now
                self.updated_at = now
                return

        self.lessons.append(lesson)
        self.updated_at = now

    # ── persistence (PRD §7.2) ──────────────────────────────────────────

    def to_persist_dict(self) -> dict:
        """Flatten to SQLite-safe primitive fields (JSON blobs for composites)."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "parent_task_id": self.parent_task_id,
            "template_id": self.template_id,
            "intent": self.intent,
            "phase": self.phase.value,
            "status": self.status.value,
            "priority": self.priority,
            "timeout_s": self.timeout_s,
            "finished_reason": self.finished_reason,
            "plan_json": _dumps(asdict(self.plan)),
            "context_json": _dumps(asdict(self.context)),
            "artifacts_json": _dumps([asdict(a) for a in self.artifacts]),
            "lessons_json": _dumps([_lesson_to_dict(le) for le in self.lessons]),
            "retries_json": _dumps(self.retries),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_persist_dict(cls, d: dict) -> "Task":
        plan_d = _loads(d.get("plan_json") or "{}")
        ctx_d = _loads(d.get("context_json") or "{}")
        arts = _loads(d.get("artifacts_json") or "[]")
        less = _loads(d.get("lessons_json") or "[]")

        plan = Plan(
            steps=[PlanStep(**s) for s in plan_d.get("steps", [])],
            expected_artifact_count=int(plan_d.get("expected_artifact_count", 0)),
            schema_version=int(plan_d.get("schema_version", 1)),
        )
        context = TaskContext(**ctx_d) if ctx_d else TaskContext()
        artifacts = [Artifact(**a) for a in arts]
        lessons = [_lesson_from_dict(le) for le in less]

        return cls(
            id=d["id"],
            agent_id=d["agent_id"],
            parent_task_id=d.get("parent_task_id", ""),
            template_id=d.get("template_id", ""),
            intent=d.get("intent", ""),
            phase=TaskPhase(d.get("phase", TaskPhase.INTAKE.value)),
            status=TaskStatus(d.get("status", TaskStatus.RUNNING.value)),
            priority=int(d.get("priority", 5)),
            timeout_s=int(d.get("timeout_s", 1800)),
            finished_reason=d.get("finished_reason", ""),
            plan=plan,
            context=context,
            artifacts=artifacts,
            lessons=lessons,
            retries=_loads(d.get("retries_json") or "{}"),
            created_at=float(d.get("created_at") or 0.0),
            started_at=(float(d["started_at"]) if d.get("started_at") is not None else None),
            updated_at=float(d.get("updated_at") or 0.0),
            completed_at=(float(d["completed_at"]) if d.get("completed_at") is not None else None),
        )


# ── helpers ─────────────────────────────────────────────────────────────

import json as _json


def _dumps(obj) -> str:
    return _json.dumps(obj, ensure_ascii=False, default=_json_default)


def _loads(s: str):
    if not s:
        return {}
    return _json.loads(s)


def _json_default(o):
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _lesson_to_dict(le: Lesson) -> dict:
    d = asdict(le)
    d["phase"] = le.phase.value if isinstance(le.phase, TaskPhase) else le.phase
    return d


def _lesson_from_dict(d: dict) -> Lesson:
    return Lesson(
        id=d["id"],
        phase=TaskPhase(d["phase"]) if isinstance(d.get("phase"), str) else d.get("phase"),
        issue=d.get("issue", ""),
        fix=d.get("fix", ""),
        created_at=float(d.get("created_at") or 0.0),
        dedup_key=d.get("dedup_key", ""),
        occurrence_count=int(d.get("occurrence_count", 1)),
        last_seen_at=float(d.get("last_seen_at") or 0.0),
    )


__all__ = [
    "TaskPhase",
    "TaskStatus",
    "PlanStep",
    "Plan",
    "Artifact",
    "Lesson",
    "TaskContext",
    "Task",
]
