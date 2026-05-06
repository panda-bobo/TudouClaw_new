"""Team Dashboard — Phase 2 P2-6 (2026-05-06).

Single source of truth for "what is each agent doing right now in this
project". Replaces the scatter of free-form 'project_status_report.md /
milestone_tracker.md / 任务派发_X.md' that agents currently re-parse on
every turn.

Data model (SQLite, one row per agent-in-project):
  agent_status(
    agent_id, project_id, scenario, current_action, started_at,
    last_progress_at, blocked_by, deliverable_progress (json)
  )

Writers:
  * Agent.chat() turn entry → update_status(agent, "starting", ...)
  * Per-response cap status JSON → update_status(agent, status_json)
  * Deliverable check pass → bump deliverable_progress
  * Task complete → mark_done(agent, task_id, deliverables_actual)
  * Watcher interventions → log_intervention(agent, kind, detail)

Readers:
  * query_team_status(project_id) tool → list[AgentStatusRow]
  * query_agent_status(agent_id) tool  → AgentStatusRow
  * UI "Team Status" tab → polls query_team_status

This is a SINGLE TABLE, append-mostly, < 1ms per write. No locks
beyond SQLite's; safe under concurrent agent threads via WAL mode.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_status (
    agent_id            TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    scenario_kind       TEXT DEFAULT '',
    task_id             TEXT DEFAULT '',
    task_title          TEXT DEFAULT '',
    current_action      TEXT DEFAULT '',
    next_action         TEXT DEFAULT '',
    blocked_by          TEXT DEFAULT '',
    started_at          REAL NOT NULL,
    last_update_at      REAL NOT NULL,
    last_progress_at    REAL NOT NULL,
    deliverable_progress TEXT DEFAULT '{}',
    PRIMARY KEY (agent_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_as_project
    ON agent_status(project_id, last_update_at DESC);

CREATE TABLE IF NOT EXISTS agent_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    project_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,           -- start | status | progress | done | intervention | error
    detail     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_asl_project_ts
    ON agent_status_log(project_id, ts DESC);
"""


class TeamDashboard:
    """SQLite-backed per-project team status table. One per data-dir."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(os.path.dirname(db_path)).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL gives us non-blocking reads from the UI while writers
        # hold the write lock briefly per update.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ── Writers ──

    def update_status(self, agent_id: str, project_id: str, *,
                      task_id: str = "", task_title: str = "",
                      current_action: str = "", next_action: str = "",
                      blocked_by: str = "", scenario_kind: str = "",
                      deliverable_progress: dict | None = None,
                      progress: bool = False) -> None:
        """Upsert a row. ``progress=True`` resets last_progress_at."""
        now = time.time()
        prog_json = json.dumps(deliverable_progress or {}, ensure_ascii=False)
        with self._lock:
            existing = self._conn.execute(
                "SELECT started_at, last_progress_at FROM agent_status "
                "WHERE agent_id=? AND project_id=?",
                (agent_id, project_id),
            ).fetchone()
            if existing is None:
                started_at = now
                last_progress_at = now
            else:
                started_at = float(existing["started_at"])
                last_progress_at = (now if progress
                                    else float(existing["last_progress_at"]))
            self._conn.execute(
                """INSERT INTO agent_status (
                       agent_id, project_id, scenario_kind, task_id,
                       task_title, current_action, next_action, blocked_by,
                       started_at, last_update_at, last_progress_at,
                       deliverable_progress)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id, project_id) DO UPDATE SET
                       scenario_kind=excluded.scenario_kind,
                       task_id=excluded.task_id,
                       task_title=excluded.task_title,
                       current_action=excluded.current_action,
                       next_action=excluded.next_action,
                       blocked_by=excluded.blocked_by,
                       last_update_at=excluded.last_update_at,
                       last_progress_at=excluded.last_progress_at,
                       deliverable_progress=excluded.deliverable_progress""",
                (agent_id, project_id, scenario_kind, task_id, task_title,
                 current_action, next_action, blocked_by, started_at,
                 now, last_progress_at, prog_json),
            )
            self._conn.commit()

    def log_event(self, agent_id: str, project_id: str, kind: str,
                  detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_status_log (agent_id, project_id, ts, kind, detail) "
                "VALUES (?,?,?,?,?)",
                (agent_id, project_id, time.time(), kind, detail[:500]),
            )
            self._conn.commit()

    def mark_done(self, agent_id: str, project_id: str,
                  detail: str = "") -> None:
        self.update_status(agent_id, project_id,
                           current_action="", blocked_by="",
                           progress=True)
        self.log_event(agent_id, project_id, "done", detail)

    # ── Readers (UI / tools) ──

    def query_project(self, project_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_status WHERE project_id=? "
            "ORDER BY last_update_at DESC",
            (project_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def query_agent(self, agent_id: str,
                    project_id: str = "") -> Optional[dict]:
        if project_id:
            row = self._conn.execute(
                "SELECT * FROM agent_status WHERE agent_id=? AND project_id=?",
                (agent_id, project_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM agent_status WHERE agent_id=? "
                "ORDER BY last_update_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def recent_events(self, project_id: str, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_status_log WHERE project_id=? "
            "ORDER BY ts DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        if d.get("deliverable_progress"):
            try:
                d["deliverable_progress"] = json.loads(d["deliverable_progress"])
            except Exception:
                d["deliverable_progress"] = {}
        return d


# ── Singleton ──

_DASH: Optional[TeamDashboard] = None
_DASH_LOCK = threading.Lock()


def get_dashboard() -> TeamDashboard:
    global _DASH
    if _DASH is not None:
        return _DASH
    with _DASH_LOCK:
        if _DASH is None:
            home = os.environ.get("TUDOU_HOME") or os.path.expanduser("~/.tudou_claw")
            _DASH = TeamDashboard(os.path.join(home, "team_dashboard.db"))
    return _DASH


# Convenience accessors (so callers don't have to import the class)

def update_status(agent_id: str, project_id: str, **kwargs: Any) -> None:
    if not project_id:
        return
    try:
        get_dashboard().update_status(agent_id, project_id, **kwargs)
    except Exception:
        pass


def log_event(agent_id: str, project_id: str, kind: str,
              detail: str = "") -> None:
    if not project_id:
        return
    try:
        get_dashboard().log_event(agent_id, project_id, kind, detail)
    except Exception:
        pass


def mark_done(agent_id: str, project_id: str, detail: str = "") -> None:
    if not project_id:
        return
    try:
        get_dashboard().mark_done(agent_id, project_id, detail)
    except Exception:
        pass
