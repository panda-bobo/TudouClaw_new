"""
TaskStore — SQLite persistence for V2 (PRD §7.2).

Tables: ``agents_v2``, ``tasks_v2``, ``task_events_v2``.
V2 uses its own SQLite file ``~/.tudou_claw/tudou.db`` (note: V1 lives
in ``tudou_claw.db`` — different file). The two systems are
file-isolated; V1 data is reached via the V1 ``Database`` class, not
through this store.

Thread-safety:
    sqlite3 connections aren't thread-safe by default, so every write path
    opens a short-lived connection. Readers do the same. This is fine for
    a local-single-user tool; if contention ever matters we add a writer
    queue, not WAL mode alone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Iterable, Optional, TYPE_CHECKING

from .task import Task
from .task_events import TaskEvent

if TYPE_CHECKING:
    from ..agent.agent_v2 import AgentV2


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents_v2 (
  id                      TEXT PRIMARY KEY,
  name                    TEXT NOT NULL,
  role                    TEXT NOT NULL,
  v1_agent_id             TEXT DEFAULT '',
  capabilities_json       TEXT NOT NULL,
  task_template_ids_json  TEXT DEFAULT '[]',
  working_directory       TEXT DEFAULT '',
  archived                INTEGER DEFAULT 0,
  created_at              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_v2_role     ON agents_v2(role);
CREATE INDEX IF NOT EXISTS idx_agents_v2_archived ON agents_v2(archived);

CREATE TABLE IF NOT EXISTS tasks_v2 (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  parent_task_id  TEXT DEFAULT '',
  template_id     TEXT DEFAULT '',
  intent          TEXT NOT NULL,
  phase           TEXT NOT NULL,
  status          TEXT NOT NULL,
  priority        INTEGER DEFAULT 5,
  timeout_s       INTEGER DEFAULT 1800,
  finished_reason TEXT DEFAULT '',
  plan_json       TEXT DEFAULT '{}',
  context_json    TEXT DEFAULT '{}',
  artifacts_json  TEXT DEFAULT '[]',
  lessons_json    TEXT DEFAULT '[]',
  retries_json    TEXT DEFAULT '{}',
  created_at      REAL NOT NULL,
  started_at      REAL,
  updated_at      REAL NOT NULL,
  completed_at    REAL,
  FOREIGN KEY (agent_id) REFERENCES agents_v2(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_v2_agent   ON tasks_v2(agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_v2_status  ON tasks_v2(status);
CREATE INDEX IF NOT EXISTS idx_tasks_v2_created ON tasks_v2(created_at DESC);

CREATE TABLE IF NOT EXISTS task_events_v2 (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  ts            REAL NOT NULL,
  phase         TEXT NOT NULL,
  type          TEXT NOT NULL,
  payload_json  TEXT DEFAULT '{}',
  FOREIGN KEY (task_id) REFERENCES tasks_v2(id)
);
CREATE INDEX IF NOT EXISTS idx_events_v2_task ON task_events_v2(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_v2_type ON task_events_v2(type);
"""


def _default_db_path() -> str:
    # Import lazily so test setups can monkeypatch DEFAULT_DATA_DIR.
    from app import DEFAULT_DATA_DIR
    return os.environ.get("TUDOU_CLAW_DB_PATH") or os.path.join(DEFAULT_DATA_DIR, "tudou.db")


