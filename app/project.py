"""
Project — 项目组管理模块。

一个 Project 将多个 Agent 组织成一个协作团队：
  - 每个 Agent 有指定的角色职责
  - Agent 之间通过 Project Chat 进行对话
  - 支持任务分配、状态跟踪
  - 可自动驱动 Agent 按职责完成任务
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import logging

logger = logging.getLogger("tudou.project")


# ─────────────────────────────────────────────────────────────
# 项目任务
# ─────────────────────────────────────────────────────────────

class ProjectTaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class TaskStep:
    """Step-level checkpoint within a ProjectTask.

    Steps make tasks resumable at finer granularity than the task itself.
    When a worker is interrupted mid-task, the next runner can call
    `next_pending_step()` and skip every step already marked done.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    # status:
    #   pending          - 等待执行
    #   in_progress      - 执行中
    #   awaiting_review  - agent 已产出草稿，等待人工确认
    #   done             - 完成
    #   failed / skipped - 失败 / 跳过
    status: str = "pending"
    result: str = ""        # agent 产出的草稿或最终结果
    error: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    # 人工审核标记：True = agent 不能自己关闭这个 step，必须由人工 approve
    manual_review: bool = False
    reviewed_by: str = ""   # 审核人 user id（只有 manual_review=True 时填充）
    reviewed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "status": self.status, "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "manual_review": self.manual_review,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "TaskStep":
        return TaskStep(
            id=d.get("id", uuid.uuid4().hex[:8]),
            name=d.get("name", ""),
            status=d.get("status", "pending"),
            result=d.get("result", ""),
            error=d.get("error", ""),
            started_at=float(d.get("started_at", 0.0) or 0.0),
            completed_at=float(d.get("completed_at", 0.0) or 0.0),
            manual_review=bool(d.get("manual_review", False)),
            reviewed_by=d.get("reviewed_by", ""),
            reviewed_at=float(d.get("reviewed_at", 0.0) or 0.0),
        )


@dataclass
class ProjectTask:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    description: str = ""
    status: ProjectTaskStatus = ProjectTaskStatus.TODO
    assigned_to: str = ""          # agent_id
    created_by: str = ""           # "user" or agent_id
    result: str = ""
    priority: int = 0              # 0=normal, 1=high, 2=urgent
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # ── Step-level checkpoints (resumable execution) ──
    steps: list = field(default_factory=list)        # list[TaskStep]
    current_step_index: int = 0
    last_checkpoint_at: float = 0.0
    # Free-form metadata bag for things like pending_approval, etc.
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "assigned_to": self.assigned_to,
            "created_by": self.created_by,
            "result": self.result, "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "current_step_index": self.current_step_index,
            "last_checkpoint_at": self.last_checkpoint_at,
            "metadata": self.metadata or {},
        }

    @staticmethod
    def from_dict(d: dict) -> ProjectTask:
        return ProjectTask(
            id=d.get("id", uuid.uuid4().hex[:10]),
            title=d.get("title", ""),
            description=d.get("description", ""),
            status=ProjectTaskStatus(d.get("status", "todo")),
            assigned_to=d.get("assigned_to", ""),
            created_by=d.get("created_by", ""),
            result=d.get("result", ""),
            priority=d.get("priority", 0),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            steps=[TaskStep.from_dict(s) for s in (d.get("steps") or [])],
            current_step_index=int(d.get("current_step_index", 0) or 0),
            last_checkpoint_at=float(d.get("last_checkpoint_at", 0.0) or 0.0),
            metadata=dict(d.get("metadata") or {}),
        )

    # ── Step-level helpers ──

    def define_steps(self, names):
        """Initialise the step list (no-op if already populated).

        `names` may be either a list of strings (legacy) or a list of dicts
        of the form {"name": str, "manual_review": bool}.
        """
        if self.steps:
            return
        self.steps = []
        for n in names:
            if isinstance(n, dict):
                self.steps.append(TaskStep(
                    name=str(n.get("name", "")),
                    manual_review=bool(n.get("manual_review", False)),
                ))
            else:
                self.steps.append(TaskStep(name=str(n)))
        self.current_step_index = 0

    def next_pending_step(self) -> "TaskStep | None":
        """Return the next step to run (skipping done/skipped)."""
        for i, s in enumerate(self.steps):
            if s.status in ("pending", "in_progress"):
                self.current_step_index = i
                return s
        return None

    def start_step(self, step: "TaskStep"):
        step.status = "in_progress"
        step.started_at = time.time()
        self.last_checkpoint_at = step.started_at
        self.updated_at = step.started_at

    def complete_step(self, step: "TaskStep", result: str = "", error: str = "",
                      by_human: bool = False, reviewer_id: str = ""):
        """Close a step.

        - If `error` is set → status=failed
        - Else if step.manual_review and not by_human → status=awaiting_review
          (agent submitted draft, waiting for human approval; agent CANNOT
          self-close)
        - Else → status=done
        """
        step.completed_at = time.time()
        if error:
            step.status = "failed"
            step.error = error[:1000]
        elif step.manual_review and not by_human:
            # Agent finished its work but cannot self-close — park it.
            step.status = "awaiting_review"
            step.result = result[:2000]
            # Do NOT set reviewed_by/reviewed_at here.
        else:
            step.status = "done"
            if result:
                step.result = result[:2000]
            if by_human:
                step.reviewed_by = reviewer_id
                step.reviewed_at = step.completed_at
        self.last_checkpoint_at = step.completed_at
        self.updated_at = step.completed_at

    def approve_step(self, step: "TaskStep", reviewer_id: str = "user",
                     override_result: str = "") -> bool:
        """Human approval for a manual_review step.

        Only valid when step.status == 'awaiting_review'. Marks the step done
        and stamps the reviewer. Returns False if not in awaiting state.
        """
        if step.status != "awaiting_review":
            return False
        if override_result:
            step.result = override_result[:2000]
        step.status = "done"
        step.reviewed_by = reviewer_id
        step.reviewed_at = time.time()
        step.completed_at = step.reviewed_at
        self.last_checkpoint_at = step.reviewed_at
        self.updated_at = step.reviewed_at
        return True

    def reject_step(self, step: "TaskStep", reviewer_id: str = "user",
                    reason: str = "") -> bool:
        """Human rejection for a manual_review step. Resets to pending so
        the agent can re-run it. Returns False if not in awaiting state."""
        if step.status != "awaiting_review":
            return False
        step.status = "pending"
        step.error = (f"[rejected by {reviewer_id}] {reason}" if reason
                      else f"[rejected by {reviewer_id}]")[:1000]
        step.started_at = 0.0
        step.completed_at = 0.0
        self.last_checkpoint_at = time.time()
        self.updated_at = self.last_checkpoint_at
        return True

    def has_awaiting_review(self) -> bool:
        return any(s.status == "awaiting_review" for s in self.steps)

    def all_steps_done(self) -> bool:
        if not self.steps:
            return False
        return all(s.status in ("done", "skipped") for s in self.steps)

    def step_progress(self) -> tuple[int, int]:
        """(completed_count, total_count). Returns (0, 0) if no steps defined."""
        if not self.steps:
            return (0, 0)
        done = sum(1 for s in self.steps if s.status in ("done", "skipped"))
        return (done, len(self.steps))


# ─────────────────────────────────────────────────────────────
# 项目聊天消息
# ─────────────────────────────────────────────────────────────

@dataclass
class ProjectMessage:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    sender: str = ""               # "user" or agent_id
    sender_name: str = ""          # 显示名称
    sender_role: str = "agent"     # admin / agent / system —— 用于优先级判定
    content: str = ""
    msg_type: str = "chat"         # chat / task_update / system
    task_id: str = ""              # 关联的任务 ID（可选）
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "sender": self.sender,
            "sender_name": self.sender_name,
            "sender_role": self.sender_role,
            "content": self.content,
            "msg_type": self.msg_type,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict) -> ProjectMessage:
        # 兼容旧数据：sender=user 时回填 admin 角色
        sender = d.get("sender", "")
        default_role = "admin" if sender == "user" else (
            "system" if sender == "system" else "agent")
        return ProjectMessage(
            id=d.get("id", uuid.uuid4().hex[:10]),
            sender=sender,
            sender_name=d.get("sender_name", ""),
            sender_role=d.get("sender_role", default_role),
            content=d.get("content", ""),
            msg_type=d.get("msg_type", "chat"),
            task_id=d.get("task_id", ""),
            timestamp=d.get("timestamp", time.time()),
        )


# ─────────────────────────────────────────────────────────────
# 项目成员
# ─────────────────────────────────────────────────────────────

@dataclass
class ProjectMember:
    agent_id: str = ""
    responsibility: str = ""       # 在项目中的职责描述

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "responsibility": self.responsibility,
        }

    @staticmethod
    def from_dict(d: dict) -> ProjectMember:
        return ProjectMember(
            agent_id=d.get("agent_id", ""),
            responsibility=d.get("responsibility", ""),
        )


# ─────────────────────────────────────────────────────────────
# 项目里程碑
# ─────────────────────────────────────────────────────────────

@dataclass
class ProjectMilestone:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    responsible_agent_id: str = ""
    due_date: str = ""  # ISO format date string
    status: str = "pending"  # pending, in_progress, completed, confirmed, rejected
    created_at: float = field(default_factory=time.time)
    # 闭环字段
    confirmed_by: str = ""
    confirmed_at: float = 0.0
    rejected_reason: str = ""
    rejected_at: float = 0.0
    evidence: str = ""  # agent提交的完成证据/说明

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "responsible_agent_id": self.responsible_agent_id,
            "due_date": self.due_date, "status": self.status,
            "created_at": self.created_at,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "rejected_reason": self.rejected_reason,
            "rejected_at": self.rejected_at,
            "evidence": self.evidence,
        }

    @staticmethod
    def from_dict(d: dict) -> "ProjectMilestone":
        return ProjectMilestone(
            id=d.get("id", ""), name=d.get("name", ""),
            responsible_agent_id=d.get("responsible_agent_id", ""),
            due_date=d.get("due_date", ""), status=d.get("status", "pending"),
            created_at=d.get("created_at", 0.0),
            confirmed_by=d.get("confirmed_by", ""),
            confirmed_at=d.get("confirmed_at", 0.0),
            rejected_reason=d.get("rejected_reason", ""),
            rejected_at=d.get("rejected_at", 0.0),
            evidence=d.get("evidence", ""),
        )


