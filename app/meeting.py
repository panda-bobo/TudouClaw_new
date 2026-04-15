"""Meeting / Ad-hoc multi-agent conference module.

A Meeting is a lightweight, short-lived multi-agent session that pulls
together a set of agents to discuss a topic and optionally assign tasks
that may or may not belong to a specific Project.

Why a separate concept from Project:
  * Projects are long-lived plans with members, milestones, deliverables.
  * Meetings are ephemeral working sessions (e.g. "pull dev + reviewer to
    triage this bug", "ask the research team for a quick spec check").
  * A Meeting can spawn either a ProjectTask (if bound to a project) or a
    StandaloneTask (if it is non-project work).

The Meeting itself owns its own chat transcript plus a list of
*assignments* — tiny task stubs with owner/status/due — so the host can
follow up without hunting through a chat log.

This module is storage-agnostic: MeetingRegistry just holds objects in
memory and delegates persistence to a caller-supplied save callback.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger("tudouclaw.meeting")


class MeetingStatus(str, Enum):
    SCHEDULED = "scheduled"   # created but not started
    ACTIVE = "active"         # ongoing
    PAUSED = "paused"
    CLOSED = "closed"         # ended normally
    CANCELLED = "cancelled"


class AssignmentStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class MeetingMessage:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    sender: str = ""            # agent id or "user"
    sender_name: str = ""       # display name cached at write-time
    role: str = "agent"         # "user" | "agent" | "system"
    content: str = ""
    attachments: list = field(default_factory=list)  # list[{name,mime,size,data_base64}]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "MeetingMessage":
        return MeetingMessage(
            id=d.get("id", ""),
            sender=d.get("sender", ""),
            sender_name=d.get("sender_name", ""),
            role=d.get("role", "agent"),
            content=d.get("content", ""),
            attachments=list(d.get("attachments", []) or []),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class MeetingAssignment:
    """A task stub spun out of a meeting.

    Either bound to a Project (project_id set → creates a ProjectTask via
    caller) or standalone (no project_id → lives in the standalone task
    list on the hub).
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    assignee_agent_id: str = ""
    due_hint: str = ""                  # free-form "by Friday" / "today 17:00"
    project_id: str = ""                # empty → standalone
    project_task_id: str = ""           # link if materialized to ProjectTask
    standalone_task_id: str = ""        # link if materialized to StandaloneTask
    status: AssignmentStatus = AssignmentStatus.OPEN
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = (self.status.value
                       if isinstance(self.status, AssignmentStatus)
                       else str(self.status))
        return d

    @staticmethod
    def from_dict(d: dict) -> "MeetingAssignment":
        try:
            st = AssignmentStatus(d.get("status", "open"))
        except ValueError:
            st = AssignmentStatus.OPEN
        return MeetingAssignment(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            assignee_agent_id=d.get("assignee_agent_id", ""),
            due_hint=d.get("due_hint", ""),
            project_id=d.get("project_id", ""),
            project_task_id=d.get("project_task_id", ""),
            standalone_task_id=d.get("standalone_task_id", ""),
            status=st,
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            result=d.get("result", ""),
        )