class TaskStore:
    """Thin SQLite wrapper. One TaskStore per process is sufficient."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    # ── schema ─────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL reduces reader/writer contention; harmless if already set.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.DatabaseError:
            pass
        return conn

    # ── Agent ──────────────────────────────────────────────────────────

    def save_agent(self, agent: "AgentV2") -> None:
        from dataclasses import asdict
        row = {
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "v1_agent_id": agent.v1_agent_id,
            "capabilities_json": json.dumps(asdict(agent.capabilities), ensure_ascii=False),
            "task_template_ids_json": json.dumps(list(agent.task_template_ids), ensure_ascii=False),
            "working_directory": agent.working_directory,
            "archived": 1 if getattr(agent, "archived", False) else 0,
            "created_at": agent.created_at,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents_v2(id, name, role, v1_agent_id,
                    capabilities_json, task_template_ids_json,
                    working_directory, archived, created_at)
                VALUES (:id, :name, :role, :v1_agent_id,
                    :capabilities_json, :task_template_ids_json,
                    :working_directory, :archived, :created_at)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    role=excluded.role,
                    v1_agent_id=excluded.v1_agent_id,
                    capabilities_json=excluded.capabilities_json,
                    task_template_ids_json=excluded.task_template_ids_json,
                    working_directory=excluded.working_directory,
                    archived=excluded.archived
                """,
                row,
            )

    def get_agent(self, agent_id: str) -> Optional["AgentV2"]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM agents_v2 WHERE id = ?", (agent_id,)
            ).fetchone()
        return self._row_to_agent(r) if r else None

    def list_agents(self, role: str = "", include_archived: bool = False) -> list["AgentV2"]:
        q = "SELECT * FROM agents_v2 WHERE 1=1"
        args: list = []
        if role:
            q += " AND role = ?"
            args.append(role)
        if not include_archived:
            q += " AND archived = 0"
        q += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [a for a in (self._row_to_agent(r) for r in rows) if a]

    def archive_agent(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents_v2 SET archived = 1 WHERE id = ?",
                (agent_id,),
            )

    # ── Task ───────────────────────────────────────────────────────────

    def save(self, task: Task) -> None:
        row = task.to_persist_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks_v2(id, agent_id, parent_task_id, template_id,
                    intent, phase, status, priority, timeout_s, finished_reason,
                    plan_json, context_json, artifacts_json, lessons_json, retries_json,
                    created_at, started_at, updated_at, completed_at)
                VALUES (:id, :agent_id, :parent_task_id, :template_id,
                    :intent, :phase, :status, :priority, :timeout_s, :finished_reason,
                    :plan_json, :context_json, :artifacts_json, :lessons_json, :retries_json,
                    :created_at, :started_at, :updated_at, :completed_at)
                ON CONFLICT(id) DO UPDATE SET
                    parent_task_id=excluded.parent_task_id,
                    template_id=excluded.template_id,
                    intent=excluded.intent,
                    phase=excluded.phase,
                    status=excluded.status,
                    priority=excluded.priority,
                    timeout_s=excluded.timeout_s,
                    finished_reason=excluded.finished_reason,
                    plan_json=excluded.plan_json,
                    context_json=excluded.context_json,
                    artifacts_json=excluded.artifacts_json,
                    lessons_json=excluded.lessons_json,
                    retries_json=excluded.retries_json,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at,
                    completed_at=excluded.completed_at
                """,
                row,
            )

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tasks_v2 WHERE id = ?", (task_id,)
            ).fetchone()
        return Task.from_persist_dict(dict(r)) if r else None

    def list_tasks(
        self,
        *,
        agent_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        q = "SELECT * FROM tasks_v2 WHERE 1=1"
        args: list = []
        if agent_id:
            q += " AND agent_id = ?"
            args.append(agent_id)
        if status:
            q += " AND status = ?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [Task.from_persist_dict(dict(r)) for r in rows]

    def count_active_tasks(self, agent_id: str) -> int:
        """Count tasks currently occupying the agent (``RUNNING`` or
        ``PAUSED``). ``QUEUED`` tasks don't count as active — they're
        waiting to be promoted when the agent becomes free.

        Used by submit_task to decide whether a new task starts
        immediately or enters the queue.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM tasks_v2
                 WHERE agent_id = ?
                   AND status IN ('running', 'paused')
                """,
                (agent_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def next_queued_for_agent(self, agent_id: str) -> Optional[Task]:
        """Oldest QUEUED task for this agent, or ``None`` if the queue
        is empty. Used by the dispatcher to promote the next task when
        the agent becomes free."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks_v2
                 WHERE agent_id = ? AND status = 'queued'
                 ORDER BY created_at ASC LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
        return Task.from_persist_dict(dict(row)) if row else None

    def list_queued_for_agent(self, agent_id: str) -> list[Task]:
        """All QUEUED tasks for this agent in FIFO order (for UI)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks_v2
                 WHERE agent_id = ? AND status = 'queued'
                 ORDER BY created_at ASC
                """,
                (agent_id,),
            ).fetchall()
        return [Task.from_persist_dict(dict(r)) for r in rows]

    def list_orphaned_running(self) -> list[Task]:
        """Tasks persisted as RUNNING with no in-process thread.

        Used on startup by crash recovery: anything still RUNNING in the
        DB after a restart is by definition orphaned (all loops live in
        daemon threads that died with the old process)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks_v2 WHERE status = 'running' "
                "ORDER BY updated_at ASC"
            ).fetchall()
        return [Task.from_persist_dict(dict(r)) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        """Hard-delete a task and its event log.

        Only safe for tasks in a terminal state (``completed``/``failed``/
        ``cancelled``). Running or paused tasks should be cancelled
        first. Returns True if a row was removed.

        Event log is FKed via task_id, but we don't have an FK
        constraint in the schema, so we purge both tables manually in
        the same transaction.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM task_events_v2 WHERE task_id = ?", (task_id,)
            )
            cur2 = conn.execute(
                "DELETE FROM tasks_v2 WHERE id = ?", (task_id,)
            )
            conn.commit()
            return cur2.rowcount > 0

    # ── Event ──────────────────────────────────────────────────────────

    def append_event(self, evt: TaskEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events_v2(task_id, ts, phase, type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    evt.task_id,
                    evt.ts,
                    evt.phase,
                    evt.type,
                    json.dumps(evt.payload, ensure_ascii=False),
                ),
            )

    def append_events_batch(self, events: Iterable[TaskEvent]) -> None:
        rows = [
            (
                e.task_id,
                e.ts,
                e.phase,
                e.type,
                json.dumps(e.payload, ensure_ascii=False),
            )
            for e in events
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO task_events_v2(task_id, ts, phase, type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load_events(self, task_id: str, since_ts: float = 0.0) -> list[TaskEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, ts, phase, type, payload_json
                  FROM task_events_v2
                 WHERE task_id = ? AND ts > ?
                 ORDER BY ts ASC, id ASC
                """,
                (task_id, float(since_ts)),
            ).fetchall()
        return [
            TaskEvent(
                task_id=r["task_id"],
                ts=float(r["ts"]),
                phase=r["phase"],
                type=r["type"],
                payload=json.loads(r["payload_json"] or "{}"),
            )
            for r in rows
        ]

    # ── helpers ────────────────────────────────────────────────────────

    def _row_to_agent(self, r) -> Optional["AgentV2"]:
        if r is None:
            return None
        # Lazy import to avoid circular reference.
        from ..agent.agent_v2 import AgentV2, Capabilities
        caps_d = json.loads(r["capabilities_json"] or "{}")
        caps = Capabilities(
            skills=list(caps_d.get("skills", [])),
            mcps=list(caps_d.get("mcps", [])),
            tools=list(caps_d.get("tools", [])),
            llm_tier=caps_d.get("llm_tier", "default"),
            denied_tools=list(caps_d.get("denied_tools", [])),
            llm_slots=dict(caps_d.get("llm_slots") or {}),
        )
        agent = AgentV2(
            id=r["id"],
            name=r["name"],
            role=r["role"],
            v1_agent_id=r["v1_agent_id"] or "",
            capabilities=caps,
            task_template_ids=list(json.loads(r["task_template_ids_json"] or "[]")),
            working_directory=r["working_directory"] or "",
            created_at=float(r["created_at"]),
        )
        agent.archived = bool(r["archived"])
        return agent


# Process-level singleton — callers typically want one store per process.
_STORE: Optional[TaskStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> TaskStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = TaskStore()
        return _STORE


__all__ = ["TaskStore", "get_store"]