# ─────────────────────────────────────────────────────────────
# 目标 / 交付物 / 问题 — 闭环项目所需的三个新维度
# ─────────────────────────────────────────────────────────────

@dataclass
class ProjectGoal:
    """项目目标 — 带可度量指标的顶层目标.

    一个 Project 可以有多个 Goal, 每个 Goal 有 target/current 两个数值
    (或纯文本型, 用 target_text + done 布尔达成)。Milestones 和 Deliverables
    通过 linked_milestone_ids / linked_deliverable_ids 挂到 Goal 上,
    Goal 的 progress 可自动从关联项推导, 也可以手动覆盖。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""
    owner_agent_id: str = ""           # 负责该目标的 agent
    metric: str = "count"              # "count" | "percent" | "boolean" | "text"
    target_value: float = 0.0
    current_value: float = 0.0
    target_text: str = ""              # 用于 boolean/text 型
    done: bool = False                 # 布尔达成
    linked_milestone_ids: list = field(default_factory=list)
    linked_deliverable_ids: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def progress(self) -> float:
        if self.metric == "boolean":
            return 100.0 if self.done else 0.0
        if self.metric in ("count", "percent"):
            if self.target_value <= 0:
                return 0.0
            return round(min(100.0, self.current_value / self.target_value * 100.0), 1)
        return 100.0 if self.done else 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "description": self.description,
            "owner_agent_id": self.owner_agent_id,
            "metric": self.metric,
            "target_value": self.target_value,
            "current_value": self.current_value,
            "target_text": self.target_text,
            "done": self.done,
            "linked_milestone_ids": list(self.linked_milestone_ids),
            "linked_deliverable_ids": list(self.linked_deliverable_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
        }

    @staticmethod
    def from_dict(d: dict) -> "ProjectGoal":
        return ProjectGoal(
            id=d.get("id", ""), name=d.get("name", ""),
            description=d.get("description", ""),
            owner_agent_id=d.get("owner_agent_id", ""),
            metric=d.get("metric", "count"),
            target_value=float(d.get("target_value", 0) or 0),
            current_value=float(d.get("current_value", 0) or 0),
            target_text=d.get("target_text", ""),
            done=bool(d.get("done", False)),
            linked_milestone_ids=list(d.get("linked_milestone_ids", []) or []),
            linked_deliverable_ids=list(d.get("linked_deliverable_ids", []) or []),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )


class DeliverableStatus(str, Enum):
    DRAFT = "draft"              # agent 刚创建, 尚未提交审阅
    SUBMITTED = "submitted"      # agent 提交, 等待人工审阅
    APPROVED = "approved"        # 已通过审阅
    REJECTED = "rejected"        # 被驳回, 需要修改
    ARCHIVED = "archived"        # 归档 (历史版本)


@dataclass
class Deliverable:
    """项目交付物 — 一个可审阅的产出件.

    可以是: 文档 (file_path) / 代码 (code_diff) / 结论 (content_text) / URL.
    kind 区分类型, 便于 UI 分组渲染和链接预览。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    kind: str = "document"       # document | code | analysis | url | media | other
    author_agent_id: str = ""
    task_id: str = ""            # 关联的 ProjectTask (可选)
    milestone_id: str = ""       # 关联的 Milestone (可选)
    content_text: str = ""       # 正文摘要 / 结论 (markdown 友好)
    file_path: str = ""          # 如果是文档/媒体, 存相对项目 working_dir 的路径
    url: str = ""                # 如果是 URL 型
    status: DeliverableStatus = DeliverableStatus.DRAFT
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 审阅字段
    submitted_at: float = 0.0
    reviewed_by: str = ""
    reviewed_at: float = 0.0
    review_comment: str = ""
    # 修订历史
    revision_history: list = field(default_factory=list)   # list[{version,note,timestamp}]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "kind": self.kind,
            "author_agent_id": self.author_agent_id,
            "task_id": self.task_id, "milestone_id": self.milestone_id,
            "content_text": self.content_text,
            "file_path": self.file_path, "url": self.url,
            "status": self.status.value if isinstance(self.status, DeliverableStatus) else str(self.status),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "submitted_at": self.submitted_at,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "review_comment": self.review_comment,
            "revision_history": list(self.revision_history),
        }

    @staticmethod
    def from_dict(d: dict) -> "Deliverable":
        try:
            st = DeliverableStatus(d.get("status", "draft"))
        except ValueError:
            st = DeliverableStatus.DRAFT
        return Deliverable(
            id=d.get("id", ""), title=d.get("title", ""),
            kind=d.get("kind", "document"),
            author_agent_id=d.get("author_agent_id", ""),
            task_id=d.get("task_id", ""),
            milestone_id=d.get("milestone_id", ""),
            content_text=d.get("content_text", ""),
            file_path=d.get("file_path", ""),
            url=d.get("url", ""),
            status=st,
            version=int(d.get("version", 1) or 1),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            submitted_at=float(d.get("submitted_at", 0) or 0),
            reviewed_by=d.get("reviewed_by", ""),
            reviewed_at=float(d.get("reviewed_at", 0) or 0),
            review_comment=d.get("review_comment", ""),
            revision_history=list(d.get("revision_history", []) or []),
        )


