"""
Chat task management — background async chat tasks.

Extracted from agent.py to reduce file size.
"""
from __future__ import annotations
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ChatTaskStatus(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    STREAMING = "streaming"
    TOOL_EXEC = "tool_exec"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class ChatTask:
    """A background chat task that runs independently of the HTTP connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    user_message: str = ""
    status: ChatTaskStatus = ChatTaskStatus.QUEUED
    progress: int = 0           # 0-100
    phase: str = ""             # human-readable phase description
    result: str = ""            # final assistant text
    error: str = ""
    events: list = field(default_factory=list)   # SSE event dicts
    _event_cursor: int = 0      # for clients to track what they've read
    aborted: bool = False       # abort flag checked by chat loop
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def abort(self):
        """Signal the task to stop."""
        self.aborted = True
        self.set_status(ChatTaskStatus.ABORTED, "Aborted by user", -1)
        self.push_event({"type": "error", "content": "Task aborted by user"})
        self.push_event({"type": "done"})

    def push_event(self, evt: dict):
        """Thread-safe event push."""
        with self._lock:
            self.events.append(evt)
            self.updated_at = time.time()

    def get_events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return events since cursor and new cursor position."""
        with self._lock:
            new_events = self.events[cursor:]
            return new_events, len(self.events)

    def set_status(self, status: ChatTaskStatus, phase: str = "",
                   progress: int = -1):
        with self._lock:
            self.status = status
            if phase:
                self.phase = phase
            if progress >= 0:
                self.progress = min(progress, 100)
            self.updated_at = time.time()

        # Propagate terminal state to any attached ConversationTask so
        # the UI / resume logic sees DONE / FAILED. Best-effort: the
        # conversation observer silently returns if no task is linked.
        if status in (ChatTaskStatus.COMPLETED, ChatTaskStatus.FAILED,
                      ChatTaskStatus.ABORTED):
            try:
                from .conversation_observer import mark_done
                mark_done(
                    agent_id=self.agent_id,
                    chat_task_id=self.id,
                    failed=(status != ChatTaskStatus.COMPLETED),
                )
            except Exception:
                pass

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "progress": self.progress,
            "phase": self.phase,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "event_count": len(self.events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ChatTaskManager:
    """Manages background chat tasks for all agents."""

    def __init__(self):
        self._tasks: dict[str, ChatTask] = {}   # task_id -> ChatTask
        self._agent_tasks: dict[str, list[str]] = {}  # agent_id -> [task_ids]
        self._lock = threading.Lock()

    def create_task(self, agent_id: str, user_message: str) -> ChatTask:
        task = ChatTask(agent_id=agent_id, user_message=user_message)
        with self._lock:
            self._tasks[task.id] = task
            if agent_id not in self._agent_tasks:
                self._agent_tasks[agent_id] = []
            self._agent_tasks[agent_id].append(task.id)
            # Keep only last 50 tasks per agent
            if len(self._agent_tasks[agent_id]) > 50:
                old_id = self._agent_tasks[agent_id].pop(0)
                self._tasks.pop(old_id, None)
        return task

    def get_task(self, task_id: str) -> ChatTask | None:
        return self._tasks.get(task_id)

    def get_agent_tasks(self, agent_id: str) -> list[ChatTask]:
        with self._lock:
            task_ids = self._agent_tasks.get(agent_id, [])
            return [self._tasks[tid] for tid in task_ids
                    if tid in self._tasks]


# Global task manager singleton
_chat_task_manager: ChatTaskManager | None = None


def get_chat_task_manager() -> ChatTaskManager:
    global _chat_task_manager
    if _chat_task_manager is None:
        _chat_task_manager = ChatTaskManager()
    return _chat_task_manager