@dataclass
class Meeting:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    agenda: str = ""
    host: str = ""                           # "user" or agent id
    participants: list = field(default_factory=list)   # list[agent_id]
    project_id: str = ""                     # optional — pins the meeting to a project
    status: MeetingStatus = MeetingStatus.SCHEDULED
    messages: list = field(default_factory=list)        # list[MeetingMessage]
    assignments: list = field(default_factory=list)     # list[MeetingAssignment]
    summary: str = ""                        # post-meeting summary
    workspace_dir: str = ""                  # shared file directory for this meeting
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── lifecycle ──
    def start(self) -> None:
        with self._lock:
            if self.status == MeetingStatus.SCHEDULED:
                self.status = MeetingStatus.ACTIVE
                self.started_at = time.time()

    def close(self, summary: str = "") -> None:
        with self._lock:
            self.status = MeetingStatus.CLOSED
            self.ended_at = time.time()
            if summary:
                self.summary = summary

    def cancel(self) -> None:
        with self._lock:
            self.status = MeetingStatus.CANCELLED
            self.ended_at = time.time()

    # ── participants ──
    def add_participant(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self.participants:
                return False
            self.participants.append(agent_id)
            return True

    def remove_participant(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id not in self.participants:
                return False
            self.participants.remove(agent_id)
            return True

    # ── chat ──
    def add_message(self, sender: str, content: str, role: str = "agent",
                    sender_name: str = "", attachments: list | None = None) -> MeetingMessage:
        m = MeetingMessage(
            sender=sender, sender_name=sender_name or sender,
            role=role, content=content,
            attachments=list(attachments or []),
        )
        with self._lock:
            self.messages.append(m)
        return m

    # ── assignments ──
    def add_assignment(self, title: str, assignee_agent_id: str = "",
                       description: str = "", due_hint: str = "",
                       project_id: str = "") -> MeetingAssignment:
        a = MeetingAssignment(
            title=title, description=description,
            assignee_agent_id=assignee_agent_id,
            due_hint=due_hint,
            project_id=project_id or self.project_id,
        )
        with self._lock:
            self.assignments.append(a)
        return a

    def update_assignment(self, assignment_id: str, **kwargs) -> Optional[MeetingAssignment]:
        with self._lock:
            for a in self.assignments:
                if a.id == assignment_id:
                    for k, v in kwargs.items():
                        if k == "status" and v is not None:
                            try:
                                a.status = AssignmentStatus(v)
                            except ValueError:
                                pass
                        elif hasattr(a, k) and v is not None:
                            setattr(a, k, v)
                    a.updated_at = time.time()
                    return a
        return None

    # ── progress posting (called by agents to update task status) ──
    def post_progress(self, agent_id: str, agent_name: str,
                      assignment_id: str, status: str,
                      detail: str = "") -> MeetingMessage:
        """Agent posts a progress update for an assignment."""
        # Update assignment status if valid
        self.update_assignment(assignment_id, status=status)
        # Build progress message
        parts = [f"📋 任务进度更新"]
        for a in self.assignments:
            if a.id == assignment_id:
                parts.append(f"任务: {a.title}")
                break
        parts.append(f"状态: {status}")
        if detail:
            parts.append(f"详情: {detail}")
        content = "\n".join(parts)
        return self.add_message(
            sender=agent_id,
            sender_name=agent_name,
            role="system",
            content=content,
        )

    # ── workspace file listing ──
    def list_files(self) -> list[dict]:
        """List files in the meeting workspace directory."""
        if not self.workspace_dir or not os.path.isdir(self.workspace_dir):
            return []
        result = []
        for entry in os.scandir(self.workspace_dir):
            if entry.is_file():
                stat = entry.stat()
                result.append({
                    "name": entry.name,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                })
        result.sort(key=lambda x: x["modified_at"], reverse=True)
        return result

    # ── serialization ──
    def to_dict(self) -> dict:
        status = (self.status.value
                  if isinstance(self.status, MeetingStatus)
                  else str(self.status))
        return {
            "id": self.id,
            "title": self.title,
            "agenda": self.agenda,
            "host": self.host,
            "participants": list(self.participants),
            "project_id": self.project_id,
            "status": status,
            "messages": [m.to_dict() for m in self.messages],
            "assignments": [a.to_dict() for a in self.assignments],
            "summary": self.summary,
            "workspace_dir": self.workspace_dir,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "message_count": len(self.messages),
            "assignment_count": len(self.assignments),
            "file_count": len(self.list_files()),
        }

    def to_summary_dict(self) -> dict:
        """Lightweight form for list views."""
        status = (self.status.value
                  if isinstance(self.status, MeetingStatus)
                  else str(self.status))
        return {
            "id": self.id,
            "title": self.title,
            "host": self.host,
            "participants": list(self.participants),
            "project_id": self.project_id,
            "status": status,
            "message_count": len(self.messages),
            "assignment_count": len(self.assignments),
            "open_assignments": sum(
                1 for a in self.assignments
                if (a.status if isinstance(a.status, AssignmentStatus) else AssignmentStatus(a.status))
                in (AssignmentStatus.OPEN, AssignmentStatus.IN_PROGRESS)
            ),
            "workspace_dir": self.workspace_dir,
            "file_count": len(self.list_files()),
            "created_at": self.created_at,
            "ended_at": self.ended_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Meeting":
        try:
            st = MeetingStatus(d.get("status", "scheduled"))
        except ValueError:
            st = MeetingStatus.SCHEDULED
        m = Meeting(
            id=d.get("id", uuid.uuid4().hex[:10]),
            title=d.get("title", ""),
            agenda=d.get("agenda", ""),
            host=d.get("host", ""),
            participants=list(d.get("participants", []) or []),
            project_id=d.get("project_id", ""),
            status=st,
            summary=d.get("summary", ""),
            workspace_dir=d.get("workspace_dir", ""),
            created_at=d.get("created_at", time.time()),
            started_at=d.get("started_at", 0.0) or 0.0,
            ended_at=d.get("ended_at", 0.0) or 0.0,
        )
        m.messages = [MeetingMessage.from_dict(x) for x in d.get("messages", []) or []]
        m.assignments = [MeetingAssignment.from_dict(x) for x in d.get("assignments", []) or []]
        return m


class MeetingRegistry:
    """In-memory registry of meetings with JSON persistence.

    Persistence is intentionally simple: one JSON file on disk containing
    all meetings. Meetings are usually short-lived and low-volume, so this
    avoids bringing in another SQLite table.
    """

    def __init__(self, persist_path: str, data_dir: str = ""):
        self.persist_path = persist_path
        self._data_dir = data_dir or os.path.dirname(persist_path)
        self._meetings: dict[str, Meeting] = {}
        self._lock = threading.Lock()
        self.load()

    # ── persistence ──
    def load(self) -> None:
        if not os.path.isfile(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, list):
            return
        with self._lock:
            for d in raw:
                try:
                    m = Meeting.from_dict(d)
                    # Ensure workspace dir exists (migration for old data)
                    if not m.workspace_dir:
                        ws = os.path.join(self._data_dir, "workspaces", "meetings", m.id)
                        m.workspace_dir = ws
                    os.makedirs(m.workspace_dir, exist_ok=True)
                    self._meetings[m.id] = m
                except Exception:
                    continue

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
        with self._lock:
            data = [m.to_dict() for m in self._meetings.values()]
        tmp = self.persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.persist_path)

    # ── CRUD ──
    def _ensure_workspace(self, meeting: Meeting) -> None:
        """Create the shared workspace directory for a meeting."""
        ws = os.path.join(self._data_dir, "workspaces", "meetings", meeting.id)
        os.makedirs(ws, exist_ok=True)
        meeting.workspace_dir = ws
        logger.debug("Meeting workspace: %s", ws)

    def create(self, title: str, host: str, participants: list[str],
               agenda: str = "", project_id: str = "") -> Meeting:
        m = Meeting(
            title=title, agenda=agenda, host=host,
            participants=list(participants or []),
            project_id=project_id or "",
        )
        self._ensure_workspace(m)
        with self._lock:
            self._meetings[m.id] = m
        self.save()
        return m

    def get(self, meeting_id: str) -> Optional[Meeting]:
        return self._meetings.get(meeting_id)

    def list(self, project_id: str | None = None,
              status: str | None = None,
              participant: str | None = None) -> list[Meeting]:
        with self._lock:
            items = list(self._meetings.values())
        if project_id is not None:
            items = [m for m in items if m.project_id == project_id]
        if status:
            items = [m for m in items
                     if (m.status.value if isinstance(m.status, MeetingStatus) else str(m.status)) == status]
        if participant:
            items = [m for m in items if participant in m.participants]
        items.sort(key=lambda m: m.created_at, reverse=True)
        return items

    def delete(self, meeting_id: str) -> bool:
        with self._lock:
            if meeting_id in self._meetings:
                del self._meetings[meeting_id]
                ok = True
            else:
                ok = False
        if ok:
            self.save()
        return ok

    def touch(self) -> None:
        """Caller hint after mutating a meeting in-place."""
        self.save()


# ─────────────────────────────────────────────────────────────
# Standalone (non-project) Task list
# ─────────────────────────────────────────────────────────────

class StandaloneTaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class StandaloneTask:
    """A task that doesn't belong to any project.

    Used for ad-hoc work assigned to an agent — one-off questions,
    maintenance chores, quick jobs. Lives in a flat registry scoped to
    the hub, not to any project.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""
    description: str = ""
    assigned_to: str = ""
    created_by: str = ""
    status: StandaloneTaskStatus = StandaloneTaskStatus.TODO
    priority: str = "normal"         # low | normal | high | urgent
    due_hint: str = ""
    tags: list = field(default_factory=list)
    source_meeting_id: str = ""      # if spun out of a meeting
    agent_task_id: str = ""          # mirror id in the assignee agent's queue
    result: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> dict:
        status = (self.status.value
                  if isinstance(self.status, StandaloneTaskStatus)
                  else str(self.status))
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "assigned_to": self.assigned_to,
            "created_by": self.created_by,
            "status": status,
            "priority": self.priority,
            "due_hint": self.due_hint,
            "tags": list(self.tags),
            "source_meeting_id": self.source_meeting_id,
            "agent_task_id": self.agent_task_id,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "StandaloneTask":
        try:
            st = StandaloneTaskStatus(d.get("status", "todo"))
        except ValueError:
            st = StandaloneTaskStatus.TODO
        return StandaloneTask(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            assigned_to=d.get("assigned_to", ""),
            created_by=d.get("created_by", ""),
            status=st,
            priority=d.get("priority", "normal"),
            due_hint=d.get("due_hint", ""),
            tags=list(d.get("tags", []) or []),
            source_meeting_id=d.get("source_meeting_id", ""),
            agent_task_id=d.get("agent_task_id", ""),
            result=d.get("result", ""),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            started_at=float(d.get("started_at", 0) or 0),
            completed_at=float(d.get("completed_at", 0) or 0),
        )


class StandaloneTaskRegistry:
    def __init__(self, persist_path: str):
        self.persist_path = persist_path
        self._tasks: dict[str, StandaloneTask] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not os.path.isfile(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, list):
            return
        with self._lock:
            for d in raw:
                try:
                    t = StandaloneTask.from_dict(d)
                    self._tasks[t.id] = t
                except Exception:
                    continue

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
        with self._lock:
            data = [t.to_dict() for t in self._tasks.values()]
        tmp = self.persist_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.persist_path)

    def create(self, title: str, assigned_to: str = "",
               description: str = "", created_by: str = "",
               priority: str = "normal", due_hint: str = "",
               tags: list | None = None,
               source_meeting_id: str = "") -> StandaloneTask:
        t = StandaloneTask(
            title=title, description=description,
            assigned_to=assigned_to, created_by=created_by,
            priority=priority, due_hint=due_hint,
            tags=list(tags or []),
            source_meeting_id=source_meeting_id,
        )
        with self._lock:
            self._tasks[t.id] = t
        self.save()
        return t

    def get(self, task_id: str) -> Optional[StandaloneTask]:
        return self._tasks.get(task_id)

    def list(self, assignee: str | None = None,
              status: str | None = None) -> list[StandaloneTask]:
        with self._lock:
            items = list(self._tasks.values())
        if assignee:
            items = [t for t in items if t.assigned_to == assignee]
        if status:
            items = [t for t in items
                     if (t.status.value if isinstance(t.status, StandaloneTaskStatus) else str(t.status)) == status]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items

    def update(self, task_id: str, **kwargs) -> Optional[StandaloneTask]:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return None
            for k, v in kwargs.items():
                if k == "status" and v is not None:
                    try:
                        t.status = StandaloneTaskStatus(v)
                        if t.status == StandaloneTaskStatus.IN_PROGRESS and not t.started_at:
                            t.started_at = time.time()
                        if t.status == StandaloneTaskStatus.DONE:
                            t.completed_at = time.time()
                    except ValueError:
                        pass
                elif hasattr(t, k) and v is not None:
                    setattr(t, k, v)
            t.updated_at = time.time()
        self.save()
        return t

    def delete(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
                ok = True
            else:
                ok = False
        if ok:
            self.save()
        return ok


# ────────────────────────────────────────────────────────────
#   Meeting auto-reply loop
# ────────────────────────────────────────────────────────────

def _build_meeting_prompt(meeting: "Meeting", agent, user_msg: str,
                           tail: int = 20) -> str:
    """Build an LLM prompt that gives a participant agent enough context
    to reply inside a meeting (vs. a project chat).
    """
    role = getattr(agent, "role", "") or "participant"
    name = getattr(agent, "name", "") or "agent"
    # Gather participant names for context
    participant_names = []
    for pid in (meeting.participants or []):
        try:
            # agent_lookup_fn not available here; use raw id
            participant_names.append(pid)
        except Exception:
            pass

    lines = [
        f"你正在参加一个多 Agent 协作会议。",
        f"",
        f"## 会议信息",
        f"- 主题: {meeting.title or '(未命名)'}",
        f"- 议程: {meeting.agenda or '(无)'}",
        f"- 主持人: {meeting.host or 'user'}",
        f"- 参与者: {', '.join(participant_names) if participant_names else '(无)'}",
    ]
    if meeting.workspace_dir:
        lines.append(f"- 共享目录: {meeting.workspace_dir}")

    # Show current assignments
    if meeting.assignments:
        lines.append("")
        lines.append("## 当前任务")
        for a in meeting.assignments:
            st = a.status.value if hasattr(a.status, "value") else str(a.status)
            lines.append(f"- [{st}] {a.title} → {a.assignee_agent_id or '待分配'}")

    lines.extend([
        "",
        f"## 你的身份",
        f"角色: {role}",
        f"名字: {name}",
        "",
        "## 发言要求（严格遵守，否则被视为无效发言）",
        "1. **聚焦当前话题**：只针对主持人最新发言涉及的那一个话题回应，不要主动展开其他话题",
        "2. **立场表态只说一次**：若你之前已在本会议中就这个议题表过态，**不要重复立场**。只补充全新的视角/数据/风险；如果没有新东西可补充，请直接说「我与之前发言一致，无补充」",
        "3. **禁止复述他人观点**：前面的发言（无论主持人还是其他 Agent）中已出现的论点，不得再次写出",
        "4. **简短为先**：默认控制在 120 字内。只有在提出全新专业分析时可到 250 字",
        "5. **响应主持人的指令**：若主持人要求某件具体事情（做洞察、出方案、暂停等），直接照做，不要把它当成新议题再讨论",
        "6. 投票/行动项只在主持人明确要求投票/分派任务时写，不要每次发言都附带",
        "",
        "── 会议讨论记录 ──",
    ])
    msgs = list(meeting.messages or [])[-tail:]
    for m in msgs:
        sname = getattr(m, "sender_name", "") or getattr(m, "sender", "user")
        content = getattr(m, "content", "") or ""
        mrole = getattr(m, "role", "")
        tag = "[主持人]" if mrole == "user" else "[Agent]"
        lines.append(f"{tag} {sname}: {content}")
    lines.append("")
    lines.append(f"主持人最新发言: {user_msg}")
    lines.append("请基于以上讨论记录，给出你的观点和洞察。注意不要重复已有的发言内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reply sequence generation tracking — lets the user interrupt a running
# agent reply sequence by posting a new message. Each meeting has a
# monotonically increasing generation counter; each spawned reply sequence
# captures the current generation at start and bails out between agents if
# it no longer matches (meaning a newer user message / interrupt landed).
# ---------------------------------------------------------------------------

_meeting_reply_gen: dict[str, int] = {}
_meeting_reply_gen_lock = threading.Lock()


# Keywords that mean "stop talking, don't trigger another agent round".
# Kept narrow on purpose: must be a short meta-command, not a substantive message.
_STOP_KEYWORDS = (
    "暂停", "停止", "停一下", "先停", "先暂停", "大家停", "大家先停",
    "别说了", "先别说", "闭嘴", "安静", "等一下", "等等",
    "stop", "pause", "wait", "hold on", "quiet",
)


def is_stop_command(msg: str) -> bool:
    """Return True if the message is a meta-command to halt the discussion
    rather than a substantive utterance that should trigger a new reply
    round. The heuristic is deliberately conservative: message must be
    short (<= 20 chars after strip) AND contain one of the stop keywords.
    """
    if not msg:
        return False
    s = msg.strip()
    if not s or len(s) > 20:
        return False
    low = s.lower()
    for kw in _STOP_KEYWORDS:
        if kw in low:
            return True
    return False


def bump_meeting_reply_gen(meeting_id: str) -> int:
    """Advance the meeting's reply generation. Any in-flight sequence for
    this meeting will see the mismatch and abort at its next iteration.
    Returns the new generation value.
    """
    with _meeting_reply_gen_lock:
        _meeting_reply_gen[meeting_id] = _meeting_reply_gen.get(meeting_id, 0) + 1
        return _meeting_reply_gen[meeting_id]


def current_meeting_reply_gen(meeting_id: str) -> int:
    with _meeting_reply_gen_lock:
        return _meeting_reply_gen.get(meeting_id, 0)


def meeting_agent_reply(meeting: "Meeting",
                          registry: "MeetingRegistry",
                          agent_chat_fn: Callable[[str, str], str],
                          agent_lookup_fn: Callable[[str], object],
                          user_msg: str,
                          target_agent_ids: Optional[list[str]] = None,
                          max_participants: int = 10,
                          multimodal_parts: Optional[list[dict]] = None,
                          gen: Optional[int] = None) -> None:
    """Trigger each participant agent to reply in the meeting.

    Runs synchronously — callers that want non-blocking should wrap this
    in a daemon thread.

    If ``gen`` is provided, the sequence is cancellable: between each agent
    it verifies the meeting's current generation still equals ``gen``, and
    bails out if not (meaning the user posted a new message / interrupted).
    """
    if meeting.status in (MeetingStatus.CLOSED, MeetingStatus.CANCELLED):
        return
    targets = target_agent_ids or list(meeting.participants or [])
    # de-dup, cap, filter empty
    seen = set()
    chosen = []
    for aid in targets:
        if not aid or aid in seen:
            continue
        seen.add(aid)
        chosen.append(aid)
        if len(chosen) >= max_participants:
            break

    for aid in chosen:
        # -- User-priority interrupt check --
        if gen is not None and current_meeting_reply_gen(meeting.id) != gen:
            logger.info(
                "meeting %s reply sequence aborted (gen=%s, current=%s) — user interrupt",
                meeting.id, gen, current_meeting_reply_gen(meeting.id),
            )
            return
        if meeting.status in (MeetingStatus.CLOSED, MeetingStatus.CANCELLED):
            return
        ag = None
        try:
            ag = agent_lookup_fn(aid)
        except Exception:
            ag = None
        if not ag:
            continue
        try:
            prompt = _build_meeting_prompt(meeting, ag, user_msg)
            # If multimodal content exists, build list-format message
            if multimodal_parts:
                chat_msg = [{"type": "text", "text": prompt}] + list(multimodal_parts)
            else:
                chat_msg = prompt
            reply = agent_chat_fn(aid, chat_msg)
        except Exception as e:
            reply = f"❌ 回复失败: {e}"
        # -- Post-LLM interrupt check: if user interrupted while this agent
        #    was talking, drop its stale reply instead of polluting the log --
        if gen is not None and current_meeting_reply_gen(meeting.id) != gen:
            logger.info(
                "meeting %s dropping stale reply from %s — user interrupt landed mid-call",
                meeting.id, aid,
            )
            return
        try:
            role = getattr(ag, "role", "") or "agent"
            aname = getattr(ag, "name", "") or aid
            meeting.add_message(
                sender=aid,
                sender_name=f"{role}-{aname}",
                role="assistant",
                content=reply or "",
            )
            registry.save()
        except Exception:
            pass


def spawn_meeting_reply(meeting, registry, agent_chat_fn, agent_lookup_fn,
                         user_msg, target_agent_ids=None,
                         multimodal_parts=None):
    """Fire-and-forget daemon thread wrapper for meeting_agent_reply.

    Bumps the meeting's reply generation so that any previously-running
    sequence for this meeting aborts at its next iteration — giving the
    user priority to interrupt the discussion.
    """
    gen = bump_meeting_reply_gen(meeting.id)
    t = threading.Thread(
        target=meeting_agent_reply,
        args=(meeting, registry, agent_chat_fn, agent_lookup_fn, user_msg),
        kwargs={"target_agent_ids": target_agent_ids,
                "multimodal_parts": multimodal_parts,
                "gen": gen},
        daemon=True,
    )
    t.start()
    return t