@dataclass
class ProjectIssue:
    """项目过程中遇到的问题 / 风险 / 阻塞项 — 需要被跟踪到闭环."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    severity: str = "medium"         # low | medium | high | critical
    status: str = "open"             # open | investigating | resolved | wontfix
    reporter: str = ""               # user id 或 agent id
    assigned_to: str = ""            # agent id
    related_task_id: str = ""
    related_milestone_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    resolution: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description,
            "severity": self.severity, "status": self.status,
            "reporter": self.reporter,
            "assigned_to": self.assigned_to,
            "related_task_id": self.related_task_id,
            "related_milestone_id": self.related_milestone_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
        }

    @staticmethod
    def from_dict(d: dict) -> "ProjectIssue":
        return ProjectIssue(
            id=d.get("id", ""), title=d.get("title", ""),
            description=d.get("description", ""),
            severity=d.get("severity", "medium"),
            status=d.get("status", "open"),
            reporter=d.get("reporter", ""),
            assigned_to=d.get("assigned_to", ""),
            related_task_id=d.get("related_task_id", ""),
            related_milestone_id=d.get("related_milestone_id", ""),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            resolved_at=float(d.get("resolved_at", 0) or 0),
            resolution=d.get("resolution", ""),
        )


# ─────────────────────────────────────────────────────────────
# 项目
# ─────────────────────────────────────────────────────────────

class ProjectStatus(str, Enum):
    PLANNING = "planning"     # 未开始：立项 / 规划中，尚未启动
    ACTIVE = "active"         # 进行中
    SUSPENDED = "suspended"   # 挂起：临时停工，保留上下文，计划稍后继续
    CANCELLED = "cancelled"   # 停止：终止（不再继续，但非正常结束）
    COMPLETED = "completed"   # 结束：正常完成
    ARCHIVED = "archived"     # 归档：从主视图隐藏


@dataclass
class WorkflowBinding:
    """记录项目与 Workflow 模板的绑定关系和步骤 → Agent 分配。"""
    workflow_id: str = ""                # WorkflowTemplate ID
    instance_id: str = ""                # WorkflowInstance ID (执行时创建)
    step_assignments: list[dict] = field(default_factory=list)
    # [{step_index: int, step_id: str, agent_id: str}]

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "instance_id": self.instance_id,
            "step_assignments": self.step_assignments,
        }

    @staticmethod
    def from_dict(d: dict) -> "WorkflowBinding":
        if not d:
            return WorkflowBinding()
        return WorkflowBinding(
            workflow_id=d.get("workflow_id", ""),
            instance_id=d.get("instance_id", ""),
            step_assignments=d.get("step_assignments", []),
        )


@dataclass
class Project:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    members: list[ProjectMember] = field(default_factory=list)
    tasks: list[ProjectTask] = field(default_factory=list)
    chat_history: list[ProjectMessage] = field(default_factory=list)
    milestones: list[ProjectMilestone] = field(default_factory=list)
    # ── Goals / Deliverables / Issues (Project-centric view) ──
    goals: list[ProjectGoal] = field(default_factory=list)
    deliverables: list[Deliverable] = field(default_factory=list)
    issues: list[ProjectIssue] = field(default_factory=list)
    working_directory: str = ""  # Node working directory
    node_id: str = "local"  # Which node this project runs on
    # ── Workflow 绑定 ──
    workflow_binding: WorkflowBinding = field(default_factory=WorkflowBinding)
    # ── Pause / Admin override ──
    paused: bool = False
    paused_at: float = 0.0
    paused_by: str = ""
    paused_reason: str = ""
    paused_queue: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── Lifecycle status transitions ──
    def set_status(self, new_status: str, by: str = "user", reason: str = "") -> tuple[bool, str]:
        """Transition project lifecycle status.

        Allowed values: planning | active | suspended | cancelled | completed | archived
        Returns (ok, message). Idempotent: same-status is a no-op success.
        """
        try:
            target = ProjectStatus(new_status)
        except ValueError:
            return False, f"invalid status: {new_status}"

        with self._lock:
            old = self.status
            if isinstance(old, ProjectStatus):
                old_val = old.value
            else:
                _s = str(old or "").strip().lower()
                if _s.startswith("projectstatus."):
                    _s = _s.split(".", 1)[1]
                old_val = _s or "unknown"
            self.status = target
            self.updated_at = time.time()

            # Keep the chat-level `paused` flag aligned with lifecycle state so
            # existing pause/resume plumbing still works. suspended/cancelled/
            # completed/archived all stop the chat loop; planning/active don't.
            if target in (ProjectStatus.SUSPENDED, ProjectStatus.CANCELLED,
                          ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED):
                if not self.paused:
                    self.paused = True
                    self.paused_at = time.time()
                    self.paused_by = by
                    self.paused_reason = reason or f"status→{target.value}"
            elif target in (ProjectStatus.PLANNING, ProjectStatus.ACTIVE):
                if self.paused:
                    self.paused = False
                    self.paused_at = 0.0
                    self.paused_by = ""
                    self.paused_reason = ""

        return True, f"{old_val} → {target.value}"

    # ── Pause / Resume + 暂停期间消息队列 ──
    def pause(self, by: str = "user", reason: str = "") -> None:
        with self._lock:
            self.paused = True
            self.paused_at = time.time()
            self.paused_by = by
            self.paused_reason = reason
            self.updated_at = time.time()

    def resume(self, by: str = "user") -> None:
        with self._lock:
            self.paused = False
            self.paused_at = 0.0
            self.paused_by = ""
            self.paused_reason = ""
            self.updated_at = time.time()

    def queue_paused_message(self, sender: str, sender_name: str,
                              content: str, target_agents: list[str] | None = None) -> None:
        """暂停期间收到的消息先入队，恢复时回放。"""
        if not hasattr(self, "paused_queue") or self.paused_queue is None:
            self.paused_queue = []
        self.paused_queue.append({
            "sender": sender, "sender_name": sender_name,
            "content": content,
            "target_agents": list(target_agents) if target_agents else None,
            "queued_at": time.time(),
        })

    def drain_paused_queue(self) -> list[dict]:
        """恢复后取出排队消息。"""
        q = getattr(self, "paused_queue", None) or []
        self.paused_queue = []
        return q

    # ── 成员管理 ──

    def add_member(self, agent_id: str, responsibility: str = "") -> ProjectMember:
        m = ProjectMember(agent_id=agent_id, responsibility=responsibility)
        with self._lock:
            # 避免重复添加
            for existing in self.members:
                if existing.agent_id == agent_id:
                    existing.responsibility = responsibility
                    return existing
            self.members.append(m)
            self.updated_at = time.time()
        return m

    def remove_member(self, agent_id: str) -> bool:
        with self._lock:
            before = len(self.members)
            self.members = [m for m in self.members if m.agent_id != agent_id]
            self.updated_at = time.time()
            return len(self.members) < before

    def get_member_ids(self) -> list[str]:
        return [m.agent_id for m in self.members]

    # ── 里程碑管理 ──

    def add_milestone(self, name: str, responsible_agent_id: str = "",
                     due_date: str = "") -> ProjectMilestone:
        milestone = ProjectMilestone(
            name=name, responsible_agent_id=responsible_agent_id,
            due_date=due_date,
        )
        with self._lock:
            self.milestones.append(milestone)
            self.updated_at = time.time()
        return milestone

    def update_milestone(self, milestone_id: str, **kwargs) -> ProjectMilestone | None:
        with self._lock:
            for m in self.milestones:
                if m.id == milestone_id:
                    for k, v in kwargs.items():
                        if hasattr(m, k) and v is not None:
                            setattr(m, k, v)
                    self.updated_at = time.time()
                    return m
        return None

    def confirm_milestone(self, milestone_id: str, by: str = "admin") -> ProjectMilestone | None:
        with self._lock:
            for m in self.milestones:
                if m.id == milestone_id:
                    m.status = "confirmed"
                    m.confirmed_by = by
                    m.confirmed_at = time.time()
                    m.rejected_reason = ""
                    self.updated_at = time.time()
                    return m
        return None

    def reject_milestone(self, milestone_id: str, reason: str = "",
                          by: str = "admin") -> ProjectMilestone | None:
        with self._lock:
            for m in self.milestones:
                if m.id == milestone_id:
                    m.status = "rejected"
                    m.rejected_reason = reason
                    m.rejected_at = time.time()
                    m.confirmed_by = by  # 谁拒绝的
                    self.updated_at = time.time()
                    return m
        return None

    def remove_milestone(self, milestone_id: str) -> bool:
        with self._lock:
            before = len(self.milestones)
            self.milestones = [m for m in self.milestones if m.id != milestone_id]
            self.updated_at = time.time()
            return len(self.milestones) < before

    # ── Goals / Deliverables / Issues CRUD ──

    def add_goal(self, name: str, description: str = "",
                 owner_agent_id: str = "", metric: str = "count",
                 target_value: float = 0.0, target_text: str = "") -> ProjectGoal:
        g = ProjectGoal(
            name=name, description=description,
            owner_agent_id=owner_agent_id, metric=metric,
            target_value=float(target_value or 0),
            target_text=target_text or "",
        )
        with self._lock:
            self.goals.append(g)
            self.updated_at = time.time()
        return g

    def update_goal(self, goal_id: str, **kwargs) -> ProjectGoal | None:
        with self._lock:
            for g in self.goals:
                if g.id == goal_id:
                    for k, v in kwargs.items():
                        if hasattr(g, k) and v is not None:
                            setattr(g, k, v)
                    g.updated_at = time.time()
                    self.updated_at = time.time()
                    return g
        return None

    def update_goal_progress(self, goal_id: str, current_value: float | None = None,
                              done: bool | None = None) -> ProjectGoal | None:
        with self._lock:
            for g in self.goals:
                if g.id == goal_id:
                    if current_value is not None:
                        g.current_value = float(current_value)
                    if done is not None:
                        g.done = bool(done)
                    g.updated_at = time.time()
                    self.updated_at = time.time()
                    return g
        return None

    def remove_goal(self, goal_id: str) -> bool:
        with self._lock:
            before = len(self.goals)
            self.goals = [g for g in self.goals if g.id != goal_id]
            self.updated_at = time.time()
            return len(self.goals) < before

    def add_deliverable(self, title: str, kind: str = "document",
                         author_agent_id: str = "", task_id: str = "",
                         milestone_id: str = "", content_text: str = "",
                         file_path: str = "", url: str = "") -> Deliverable:
        dv = Deliverable(
            title=title, kind=kind, author_agent_id=author_agent_id,
            task_id=task_id, milestone_id=milestone_id,
            content_text=content_text, file_path=file_path, url=url,
        )
        with self._lock:
            self.deliverables.append(dv)
            self.updated_at = time.time()
        return dv

    def submit_deliverable(self, deliverable_id: str) -> Deliverable | None:
        with self._lock:
            for dv in self.deliverables:
                if dv.id == deliverable_id:
                    dv.status = DeliverableStatus.SUBMITTED
                    dv.submitted_at = time.time()
                    dv.updated_at = time.time()
                    self.updated_at = time.time()
                    return dv
        return None

    def review_deliverable(self, deliverable_id: str, approved: bool,
                            reviewer: str = "", comment: str = "") -> Deliverable | None:
        with self._lock:
            for dv in self.deliverables:
                if dv.id == deliverable_id:
                    dv.status = DeliverableStatus.APPROVED if approved else DeliverableStatus.REJECTED
                    dv.reviewed_by = reviewer
                    dv.reviewed_at = time.time()
                    dv.review_comment = comment
                    dv.updated_at = time.time()
                    if not approved:
                        # bump version slot so next submission is v+1
                        dv.revision_history.append({
                            "version": dv.version,
                            "note": f"rejected: {comment}",
                            "timestamp": time.time(),
                        })
                        dv.version += 1
                    self.updated_at = time.time()
                    return dv
        return None

    def update_deliverable(self, deliverable_id: str, **kwargs) -> Deliverable | None:
        with self._lock:
            for dv in self.deliverables:
                if dv.id == deliverable_id:
                    for k, v in kwargs.items():
                        if k == "status":
                            continue  # status goes through submit/review
                        if hasattr(dv, k) and v is not None:
                            setattr(dv, k, v)
                    dv.updated_at = time.time()
                    self.updated_at = time.time()
                    return dv
        return None

    def remove_deliverable(self, deliverable_id: str) -> bool:
        with self._lock:
            before = len(self.deliverables)
            self.deliverables = [dv for dv in self.deliverables if dv.id != deliverable_id]
            self.updated_at = time.time()
            return len(self.deliverables) < before

    def add_issue(self, title: str, description: str = "",
                   severity: str = "medium", reporter: str = "",
                   assigned_to: str = "", related_task_id: str = "",
                   related_milestone_id: str = "") -> ProjectIssue:
        iss = ProjectIssue(
            title=title, description=description, severity=severity,
            reporter=reporter, assigned_to=assigned_to,
            related_task_id=related_task_id,
            related_milestone_id=related_milestone_id,
        )
        with self._lock:
            self.issues.append(iss)
            self.updated_at = time.time()
        return iss

    def update_issue(self, issue_id: str, **kwargs) -> ProjectIssue | None:
        with self._lock:
            for iss in self.issues:
                if iss.id == issue_id:
                    for k, v in kwargs.items():
                        if hasattr(iss, k) and v is not None:
                            setattr(iss, k, v)
                    iss.updated_at = time.time()
                    self.updated_at = time.time()
                    return iss
        return None

    def resolve_issue(self, issue_id: str, resolution: str = "",
                       status: str = "resolved") -> ProjectIssue | None:
        with self._lock:
            for iss in self.issues:
                if iss.id == issue_id:
                    iss.status = status
                    iss.resolution = resolution
                    iss.resolved_at = time.time()
                    iss.updated_at = time.time()
                    self.updated_at = time.time()
                    return iss
        return None

    def remove_issue(self, issue_id: str) -> bool:
        with self._lock:
            before = len(self.issues)
            self.issues = [iss for iss in self.issues if iss.id != issue_id]
            self.updated_at = time.time()
            return len(self.issues) < before

    # ── Workflow 绑定与任务自动生成 ──

    def bind_workflow(self, workflow_template: dict,
                      step_assignments: list[dict]) -> list[ProjectTask]:
        """
        绑定 Workflow 模板到项目，并根据步骤自动生成 ProjectTask。

        Args:
            workflow_template: WorkflowTemplate.to_dict() 格式
            step_assignments:  [{step_index: int, agent_id: str}, ...]

        Returns:
            自动创建的 ProjectTask 列表
        """
        wf_id = workflow_template.get("id", "")
        wf_steps = workflow_template.get("steps", [])

        # 构建 step_index → agent_id 映射
        agent_map = {}
        for sa in step_assignments:
            idx = sa.get("step_index")
            aid = sa.get("agent_id", "")
            if idx is not None and aid:
                agent_map[idx] = aid

        # 更新 binding
        binding_assignments = []
        created_tasks = []
        with self._lock:
            self.workflow_binding = WorkflowBinding(
                workflow_id=wf_id,
                step_assignments=[],
            )

            for i, step in enumerate(wf_steps):
                agent_id = agent_map.get(i, "")
                step_id = step.get("id", "")
                binding_assignments.append({
                    "step_index": i,
                    "step_id": step_id,
                    "agent_id": agent_id,
                })

                # 创建对应的 ProjectTask
                step_name = step.get("name", f"Step {i + 1}")
                step_desc = step.get("description", "")
                input_spec = step.get("input_spec", step.get("input_desc", ""))
                output_spec = step.get("output_spec", step.get("output_desc", ""))

                task_desc_parts = []
                if step_desc:
                    task_desc_parts.append(step_desc)
                if input_spec:
                    task_desc_parts.append(f"Input: {input_spec}")
                if output_spec:
                    task_desc_parts.append(f"Expected output: {output_spec}")

                task = ProjectTask(
                    title=f"[WF Step {i + 1}] {step_name}",
                    description="\n".join(task_desc_parts),
                    assigned_to=agent_id,
                    created_by="workflow",
                    priority=0,
                )
                self.tasks.append(task)
                created_tasks.append(task)

            self.workflow_binding.step_assignments = binding_assignments
            self.updated_at = time.time()

        # 系统消息
        self.post_message(
            sender="system", sender_name="System",
            content=f"Workflow '{workflow_template.get('name', '')}' bound to project. "
                    f"{len(created_tasks)} tasks auto-created.",
            msg_type="system",
        )

        # 自动添加已分配的 Agent 为项目成员
        for sa in step_assignments:
            aid = sa.get("agent_id", "")
            if aid:
                self.add_member(aid, responsibility="Workflow step executor")

        return created_tasks

    def get_workflow_progress(self) -> dict | None:
        """获取 Workflow 在本项目中的执行进度（基于 task 状态）。"""
        if not self.workflow_binding.workflow_id:
            return None
        wf_tasks = [t for t in self.tasks if t.title.startswith("[WF Step")]
        total = len(wf_tasks)
        done = sum(1 for t in wf_tasks if t.status == ProjectTaskStatus.DONE)
        in_progress = sum(1 for t in wf_tasks if t.status == ProjectTaskStatus.IN_PROGRESS)
        return {
            "workflow_id": self.workflow_binding.workflow_id,
            "instance_id": self.workflow_binding.instance_id,
            "total_steps": total,
            "done": done,
            "in_progress": in_progress,
            "progress_pct": int(done / total * 100) if total > 0 else 0,
        }

    # ── 任务管理 ──

    def add_task(self, title: str, description: str = "",
                 assigned_to: str = "", created_by: str = "user",
                 priority: int = 0) -> ProjectTask:
        task = ProjectTask(
            title=title, description=description,
            assigned_to=assigned_to, created_by=created_by,
            priority=priority,
        )
        with self._lock:
            self.tasks.append(task)
            self.updated_at = time.time()
        # 自动发一条系统消息
        self.post_message(
            sender=created_by,
            sender_name=created_by,
            content=f"📋 新任务: {title}" + (f" → 分配给 {assigned_to}" if assigned_to else ""),
            msg_type="task_update",
            task_id=task.id,
        )
        return task

    def update_task(self, task_id: str, **kwargs) -> ProjectTask | None:
        with self._lock:
            for t in self.tasks:
                if t.id == task_id:
                    for k, v in kwargs.items():
                        if hasattr(t, k) and v is not None:
                            if k == "status":
                                setattr(t, k, ProjectTaskStatus(v))
                            else:
                                setattr(t, k, v)
                    t.updated_at = time.time()
                    self.updated_at = time.time()
                    return t
        return None

    def get_task(self, task_id: str) -> ProjectTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def list_tasks(self, status: str = "", assigned_to: str = "") -> list[ProjectTask]:
        result = self.tasks
        if status:
            result = [t for t in result if t.status.value == status]
        if assigned_to:
            result = [t for t in result if t.assigned_to == assigned_to]
        return result

    # ── 聊天 ──

    def post_message(self, sender: str, sender_name: str, content: str,
                     msg_type: str = "chat",
                     task_id: str = "",
                     sender_role: str = "") -> ProjectMessage:
        # 默认推断角色：user→admin, system→system, 其它→agent
        if not sender_role:
            if sender == "user":
                sender_role = "admin"
            elif sender == "system":
                sender_role = "system"
            else:
                sender_role = "agent"
        msg = ProjectMessage(
            sender=sender, sender_name=sender_name,
            sender_role=sender_role,
            content=content, msg_type=msg_type,
            task_id=task_id,
        )
        with self._lock:
            self.chat_history.append(msg)
            # 保留最近 500 条
            if len(self.chat_history) > 500:
                self.chat_history = self.chat_history[-400:]
            self.updated_at = time.time()
        return msg

    def get_chat_history(self, limit: int = 50,
                         since: float = 0) -> list[ProjectMessage]:
        with self._lock:
            msgs = self.chat_history
            if since > 0:
                msgs = [m for m in msgs if m.timestamp > since]
            return msgs[-limit:]

    def get_chat_context_for_agent(self, agent_id: str,
                                    limit: int = 20) -> str:
        """构建给 Agent 看的聊天上下文摘要。"""
        msgs = self.get_chat_history(limit=limit)
        if not msgs:
            return "(项目聊天暂无消息)"
        lines = []
        for m in msgs:
            name = m.sender_name or m.sender
            lines.append(f"[{name}]: {m.content}")
        return "\n".join(lines)

    # ── 序列化 ──

    def to_dict(self) -> dict:
        # Defensive: legacy projects may have raw-string status
        if isinstance(self.status, ProjectStatus):
            _status_str = self.status.value
        else:
            _s = str(self.status or "active").strip().lower()
            # Handle "ProjectStatus.ACTIVE" repr leak
            if _s.startswith("projectstatus."):
                _s = _s.split(".", 1)[1]
            _status_str = _s
        # Projects created before the shared-dir autopopulate fix have an
        # empty working_directory. Fall back to the canonical shared path so
        # the UI (project sidebar, chat header, etc.) always shows something
        # concrete. Lazy import to avoid a circular project<->agent import.
        _wd = self.working_directory
        if not _wd:
            try:
                from .agent import Agent as _Agent
                _wd = _Agent.get_shared_workspace_path(self.id)
            except Exception:
                _wd = ""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": _status_str,
            "members": [m.to_dict() for m in self.members],
            "tasks": [t.to_dict() for t in self.tasks],
            "milestones": [m.to_dict() for m in self.milestones],
            "goals": [g.to_dict() for g in self.goals],
            "deliverables": [dv.to_dict() for dv in self.deliverables],
            "issues": [iss.to_dict() for iss in self.issues],
            "goal_summary": {
                "total": len(self.goals),
                "done": sum(1 for g in self.goals if g.progress >= 100.0),
                "avg_progress": (
                    round(sum(g.progress for g in self.goals) / len(self.goals), 1)
                    if self.goals else 0.0
                ),
            },
            "deliverable_summary": {
                "total": len(self.deliverables),
                "draft": sum(1 for dv in self.deliverables if dv.status == DeliverableStatus.DRAFT),
                "submitted": sum(1 for dv in self.deliverables if dv.status == DeliverableStatus.SUBMITTED),
                "approved": sum(1 for dv in self.deliverables if dv.status == DeliverableStatus.APPROVED),
                "rejected": sum(1 for dv in self.deliverables if dv.status == DeliverableStatus.REJECTED),
            },
            "issue_summary": {
                "total": len(self.issues),
                "open": sum(1 for iss in self.issues if iss.status in ("open", "investigating")),
                "resolved": sum(1 for iss in self.issues if iss.status == "resolved"),
            },
            "working_directory": _wd,
            "node_id": self.node_id,
            "workflow_binding": self.workflow_binding.to_dict() if self.workflow_binding.workflow_id else None,
            "paused": self.paused,
            "paused_at": self.paused_at,
            "paused_by": self.paused_by,
            "paused_reason": self.paused_reason,
            "paused_queue_count": len(getattr(self, "paused_queue", []) or []),
            "chat_count": len(self.chat_history),
            "task_summary": {
                "total": len(self.tasks),
                "todo": sum(1 for t in self.tasks
                            if t.status == ProjectTaskStatus.TODO),
                "in_progress": sum(1 for t in self.tasks
                                   if t.status == ProjectTaskStatus.IN_PROGRESS),
                "done": sum(1 for t in self.tasks
                            if t.status == ProjectTaskStatus.DONE),
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_persist_dict(self) -> dict:
        """持久化用（包含聊天记录）。"""
        d = self.to_dict()
        d["chat_history"] = [m.to_dict() for m in self.chat_history[-200:]]
        d["milestones"] = [m.to_dict() for m in self.milestones]
        d["goals"] = [g.to_dict() for g in self.goals]
        d["deliverables"] = [dv.to_dict() for dv in self.deliverables]
        d["issues"] = [iss.to_dict() for iss in self.issues]
        d["working_directory"] = self.working_directory
        d["node_id"] = self.node_id
        d["workflow_binding"] = self.workflow_binding.to_dict()
        d["paused_queue"] = list(getattr(self, "paused_queue", []) or [])
        return d

    # ── Markdown 导出 ──

    def to_markdown(self, agent_lookup=None) -> str:
        """Export project as a readable Markdown document.

        *agent_lookup* is an optional callable(agent_id) → Agent that
        provides agent names for display.
        """
        def _agent_name(aid: str) -> str:
            if agent_lookup:
                a = agent_lookup(aid)
                if a:
                    return getattr(a, "name", aid)
            return aid

        lines = [f"# {self.name}", ""]
        if self.description:
            lines += [self.description, ""]
        lines += [f"**Status:** {self.status.value}", ""]

        # Members
        if self.members:
            lines += ["## Members", ""]
            for m in self.members:
                resp = f" — {m.responsibility}" if m.responsibility else ""
                lines.append(f"- **{_agent_name(m.agent_id)}**{resp}")
            lines.append("")

        # Tasks
        if self.tasks:
            lines += ["## Tasks", ""]
            status_icons = {"todo": "[ ]", "in_progress": "[~]", "done": "[x]",
                            "blocked": "[!]", "cancelled": "[-]"}
            for t in self.tasks:
                icon = status_icons.get(t.status.value, "[ ]")
                assignee = f" @{_agent_name(t.assigned_to)}" if t.assigned_to else ""
                lines.append(f"- {icon} **{t.title}**{assignee}")
                if t.description:
                    lines.append(f"  {t.description}")
                if t.result:
                    lines.append(f"  > Result: {t.result}")
            lines.append("")

        return "\n".join(lines)

    def agent_context(self, agent_id: str, agent_lookup=None) -> str:
        """Return a concise context string for a specific agent.

        This tells the agent what project it belongs to and which tasks
        are assigned to it.
        """
        def _agent_name(aid: str) -> str:
            if agent_lookup:
                a = agent_lookup(aid)
                if a:
                    return getattr(a, "name", aid)
            return aid

        # Find this agent's membership
        member = None
        for m in self.members:
            if m.agent_id == agent_id:
                member = m
                break
        if not member:
            return ""

        lines = [f"[Project: {self.name}]"]
        if member.responsibility:
            lines.append(f"Your responsibility: {member.responsibility}")

        # Agent's tasks
        my_tasks = [t for t in self.tasks if t.assigned_to == agent_id]
        if my_tasks:
            lines.append("Your tasks:")
            for t in my_tasks:
                lines.append(f"  - [{t.status.value}] {t.title}")
                if t.description:
                    lines.append(f"    {t.description}")

        # Team awareness
        teammates = [m for m in self.members if m.agent_id != agent_id]
        if teammates:
            names = [f"{_agent_name(m.agent_id)}({m.responsibility})"
                     if m.responsibility else _agent_name(m.agent_id)
                     for m in teammates]
            lines.append(f"Team: {', '.join(names)}")

        return "\n".join(lines)

    def save_markdown(self, data_dir: str, agent_lookup=None):
        """Save project markdown to data_dir and each member's workspace."""
        import os, re
        md = self.to_markdown(agent_lookup=agent_lookup)
        safe_name = re.sub(r'[^\w\u4e00-\u9fff-]', '_', self.name).strip('_')
        filename = f"{safe_name}_Project.md"

        # Save to global data dir
        os.makedirs(data_dir, exist_ok=True)
        projects_dir = os.path.join(data_dir, "projects")
        os.makedirs(projects_dir, exist_ok=True)
        with open(os.path.join(projects_dir, filename), "w",
                  encoding="utf-8") as f:
            f.write(md)

        # Save agent-specific context to each member's workspace
        for m in self.members:
            ws = os.path.join(data_dir, "workspaces", m.agent_id)
            os.makedirs(ws, exist_ok=True)
            ctx = self.agent_context(m.agent_id, agent_lookup=agent_lookup)
            with open(os.path.join(ws, filename), "w",
                      encoding="utf-8") as f:
                f.write(ctx + "\n\n---\n\n" + md)

    @staticmethod
    def from_persist_dict(d: dict) -> Project:
        p = Project(
            id=d.get("id", uuid.uuid4().hex[:10]),
            name=d.get("name", ""),
            description=d.get("description", ""),
            status=ProjectStatus(d.get("status", "active")),
            working_directory=d.get("working_directory", ""),
            node_id=d.get("node_id", "local"),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )
        p.paused = bool(d.get("paused", False))
        p.paused_at = float(d.get("paused_at", 0.0) or 0.0)
        p.paused_by = d.get("paused_by", "") or ""
        p.paused_reason = d.get("paused_reason", "") or ""
        p.paused_queue = list(d.get("paused_queue", []) or [])
        p.members = [ProjectMember.from_dict(m) for m in d.get("members", [])]
        p.tasks = [ProjectTask.from_dict(t) for t in d.get("tasks", [])]
        p.milestones = [ProjectMilestone.from_dict(m) for m in d.get("milestones", [])]
        p.goals = [ProjectGoal.from_dict(g) for g in d.get("goals", [])]
        p.deliverables = [Deliverable.from_dict(dv) for dv in d.get("deliverables", [])]
        p.issues = [ProjectIssue.from_dict(iss) for iss in d.get("issues", [])]
        p.chat_history = [ProjectMessage.from_dict(m)
                          for m in d.get("chat_history", [])]
        wb = d.get("workflow_binding")
        if wb:
            p.workflow_binding = WorkflowBinding.from_dict(wb)
        return p


