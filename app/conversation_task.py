"""ConversationTask — persistent record of a V1 chat that crosses the
"complex enough to track" threshold.

Not to be confused with:
  - ``app.agent.AgentTask``  (V1 todo-style task on the agent's backlog)
  - ``app.agent.ChatTask``   (in-flight async chat request managed by
                              ``ChatTaskManager``)
  - ``app.v2.core.task.Task`` (V2 state-machine — deprecated)

What this IS
------------
A thin record attached to complex chat conversations so the UI can
show them in the TASK QUEUE panel with plan + step progress, and so
interrupted conversations can resume after a server restart.

The V1 chat loop is still the execution engine. This record just
observes and annotates — no new LLM calls, no state machine, no
structured-JSON coercion.

Lifecycle
---------

    user message → classifier
                   │
        ┌──────────┴──────────┐
     simple                complex
   (chitchat)          (tool delivery)
        │                    │
   no record        ConversationTask row created
                            │
                     agent.chat_async runs as usual
                            │
                     hook observes emitted events:
                       • plan extracted → steps set
                       • tool_call      → push to step.tool_calls
                       • step ✓ marker  → advance current_step_idx
                       • final message  → status=done
                            │
                     row updated, UI reflects in real time

Persistence
-----------
SQLite table ``conversation_tasks`` keyed by ``task_id`` with one
``data`` JSON blob — schema migrations stay cheap. Only hot columns
are promoted to actual SQL columns (``agent_id``, ``status``,
``updated_at``) for query filters / ORDER BY.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("tudou.conversation_task")


# ── Status tags (loose strings so JSON persistence is trivial) ─────────

class ConversationTaskStatus:
    RUNNING   = "running"    # agent is actively working
    PAUSED    = "paused"     # server restarted / user closed tab; resumable
    DONE      = "done"       # agent reported completion
    FAILED    = "failed"     # unrecoverable error
    CANCELLED = "cancelled"  # user aborted


# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class ConversationStep:
    """A single step in the agent's plan."""
    id: str = ""
    goal: str = ""                 # human-readable description
    tool_hint: str = ""            # tool the model plans to use (may be empty)
    status: str = "pending"        # pending | running | done | skipped
    tool_calls: list[dict] = field(default_factory=list)
    # Each tool_calls entry: {name, arguments_preview, result_preview, ts}
    started_at: float = 0.0
    completed_at: float = 0.0


@dataclass
class ConversationTask:
    id: str = field(default_factory=lambda: "ct_" + uuid.uuid4().hex[:12])
    agent_id: str = ""
    title: str = ""                # short label for UI (first ~40 chars of intent)
    intent: str = ""               # the user's message verbatim
    status: str = "running"
    steps: list[ConversationStep] = field(default_factory=list)
    current_step_idx: int = 0
    # Link to underlying V1 ChatTask so SSE consumers can follow:
    chat_task_id: str = ""
    # Last assistant content preview (for resume prompt / UI summary):
    last_assistant_preview: str = ""
    # Count of tool calls across all steps (for UI "⟳ 7 tools called"):
    tool_call_total: int = 0
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ConversationTask":
        steps_raw = d.get("steps") or []
        steps = [
            ConversationStep(**s) if isinstance(s, dict) and not isinstance(s, ConversationStep)
            else (s if isinstance(s, ConversationStep) else ConversationStep())
            for s in steps_raw
        ]
        return ConversationTask(
            id=d.get("id", ""),
            agent_id=d.get("agent_id", ""),
            title=d.get("title", ""),
            intent=d.get("intent", ""),
            status=d.get("status", ConversationTaskStatus.RUNNING),
            steps=steps,
            current_step_idx=int(d.get("current_step_idx", 0) or 0),
            chat_task_id=d.get("chat_task_id", ""),
            last_assistant_preview=d.get("last_assistant_preview", ""),
            tool_call_total=int(d.get("tool_call_total", 0) or 0),
            created_by=d.get("created_by", ""),
            created_at=float(d.get("created_at", 0.0) or time.time()),
            updated_at=float(d.get("updated_at", 0.0) or time.time()),
            completed_at=float(d.get("completed_at", 0.0) or 0.0),
        )


# ── Persistence ────────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_tasks (
    task_id    TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ct_agent   ON conversation_tasks(agent_id);