# ─────────────────────────────────────────────────────────────
# 项目聊天引擎 — 驱动 Agent 在 Project Chat 中对话
# ─────────────────────────────────────────────────────────────

class ProjectChatEngine:
    """
    驱动项目聊天：
    1. 用户发消息到项目群聊
    2. 根据消息内容或 @agent 决定哪个/哪些 Agent 回复
    3. Agent 回复时能看到聊天上下文 + 自己的职责
    4. Agent 回复自动发回群聊
    """

    def __init__(self, agent_chat_fn: Callable[[str, str], str],
                 agent_lookup_fn: Callable[[str], Any],
                 save_fn: Callable[[], None] | None = None):
        """
        agent_chat_fn(agent_id, prompt) -> str
        agent_lookup_fn(agent_id) -> Agent or None
        save_fn() -> None  # called after state changes to persist
        """
        self._chat = agent_chat_fn
        self._lookup = agent_lookup_fn
        self._save = save_fn or (lambda: None)

    def handle_user_message(self, project: Project, content: str,
                             target_agents: list[str] | None = None):
        """
        处理用户在项目群聊中的消息。
        target_agents: 指定回复的 Agent 列表，None 则自动决定。
        """
        # 记录用户消息
        project.post_message(
            sender="user", sender_name="User",
            content=content, msg_type="chat",
        )
        logger.info("Project chat [%s] user msg: %s", project.name,
                     content[:80])

        # ── Admin 优先级最高: 检测暂停/继续指令 ──
        # 用户的消息直接来自 admin，优先级高于所有 agent。
        # 暂停指令会立刻阻止 workflow 自动推进，并通知群聊。
        pause_kw = ("暂停", "停一下", "停下", "停止", "先停", "先暂停",
                    "pause", "stop", "halt", "hold on")
        resume_kw = ("继续", "恢复", "重启", "再继续", "接着干", "继续干",
                     "resume", "continue", "unpause", "go on", "proceed")
        clow = content.lower().strip()
        is_pause = any(kw in clow for kw in pause_kw) and not any(
            kw in clow for kw in resume_kw)
        is_resume = any(kw in clow for kw in resume_kw) and not is_pause

        if is_pause and not project.paused:
            project.pause(by="admin", reason=content[:200])
            project.post_message(
                sender="system", sender_name="System",
                content=("⏸️ 项目已被管理员暂停。所有 Workflow 自动推进已停止，"
                         "Agent 不会再被自动唤醒。发送「继续」可恢复。"),
                msg_type="system",
            )
            logger.info("Project [%s] PAUSED by admin: %s",
                        project.name, content[:80])
            try:
                self._save()
            except Exception:
                pass
            # 关键：暂停指令不再触发任何 agent 响应。
            # 否则被 @ 的 agent 还会调 LLM 回一段"收到"，浪费 token；
            # 并且 admin 的本意是让所有 agent 停下，不是让某一个解释。
            return []
        elif is_resume and project.paused:
            project.resume(by="admin")
            queued = project.drain_paused_queue()
            project.post_message(
                sender="system", sender_name="System",
                content=(f"▶️ 项目已恢复。" +
                         (f"重放暂停期间的 {len(queued)} 条消息..." if queued
                          else "Workflow 可以继续推进。")),
                msg_type="system",
            )
            logger.info("Project [%s] RESUMED by admin (queued=%d)",
                        project.name, len(queued))
            try:
                self._save()
            except Exception:
                pass

            # 1) 重放暂停期间排队的消息
            for q in queued:
                try:
                    self.handle_user_message(
                        project, q.get("content", ""),
                        target_agents=q.get("target_agents"))
                except Exception as e:
                    logger.warning("Replay queued msg failed: %s", e)

            # 2) 自动唤醒所有有未完成任务的成员
            try:
                self._resume_auto_wake(project)
            except Exception as e:
                logger.warning("Resume auto-wake failed: %s", e)
            return []

        # 解析 @mentions
        mentioned = self._parse_mentions(content, project)
        respondents = target_agents or mentioned or self._auto_select(
            content, project)

        logger.info("Project chat [%s] mentioned=%s target=%s respondents=%s",
                     project.name, mentioned, target_agents, respondents)

        # 项目暂停期间：消息进入排队队列，恢复时统一回放。
        if project.paused:
            logger.info("Project [%s] is paused, queueing user msg: %s",
                        project.name, content[:60])
            project.queue_paused_message(
                sender="user", sender_name="User",
                content=content, target_agents=respondents)
            qcount = len(getattr(project, "paused_queue", []) or [])
            project.post_message(
                sender="system", sender_name="System",
                content=(f"⏸️ 项目处于暂停状态，消息已加入队列（共 {qcount} 条），"
                         f"将在恢复后回放。发送「继续」可恢复。"),
                msg_type="system",
            )
            try:
                self._save()
            except Exception:
                pass
            return []

        if not respondents:
            logger.warning("Project chat [%s] no respondents for msg: %s",
                           project.name, content[:60])

        # ── 依次回复（而非并行）──
        # 项目群聊里 @全员 / 多人触发时，按 respondents 顺序依次回复：
        #   1. 避免多个 agent 同时抢发、话题撞车
        #   2. 后发言的 agent 能看到前者的回复作为上下文（更像真人会议）
        #   3. 下游工具（submit_deliverable 等）基于 thread-local 的
        #      project_context 也更稳定
        # 所有回复仍放在一个后台 daemon 线程里，HTTP 请求立即返回 respondents
        # 用于前端渲染 "agent 正在输入" 气泡。
        def _respond_sequentially(agent_ids: list[str]):
            for aid in agent_ids:
                try:
                    self._agent_respond(project, aid, content)
                except Exception as e:
                    logger.exception(
                        "Project chat [%s] sequential respond failed for %s: %s",
                        project.name, aid, e)
                # 项目被中途暂停/停止/关闭 → 终止后续 agent 发言
                if project.paused or project.status in (
                        ProjectStatus.CANCELLED, ProjectStatus.COMPLETED,
                        ProjectStatus.ARCHIVED):
                    logger.info(
                        "Project chat [%s] sequential respond aborted "
                        "(paused=%s status=%s) — remaining agents skipped",
                        project.name, project.paused, project.status)
                    break

        if respondents:
            threading.Thread(
                target=_respond_sequentially,
                args=(list(respondents),),
                daemon=True,
            ).start()

        return respondents

    def handle_task_assignment(self, project: Project, task: ProjectTask):
        """任务分配后，通知被分配的 Agent。"""
        if not task.assigned_to:
            return
        agent = self._lookup(task.assigned_to)
        if not agent:
            return

        member = None
        for m in project.members:
            if m.agent_id == task.assigned_to:
                member = m
                break

        prompt = self._build_task_prompt(project, task, member)

        def _run():
            try:
                task.status = ProjectTaskStatus.IN_PROGRESS
                task.updated_at = time.time()
                agent_obj = self._lookup(task.assigned_to)
                name = f"{agent_obj.role}-{agent_obj.name}" if agent_obj else task.assigned_to

                # ── Step-level resumable execution ──
                paused_for_review = False
                if task.steps:
                    # Resume mode: skip done/awaiting_review steps; run pending steps one by one
                    aggregated = []
                    while True:
                        pending = task.next_pending_step()
                        if pending is None:
                            break
                        task.start_step(pending)
                        try:
                            self._save() if self._save else None
                        except Exception:
                            pass
                        review_note = (
                            "\n⚠️ 这一步标记为「人工审核」: 你只需要产出草稿/初步结果并返回，"
                            "**不要**自行宣布完成 —— 系统会等待人工 approve 才会推进到下一步。"
                            if pending.manual_review else ""
                        )
                        step_prompt = (
                            f"{prompt}\n\n[Step {task.current_step_index+1}/{len(task.steps)}: "
                            f"{pending.name}]\n"
                            f"已完成步骤: " + ", ".join(
                                f"{s.name}" for s in task.steps[:task.current_step_index]
                                if s.status in ("done", "skipped")
                            ) + "\n请只完成当前 step，然后简要返回结果。" + review_note
                        )
                        try:
                            step_result = self._chat(task.assigned_to, step_prompt)
                            # complete_step honours pending.manual_review automatically:
                            # - manual_review=True  -> status becomes 'awaiting_review'
                            # - manual_review=False -> status becomes 'done'
                            task.complete_step(pending, result=step_result)
                            aggregated.append(f"[{pending.name}] {step_result}")
                            try:
                                self._save() if self._save else None
                            except Exception:
                                pass
                            # If this step needs human review, pause the whole task here.
                            if pending.status == "awaiting_review":
                                paused_for_review = True
                                project.post_message(
                                    sender=task.assigned_to,
                                    sender_name=name,
                                    content=(
                                        f"⏸️ Step「{pending.name}」已产出草稿，等待人工审核。"
                                        f"请在任务面板点击「Approve」继续后续步骤。"
                                    ),
                                    msg_type="task_update",
                                    task_id=task.id,
                                )
                                break
                        except Exception as se:
                            task.complete_step(pending, error=str(se))
                            raise
                    result = "\n\n".join(aggregated)
                    # Safety: if the loop exited because there were no more
                    # runnable steps but at least one step is still awaiting
                    # review (e.g. task was resumed from a previous run after
                    # restart), treat it as paused too — never auto-finalize.
                    if not paused_for_review and task.has_awaiting_review():
                        paused_for_review = True
                else:
                    result = self._chat(task.assigned_to, prompt)

                if paused_for_review:
                    # Task remains IN_PROGRESS; do not post the final ✅ message.
                    # An approve_step API call will re-trigger handle_task_assignment.
                    task.updated_at = time.time()
                    return

                project.post_message(
                    sender=task.assigned_to,
                    sender_name=name,
                    content=result,
                    msg_type="task_update",
                    task_id=task.id,
                )
                # 标记完成
                task.status = ProjectTaskStatus.DONE
                task.result = result[:2000]
                task.updated_at = time.time()
                project.post_message(
                    sender=task.assigned_to,
                    sender_name=name,
                    content=f"✅ 任务完成: {task.title}",
                    msg_type="task_update",
                    task_id=task.id,
                )
            except Exception as e:
                task.status = ProjectTaskStatus.BLOCKED
                task.result = f"Error: {e}"
                task.updated_at = time.time()

        threading.Thread(target=_run, daemon=True).start()

    # ── 内部方法 ──

    def _resume_auto_wake(self, project: Project) -> None:
        """
        项目恢复时：扫描所有成员，给每个有未完成任务的 agent 发送
        "继续干活" 的唤醒消息。这样即使 admin 没有手动点 Wake，
        在 resume 之后流程也能自动续上。
        """
        if project.paused:
            return
        woke = 0
        for m in project.members:
            agent_id = m.agent_id
            if not agent_id:
                continue
            pending = [
                t for t in project.tasks
                if t.assigned_to == agent_id
                and t.status in (ProjectTaskStatus.TODO,
                                 ProjectTaskStatus.IN_PROGRESS)
            ]
            if not pending:
                continue
            agent = self._lookup(agent_id)
            if not agent:
                continue
            titles = "\n".join(f"  - {t.title}" for t in pending[:5])
            trigger = (
                f"【项目恢复】{project.name} 已恢复运行，你还有 "
                f"{len(pending)} 个未完成任务：\n{titles}\n\n"
                f"请立即继续。完成后请在回复中包含 ✅ 和 '已完成' 字样。"
            )
            try:
                threading.Thread(
                    target=self._agent_respond,
                    args=(project, agent_id, trigger),
                    daemon=True,
                ).start()
                woke += 1
            except Exception as e:
                logger.warning("Resume auto-wake spawn failed for %s: %s",
                               agent_id[:8], e)
        if woke:
            project.post_message(
                sender="system", sender_name="System",
                content=f"🔔 已唤醒 {woke} 个有未完成任务的 Agent。",
                msg_type="system",
            )

    def _agent_respond(self, project: Project, agent_id: str, user_msg: str):
        """让指定 Agent 在项目上下文中回复。"""
        # Admin 优先级守卫：项目暂停期间，所有 in-flight 的 agent 响应都短路。
        # 由于 agent 调用 LLM 之前会经过这里，可以拦截那些已经被 spawn 但还
        # 没开始跑 LLM 的 daemon 线程，避免暂停后还出现"agent 又在干活"。
        if project.paused:
            logger.info("Project [%s] paused — _agent_respond aborted for %s",
                        project.name, agent_id[:8])
            return

        agent = self._lookup(agent_id)
        if not agent:
            logger.warning("Project chat [%s] agent not found: %s",
                           project.name, agent_id)
            return

        logger.info("Project chat [%s] agent %s-%s (%s) starting response...",
                     project.name, agent.role, agent.name, agent_id[:8])

        member = None
        for m in project.members:
            if m.agent_id == agent_id:
                member = m
                break

        prompt = self._build_chat_prompt(project, agent, member, user_msg)

        try:
            # ── Thread-local project context: lets tool handlers (e.g.
            #    submit_deliverable, create_goal, create_milestone) discover
            #    the project id without threading it through every tool call. ──
            from .project_context import set_project_context
            set_project_context(project.id)
            try:
                result = self._chat(agent_id, prompt)
            finally:
                set_project_context("")
            name = f"{agent.role}-{agent.name}" if agent else agent_id
            logger.info("Project chat [%s] agent %s responded (%d chars)",
                        project.name, name, len(result))
            project.post_message(
                sender=agent_id,
                sender_name=name,
                content=result,
                msg_type="chat",
            )
            # B: auto-registration from chat replies is disabled on purpose —
            # deliverables should only come from explicit submit_deliverable tool
            # calls (or manual UI submission). Auto-scanning 📎 markers and
            # markdown links produced too much noise (skill.md, MCP.md, code files,
            # framework artifacts). The helper _auto_register_deliverables_from_reply
            # is kept for reference but no longer invoked.
            # 检查是否完成了 WF Step → 自动更新状态
            self._check_wf_step_completion(project, agent_id, result)
        except Exception as e:
            logger.error("Project chat [%s] agent %s-%s respond FAILED: %s",
                         project.name, agent.role, agent.name, e)
            project.post_message(
                sender=agent_id,
                sender_name=f"{agent.role}-{agent.name}" if agent else agent_id,
                content=f"❌ 回复失败: {e}",
                msg_type="system",
            )
        # Persist after each agent response
        try:
            self._save()
        except Exception:
            pass

    def _auto_register_deliverables_from_reply(self, project: Project,
                                                  agent_id: str,
                                                  reply: str) -> int:
        """Scan an agent reply for file references and register each as a
        draft Deliverable (kind derived from file extension).

        Scans two forms:
          1. Paperclip marker:  📎filename.ext   (what project chat uses)
          2. Markdown link:     [name](path/to/file.ext)
                                [name](./file.ext)

        De-dup rule: (author_agent_id, file_path) is unique — if a deliverable
        already exists for this pair, it's skipped.

        Returns the number of newly-registered deliverables.
        """
        if not reply:
            return 0
        import re

        # Allowed extensions (reviewable output, not code noise)
        EXT_KIND = {
            ".md": "document", ".markdown": "document",
            ".pdf": "document", ".doc": "document", ".docx": "document",
            ".txt": "document", ".rtf": "document",
            ".xlsx": "analysis", ".xls": "analysis", ".csv": "analysis",
            ".ppt": "document", ".pptx": "document",
            ".png": "media", ".jpg": "media", ".jpeg": "media",
            ".gif": "media", ".svg": "media", ".webp": "media",
            ".mp3": "media", ".wav": "media", ".mp4": "media", ".webm": "media",
            ".json": "analysis", ".yaml": "analysis", ".yml": "analysis",
            ".html": "document", ".htm": "document",
        }

        candidates: set[str] = set()

        # Form 1: 📎 marker → "📎filename.ext" or "📎 filename.ext"
        for m in re.finditer(r"\U0001f4ce\s*([^\s<>]+)", reply):
            candidates.add(m.group(1).strip())

        # Form 2: Markdown link [text](path) — only keep paths with known ext
        for m in re.finditer(r"\[([^\]]+)\]\(([^)\s]+)\)", reply):
            url = m.group(2).strip()
            # Skip absolute URLs
            if url.startswith(("http://", "https://", "mailto:", "#")):
                continue
            candidates.add(url)

        if not candidates:
            return 0

        existing_pairs = {
            (dv.author_agent_id, dv.file_path)
            for dv in project.deliverables
            if dv.file_path
        }
        registered = 0
        for path in candidates:
            # Strip leading ./ and anchors/fragments
            clean = path.lstrip("./").split("#")[0].split("?")[0]
            if not clean:
                continue
            ext = ""
            idx = clean.rfind(".")
            if idx >= 0:
                ext = clean[idx:].lower()
            if ext not in EXT_KIND:
                continue
            if (agent_id, clean) in existing_pairs:
                continue
            title = clean.rsplit("/", 1)[-1]
            try:
                project.add_deliverable(
                    title=title,
                    kind=EXT_KIND[ext],
                    author_agent_id=agent_id,
                    file_path=clean,
                    content_text=f"(auto-registered from chat reply)",
                )
                registered += 1
                existing_pairs.add((agent_id, clean))
            except Exception as _ae:
                logger.debug("add_deliverable failed for %s: %s", clean, _ae)
        if registered:
            logger.info("Project [%s] auto-registered %d deliverable(s) from %s's reply",
                        project.name, registered, agent_id[:8])
        return registered

    def _check_wf_step_completion(self, project: Project, agent_id: str,
                                    response: str):
        """
        检测 Agent 回复是否包含步骤完成信号，自动更新 WF Step 任务状态。

        触发条件（任一）:
        1. Agent 回复中包含 [STEP_DONE] 标记（显式）
        2. Agent 回复中包含 "✅" + "完成" 类关键词（隐式检测）

        只处理绑定了 Workflow 且该 Agent 有负责 step 的情况。
        """
        if not project.workflow_binding.workflow_id:
            return

        # Admin 优先级守卫：项目暂停期间，不允许把任何 step 标记为完成。
        # 否则 in-flight 的 LLM 调用结束后还会推进进度，让 admin 看到 7/7。
        if project.paused:
            logger.info("Project [%s] paused — _check_wf_step_completion "
                        "skipped for %s", project.name, agent_id[:8])
            return

        import re
        response_lower = response.lower()

        # 显式标记: [STEP_DONE] 或 [STEP_DONE:步骤名]
        explicit_done = "[step_done]" in response_lower or "[step done]" in response_lower

        # 隐式检测: ✅ + 完成/done/finished/已完成 等
        completion_keywords = ("完成", "done", "finished", "已完成", "交付", "已交付",
                               "完工", "已提交", "step completed")
        has_checkmark = "✅" in response
        has_keyword = any(kw in response_lower for kw in completion_keywords)
        implicit_done = has_checkmark and has_keyword

        if not explicit_done and not implicit_done:
            return

        # 找到该 Agent 负责的、尚未完成的 WF Step 任务
        wf_tasks = [t for t in project.tasks
                    if t.title.startswith("[WF Step")
                    and t.assigned_to == agent_id
                    and t.status != ProjectTaskStatus.DONE]

        if not wf_tasks:
            return

        # 优先处理第一个未完成的步骤（按步骤号排序）
        wf_tasks.sort(key=lambda t: int(
            re.search(r'\[WF Step (\d+)\]', t.title).group(1)
            if re.search(r'\[WF Step (\d+)\]', t.title) else 0))

        task = wf_tasks[0]

        # 顺序检查：确认前序步骤都已完成，否则不允许关闭当前步骤
        task_step_num = int(
            (re.search(r'\[WF Step (\d+)\]', task.title) or
             type('', (), {'group': lambda s, x: '0'})()).group(1))
        all_wf = [t for t in project.tasks if t.title.startswith("[WF Step")]
        for prev in all_wf:
            prev_num = int(
                (re.search(r'\[WF Step (\d+)\]', prev.title) or
                 type('', (), {'group': lambda s, x: '0'})()).group(1))
            if prev_num < task_step_num and prev.status != ProjectTaskStatus.DONE:
                prev_name = re.sub(r'^\[WF Step \d+\]\s*', '', prev.title)
                logger.warning(
                    "WF Step completion blocked: step %d '%s' cannot close — "
                    "preceding step %d '%s' not done yet (project=%s)",
                    task_step_num, task.title, prev_num, prev_name, project.name)
                agent = self._lookup(agent_id)
                name = f"{agent.role}-{agent.name}" if agent else agent_id
                project.post_message(
                    sender="system", sender_name="System",
                    content=(f"⚠️ 步骤顺序检查: {name} 标记了完成信号，"
                             f"但前序步骤「{prev_name}」(Step {prev_num}) 尚未完成，"
                             f"当前步骤暂不关闭。"),
                    msg_type="system",
                )
                return  # 不关闭

        task.status = ProjectTaskStatus.DONE
        task.result = response[:2000]
        task.updated_at = time.time()

        agent = self._lookup(agent_id)
        name = f"{agent.role}-{agent.name}" if agent else agent_id
        step_name = re.sub(r'^\[WF Step \d+\]\s*', '', task.title)
        project.post_message(
            sender="system", sender_name="System",
            content=f"☑️ Workflow Step 完成: {step_name} (by {name})",
            msg_type="system",
        )
        logger.info("WF Step auto-completed: %s by %s in project %s",
                     task.title, agent_id, project.id)

        # Auto-progress: trigger next step's agent
        self._auto_progress_next_step(project, task)

    def _auto_progress_next_step(self, project: Project, completed_task: ProjectTask):
        """
        Workflow 自动推进：当一个步骤完成后，自动唤醒下一个步骤的负责 Agent。

        逻辑：
        1. 从完成的 step 号开始，找到下一个状态为 todo 的 WF Step
        2. 提取其负责的 agent_id
        3. 构建上下文消息（包含前序步骤的产出）发给该 agent
        4. Agent 在后台线程中执行
        """
        import re

        if not project.workflow_binding.workflow_id:
            return

        # Admin 优先级: 项目暂停时，禁止自动推进
        if project.paused:
            logger.info("WF auto-progress: project '%s' is paused, skip "
                        "next-step trigger after '%s'",
                        project.name, completed_task.title)
            project.post_message(
                sender="system", sender_name="System",
                content=("⏸️ 项目处于暂停状态，已跳过下一步骤的自动唤醒。"
                         "发送「继续」可恢复 Workflow 推进。"),
                msg_type="system",
            )
            return

        # 获取所有 WF Step 任务，按步骤号排序
        wf_tasks = [t for t in project.tasks if t.title.startswith("[WF Step")]
        wf_tasks.sort(key=lambda t: int(
            (re.search(r'\[WF Step (\d+)\]', t.title) or type('', (), {'group': lambda s, x: '0'})()).group(1)
        ))

        # 找到刚完成步骤的位置
        completed_idx = -1
        for i, t in enumerate(wf_tasks):
            if t.id == completed_task.id:
                completed_idx = i
                break
        if completed_idx < 0:
            return

        # 找下一个未完成的步骤
        next_task = None
        for t in wf_tasks[completed_idx + 1:]:
            if t.status == ProjectTaskStatus.TODO:
                next_task = t
                break

        if not next_task or not next_task.assigned_to:
            logger.info("WF auto-progress: no next todo step after '%s'",
                        completed_task.title)
            return

        next_agent = self._lookup(next_task.assigned_to)
        if not next_agent:
            logger.warning("WF auto-progress: agent %s not found for next step",
                           next_task.assigned_to)
            return

        next_step_name = re.sub(r'^\[WF Step \d+\]\s*', '', next_task.title)
        completed_step_name = re.sub(r'^\[WF Step \d+\]\s*', '', completed_task.title)

        # Step-level approval gate: if the next step is marked require_approval,
        # pause auto-progress and post a system approval request instead of
        # triggering the agent immediately.
        try:
            m_next = re.search(r'\[WF Step (\d+)\]', next_task.title)
            next_step_idx = (int(m_next.group(1)) - 1) if m_next else -1
            needs_approval = False
            for sa in (project.workflow_binding.step_assignments or []):
                if int(sa.get("step_index", -1)) == next_step_idx and sa.get("require_approval"):
                    needs_approval = True
                    break
            if needs_approval:
                next_task.status = ProjectTaskStatus.TODO
                next_task.updated_at = time.time()
                agent_name_pending = f"{next_agent.role}-{next_agent.name}"
                project.post_message(
                    sender="system", sender_name="System",
                    content=(
                        f"⏸️ 待人工批准: 步骤「{next_step_name}」需审核后方可启动 "
                        f"(负责人 @{agent_name_pending})。\n"
                        f"在项目详情中点击『批准步骤』或回复 `/approve-step {next_step_idx + 1}` 以继续。"
                    ),
                    msg_type="system",
                )
                # Record pending approval on the task so UI can surface it
                try:
                    if not hasattr(next_task, "metadata") or next_task.metadata is None:
                        next_task.metadata = {}
                    next_task.metadata["pending_approval"] = True
                    next_task.metadata["pending_step_index"] = next_step_idx
                except Exception:
                    pass
                logger.info(
                    "WF auto-progress: step %d in '%s' requires approval, "
                    "paused awaiting admin",
                    next_step_idx + 1, project.name,
                )
                return
        except Exception as _ap_err:
            logger.debug("approval gate check skipped: %s", _ap_err)

        # 收集前序已完成步骤的产出摘要
        prev_results = []
        for t in wf_tasks[:completed_idx + 1]:
            if t.status == ProjectTaskStatus.DONE and t.result:
                sname = re.sub(r'^\[WF Step \d+\]\s*', '', t.title)
                prev_results.append(f"  - {sname}: {t.result[:300]}")

        prev_context = ""
        if prev_results:
            prev_context = "\n前序步骤产出:\n" + "\n".join(prev_results[-3:])  # 最多3条

        # 更新任务状态为 IN_PROGRESS
        next_task.status = ProjectTaskStatus.IN_PROGRESS
        next_task.updated_at = time.time()

        # 发系统消息通知
        agent_name = f"{next_agent.role}-{next_agent.name}"
        project.post_message(
            sender="system", sender_name="System",
            content=f"🔄 自动推进: {completed_step_name} 已完成，开始 {next_step_name} → @{agent_name}",
            msg_type="system",
        )

        logger.info("WF auto-progress: triggering agent %s for step '%s' in project %s",
                     agent_name, next_step_name, project.name)

        # 构建触发消息并在后台启动 Agent
        trigger_msg = (
            f"上一个步骤「{completed_step_name}」已完成。\n"
            f"现在轮到你执行「{next_step_name}」。\n"
            f"{prev_context}\n"
            f"请根据你的职责和前序步骤的产出，立即开始执行这个步骤。"
            f"完成后请在回复中包含 ✅ 和 '已完成' 来标记步骤完成。"
        )

        t = threading.Thread(
            target=self._agent_respond,
            args=(project, next_task.assigned_to, trigger_msg),
            daemon=True,
        )
        t.start()

    def _build_chat_prompt(self, project: Project, agent, member, user_msg: str) -> str:
        """构建带项目上下文的聊天提示词。"""
        ctx = project.get_chat_context_for_agent(agent.id if agent else "", limit=15)
        responsibility = member.responsibility if member else "通用成员"

        # ── Admin 优先级：抓取最近的 admin 指令并高亮 ──
        # 这一段会出现在 prompt 顶部，让 agent 在执行任何工作前先看到。
        admin_cmds_block = ""
        try:
            recent = project.chat_history[-30:] if project.chat_history else []
            admin_msgs = [m for m in recent
                          if getattr(m, "sender_role", "") == "admin"
                          or m.sender == "user"]
            if admin_msgs:
                last_admin = admin_msgs[-3:]  # 最近 3 条
                lines = []
                for m in last_admin:
                    ts = ""
                    if m.timestamp:
                        try:
                            ts = time.strftime("%H:%M:%S",
                                               time.localtime(m.timestamp))
                        except Exception:
                            pass
                    lines.append(f"  [{ts}] {m.content[:300]}")
                admin_cmds_block = (
                    "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "⚠️ ADMIN 指令（最高优先级，必须遵守）\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join(lines) + "\n"
                    "规则：\n"
                    "1. 如果上面有「暂停/停止/取消/停一下/pause/stop」类指令，"
                    "你必须立即停止工作，回复一句确认（不要继续执行任何步骤、"
                    "不要调用工具、不要标记任何任务完成）。\n"
                    "2. ADMIN 指令凌驾于 Workflow Step、任务分配、自动推进之上。\n"
                    "3. 如果 ADMIN 指令与你被分配的工作冲突，以 ADMIN 指令为准。\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                )
        except Exception as _e:
            logger.debug("admin_cmds_block build failed: %s", _e)

        # 项目暂停状态显式提示（即使没拦截住也让 agent 自己看到）
        pause_block = ""
        if project.paused:
            pause_block = (
                "\n🛑 项目当前处于暂停状态。请不要执行任何工作。"
                "只回复一句「已收到暂停指令」即可。\n"
            )

        # 当前任务摘要
        my_tasks = project.list_tasks(assigned_to=agent.id if agent else "")
        task_lines = ""
        if my_tasks:
            task_lines = "\n你当前的任务:\n" + "\n".join(
                f"  - [{t.status.value}] {t.title}" for t in my_tasks[:5]
            )

        # 团队成员
        team_lines = []
        for m in project.members:
            a = self._lookup(m.agent_id)
            if a:
                team_lines.append(f"  - {a.role}-{a.name}: {m.responsibility}")

        # Workflow 步骤提示 + 实时状态
        wf_hint = ""
        if project.workflow_binding.workflow_id:
            import re as _re
            wf_all = [t for t in project.tasks if t.title.startswith("[WF Step")]
            wf_all.sort(key=lambda t: int(
                (_re.search(r'\[WF Step (\d+)\]', t.title) or
                 type('', (), {'group': lambda s, x: '0'})()).group(1)))
            status_map = {"done": "✅", "in_progress": "⏳", "todo": "○", "blocked": "🚫"}
            step_lines = []
            for t in wf_all:
                icon = status_map.get(t.status.value, "○")
                sname = _re.sub(r'^\[WF Step \d+\]\s*', '', t.title)
                assignee = ""
                a = self._lookup(t.assigned_to) if t.assigned_to else None
                if a:
                    assignee = f" ({a.name})"
                step_lines.append(f"  {icon} {sname}{assignee}")
            done_count = sum(1 for t in wf_all if t.status == ProjectTaskStatus.DONE)
            wf_hint = (
                f"\n[Workflow 步骤实时状态] {done_count}/{len(wf_all)} 完成:\n"
                + "\n".join(step_lines) + "\n"
                "⚠️ 重要：上面的状态是系统实时数据，请以此为准判断项目进度。"
                "未显示 ✅ 的步骤尚未完成，不要声称所有步骤已完成。\n"
                "[Workflow 提示] 当你完成了自己负责的步骤，"
                "请在回复中包含 ✅ 和 '已完成' 字样（例如 '✅ 需求分析已完成'），"
                "或使用 [STEP_DONE] 标记，系统会自动更新步骤状态。\n"
            )

        project_scope_hint = (
            f"\n[项目上下文] 你正在项目 {project.name!r} 中 (project_id={project.id})。\n"
            f"可用的项目工具 (自动识别当前项目，无需传 project_id):\n"
            f"  - submit_deliverable(title, file_path?/content_text?/url?, kind?, milestone_id?)\n"
            f"    ⚠️ 必须显式调用该工具 —— 只在聊天回复里提到文件名或说\"已完成\"**不会**登记。\n"
            f"    每产出一个交付件（文档/代码/设计/分析）都要单独调一次；\n"
            f"    如果提供 file_path，系统会自动把文件复制到项目共享目录\n"
            f"    ~/.tudou_claw/workspaces/shared/{project.id}/。\n"
            f"  - create_goal(name, metric?, target_value?, target_text?) / update_goal_progress(goal_id, current_value?, done?)\n"
            f"    → 识别到可度量目标时创建；有进展时更新。\n"
            f"  - create_milestone(name, responsible_agent_id?, due_date?) / update_milestone_status(milestone_id, status?, evidence?)\n"
            f"    → 规划重大检查点或推进状态时使用。\n"
        )
        return (
            f"{admin_cmds_block}"
            f"{pause_block}"
            f"[项目群聊 — {project.name}]\n"
            f"你的职责: {responsibility}\n"
            f"\n团队成员:\n" + "\n".join(team_lines) +
            f"{task_lines}\n"
            f"{wf_hint}"
            f"{project_scope_hint}"
            f"\n最近聊天记录:\n{ctx}\n"
            f"\n[User]: {user_msg}\n"
            f"\n请以你的角色和职责回复。简洁、有用、直接。"
            f"\n注意：如果上面有 ADMIN 指令，请先遵守 ADMIN 指令。"
        )

    def _build_task_prompt(self, project: Project, task: ProjectTask,
                            member) -> str:
        """构建任务执行提示词。"""
        responsibility = member.responsibility if member else ""
        ctx = project.get_chat_context_for_agent("", limit=10)

        return (
            f"[项目: {project.name} — 任务分配]\n"
            f"你的职责: {responsibility}\n"
            f"\n任务标题: {task.title}\n"
            f"任务描述: {task.description}\n"
            f"优先级: {'🔴紧急' if task.priority >= 2 else '🟡高' if task.priority == 1 else '🟢普通'}\n"
            f"\n最近聊天上下文:\n{ctx}\n"
            f"\n请完成这个任务，给出详细的执行结果。"
        )

    def _parse_mentions(self, text: str, project: Project) -> list[str]:
        """解析消息中的 @agent 提及。"""
        mentioned = []
        for m in project.members:
            agent = self._lookup(m.agent_id)
            if not agent:
                logger.debug("_parse_mentions: member %s agent not found",
                             m.agent_id)
                continue
            # 匹配 @name 或 @role-name
            triggers = [
                f"@{agent.name}",
                f"@{agent.role}-{agent.name}",
                f"@{agent.role}",
            ]
            for trigger in triggers:
                if trigger in text:
                    if m.agent_id not in mentioned:
                        mentioned.append(m.agent_id)
                        logger.debug("_parse_mentions: matched '%s' → %s-%s",
                                     trigger, agent.role, agent.name)
                    break
        return mentioned

    def _auto_select(self, text: str, project: Project) -> list[str]:
        """自动选择最相关的 Agent（没有 @mention 时）。"""
        # 简单策略: 如果只有一个成员就选它; 否则选第一个
        # 未来可以用 LLM 做智能路由
        if not project.members:
            return []
        if len(project.members) == 1:
            return [project.members[0].agent_id]
        # 多成员时全部回复（群聊语义）
        return [m.agent_id for m in project.members]