CREATE INDEX IF NOT EXISTS idx_ct_status  ON conversation_tasks(status);
CREATE INDEX IF NOT EXISTS idx_ct_updated ON conversation_tasks(updated_at DESC);
"""


class ConversationTaskStore:
    """Thread-safe SQLite store for ConversationTask. One instance
    per data_dir. Callers usually want ``get_store()`` for the process
    singleton."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── CRUD ──────────────────────────────────────────────────────────

    def save(self, task: ConversationTask) -> None:
        task.updated_at = time.time()
        payload = json.dumps(task.to_dict(), ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO conversation_tasks(
                        task_id, agent_id, status, created_at, updated_at, data)
                        VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                        agent_id=excluded.agent_id,
                        status=excluded.status,
                        updated_at=excluded.updated_at,
                        data=excluded.data""",
                (task.id, task.agent_id, task.status,
                 task.created_at, task.updated_at, payload),
            )

    def get(self, task_id: str) -> Optional[ConversationTask]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM conversation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return ConversationTask.from_dict(json.loads(row["data"]))
        except Exception as e:   # noqa: BLE001
            logger.warning("ConversationTaskStore.get(%s) parse error: %s", task_id, e)
            return None

    def list_for_agent(self, agent_id: str,
                       include_terminal: bool = True,
                       limit: int = 50) -> list[ConversationTask]:
        """Return tasks newest-first. include_terminal=False hides
        done/failed/cancelled rows — the "active queue" view."""
        q = "SELECT data FROM conversation_tasks WHERE agent_id = ?"
        args: list = [agent_id]
        if not include_terminal:
            q += " AND status NOT IN (?, ?, ?)"
            args += [ConversationTaskStatus.DONE,
                     ConversationTaskStatus.FAILED,
                     ConversationTaskStatus.CANCELLED]
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        out: list[ConversationTask] = []
        for r in rows:
            try:
                out.append(ConversationTask.from_dict(json.loads(r["data"])))
            except Exception:   # noqa: BLE001
                continue
        return out

    def list_resumable(self, agent_id: str = "") -> list[ConversationTask]:
        """Tasks in RUNNING or PAUSED — candidates for M4 resume.

        ``agent_id`` empty → global scan (startup recovery). Otherwise
        restrict to that agent.
        """
        q = "SELECT data FROM conversation_tasks WHERE status IN (?, ?)"
        args: list = [ConversationTaskStatus.RUNNING,
                      ConversationTaskStatus.PAUSED]
        if agent_id:
            q += " AND agent_id = ?"
            args.append(agent_id)
        q += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [ConversationTask.from_dict(json.loads(r["data"])) for r in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM conversation_tasks WHERE task_id = ?",
                (task_id,),
            )
            return cur.rowcount > 0

    def mark_paused_if_running(self) -> int:
        """On server startup, flip every RUNNING task to PAUSED.
        Rationale: the process that owned the row is dead. PAUSED is a
        hint to the UI ("continue?") and keeps the row visible.

        Must rewrite the full JSON blob — not just the ``status`` column —
        because ``get()`` deserialises the blob and callers expect the
        in-blob ``status`` field to match.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, data FROM conversation_tasks WHERE status = ?",
                (ConversationTaskStatus.RUNNING,),
            ).fetchall()
            if not rows:
                return 0
            now = time.time()
            for r in rows:
                try:
                    d = json.loads(r["data"])
                except Exception:
                    continue
                d["status"] = ConversationTaskStatus.PAUSED
                d["updated_at"] = now
                conn.execute(
                    """UPDATE conversation_tasks
                          SET status = ?, updated_at = ?, data = ?
                        WHERE task_id = ?""",
                    (ConversationTaskStatus.PAUSED, now,
                     json.dumps(d, ensure_ascii=False), r["task_id"]),
                )
            return len(rows)


# ── Singleton ──────────────────────────────────────────────────────────

_store_singleton: Optional[ConversationTaskStore] = None
_store_singleton_lock = threading.Lock()


def get_store(data_dir: str = "") -> ConversationTaskStore:
    """Return the process-wide ``ConversationTaskStore``.

    ``data_dir`` is only honoured on the first call. Subsequent calls
    return the same instance regardless of the passed path.
    """
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    with _store_singleton_lock:
        if _store_singleton is not None:
            return _store_singleton
        if not data_dir:
            try:
                from . import DEFAULT_DATA_DIR
                data_dir = os.environ.get(
                    "TUDOU_CLAW_DATA_DIR", DEFAULT_DATA_DIR)
            except Exception:
                data_dir = os.path.expanduser("~/.tudou_claw")
        db_path = os.path.join(data_dir, "conversation_tasks.db")
        _store_singleton = ConversationTaskStore(db_path)
    return _store_singleton


def _reset_singleton_for_tests() -> None:
    """Called by test fixtures that swap ``TUDOU_CLAW_DATA_DIR``."""
    global _store_singleton
    with _store_singleton_lock:
        _store_singleton = None


# ── Resume-prompt builder ─────────────────────────────────────────────
#
# Pure function — no I/O, no globals. Easy to unit-test and safe to
# reuse from batch-resume scripts or CLI tools that want the same
# continuation string without going through the REST layer.


def build_resume_prompt(task: ConversationTask) -> str:
    """Produce the user-facing continuation message for a PAUSED task.

    The resulting string is what we POST into the chat endpoint as a
    fresh user message when the operator clicks "Continue". The agent
    reads it, realises it's a resume, and picks up from the first
    un-finished step.

    Kept deliberately short: the agent's own conversation history
    still has the full original thread, so this is just a reminder
    header — not a re-narration of everything already done.
    """
    done_steps = [s for s in (task.steps or []) if s.status == "done"]
    todo_steps = [s for s in (task.steps or []) if s.status != "done"]

    lines: list[str] = [
        "[继续任务 · 之前中断了]",
        f"原始请求：{task.intent}",
    ]
    if done_steps:
        lines.append("已完成：")
        for i, step in enumerate(done_steps, 1):
            lines.append(f"  {i}. {step.goal}")
    if todo_steps:
        lines.append("还要做：")
        for i, step in enumerate(todo_steps, 1):
            suffix = f"（工具: {step.tool_hint}）" if step.tool_hint else ""
            lines.append(f"  {i}. {step.goal}{suffix}")
        lines.append("请从未完成的第一步继续。")
    else:
        lines.append("请检查现有状态并完成未尽事宜。")
    return "\n".join(lines)
