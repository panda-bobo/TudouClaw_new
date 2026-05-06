"""Structured task handoff — Day 5 (2026-05-05).

Replaces the "PM writes 任务派发_M2-M4_小新.md, Coder reads markdown
and re-interprets" pattern with a structured TaskAssignment object
persisted in SQLite + the receiving agent's inbox.

Flow:

  PM agent             →  dispatch_task tool  →  SQLite + recipient inbox
  Coder agent's turn   →  accept_task tool    →  reads structured object

The Coder agent then:
  * reads ONLY the listed context_refs (not glob-searches)
  * writes ONLY the listed deliverables
  * triggers the Day 1 deliverable contract verifier on each write_file
  * task_complete only when all deliverables verified

Compared to free-form markdown handoff:
  * brief: 1-3 sentences, max 500 chars (forces PM to be concise)
  * context_refs: explicit (file_path, why_relevant) list
  * deliverables: typed (path + must_contain + min_lines)
  * acceptance: optional shell verifier
  * deadline / priority: typed fields, not parsed from prose

The data is persisted in a SQLite table (one per hub) so it survives
agent restarts and can be queried for analytics.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class FileRef:
    """A pinned context file with rationale. Why_relevant tells the
    receiver WHICH SECTION matters, so it doesn't read the whole thing."""
    path: str
    why_relevant: str = ""
    expected_section: str = ""  # e.g. "section 3", "the Constraints table"

    def to_dict(self) -> dict:
        return {"path": self.path,
                "why_relevant": self.why_relevant,
                "expected_section": self.expected_section}

    @staticmethod
    def from_dict(d: dict) -> "FileRef":
        return FileRef(
            path=str(d.get("path", "")),
            why_relevant=str(d.get("why_relevant", "")),
            expected_section=str(d.get("expected_section", "")),
        )


@dataclass
class Deliverable:
    """A required output. Mirrors the StepTemplate contract from Day 1
    AM so the same verifier (deliverable_check.py) can validate."""
    path: str
    kind: str = "code"  # code / doc / data / image / other
    must_contain: list[str] = field(default_factory=list)
    min_lines: int = 0
    max_lines: int = 0
    acceptance_cmd: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind,
                "must_contain": list(self.must_contain),
                "min_lines": self.min_lines, "max_lines": self.max_lines,
                "acceptance_cmd": self.acceptance_cmd}

    @staticmethod
    def from_dict(d: dict) -> "Deliverable":
        return Deliverable(
            path=str(d.get("path", "")),
            kind=str(d.get("kind", "code")),
            must_contain=list(d.get("must_contain") or []),
            min_lines=int(d.get("min_lines", 0) or 0),
            max_lines=int(d.get("max_lines", 0) or 0),
            acceptance_cmd=str(d.get("acceptance_cmd", "")),
        )


@dataclass
class TaskAssignment:
    """Structured PM → Coder handoff. Atomic — once persisted, immutable
    (PM revisions create a new TaskAssignment)."""
    id: str = field(default_factory=lambda: "ta_" + uuid.uuid4().hex[:10])
    project_id: str = ""
    from_agent: str = ""
    to_agent: str = ""
    brief: str = ""                # 1-3 sentences, ≤ 500 chars
    context_refs: list[FileRef] = field(default_factory=list)
    deliverables: list[Deliverable] = field(default_factory=list)
    deadline: float = 0.0          # epoch seconds; 0 = no deadline
    priority: int = 0              # 0 normal, 1 high, 2 urgent
    project_task_id: str = ""      # optional link to ProjectTask.id
    status: str = "pending"        # pending / accepted / done / cancelled
    created_at: float = field(default_factory=time.time)
    accepted_at: float = 0.0
    done_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "from_agent": self.from_agent, "to_agent": self.to_agent,
            "brief": self.brief,
            "context_refs": [r.to_dict() for r in self.context_refs],
            "deliverables": [d.to_dict() for d in self.deliverables],
            "deadline": self.deadline, "priority": self.priority,
            "project_task_id": self.project_task_id, "status": self.status,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at, "done_at": self.done_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "TaskAssignment":
        return TaskAssignment(
            id=str(d.get("id", "ta_" + uuid.uuid4().hex[:10])),
            project_id=str(d.get("project_id", "")),
            from_agent=str(d.get("from_agent", "")),
            to_agent=str(d.get("to_agent", "")),
            brief=str(d.get("brief", "")),
            context_refs=[FileRef.from_dict(r) for r in (d.get("context_refs") or [])],
            deliverables=[Deliverable.from_dict(x) for x in (d.get("deliverables") or [])],
            deadline=float(d.get("deadline", 0.0) or 0.0),
            priority=int(d.get("priority", 0) or 0),
            project_task_id=str(d.get("project_task_id", "")),
            status=str(d.get("status", "pending")),
            created_at=float(d.get("created_at", time.time()) or time.time()),
            accepted_at=float(d.get("accepted_at", 0.0) or 0.0),
            done_at=float(d.get("done_at", 0.0) or 0.0),
        )

    def render_for_recipient(self) -> str:
        """Render the assignment as plain text for the recipient agent's
        first turn message. Concise, structured — no prose-to-parse."""
        lines = [f"# Task Assignment {self.id}"]
        if self.from_agent:
            lines.append(f"From: {self.from_agent}")
        if self.priority >= 2:
            lines.append("Priority: 🔥 URGENT")
        elif self.priority == 1:
            lines.append("Priority: HIGH")
        if self.deadline:
            lines.append(f"Deadline: {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.deadline))}")
        lines.append("")
        lines.append("## Brief")
        lines.append(self.brief or "(no brief)")
        if self.context_refs:
            lines.append("")
            lines.append("## Context (read ONLY these — DO NOT search/glob)")
            for r in self.context_refs:
                seg = f"  - `{r.path}`"
                if r.why_relevant:
                    seg += f"  ← {r.why_relevant}"
                if r.expected_section:
                    seg += f" [section: {r.expected_section}]"
                lines.append(seg)
        if self.deliverables:
            lines.append("")
            lines.append("## Deliverables (you MUST produce all of these)")
            for d in self.deliverables:
                seg = f"  - `{d.path}` ({d.kind})"
                bits = []
                if d.min_lines:
                    bits.append(f"min {d.min_lines} lines")
                if d.max_lines:
                    bits.append(f"max {d.max_lines} lines")
                if bits:
                    seg += "  " + " · ".join(bits)
                lines.append(seg)
                for needle in d.must_contain[:5]:
                    npreview = needle if len(needle) <= 60 else needle[:60] + "…"
                    lines.append(f"      • must contain: {npreview!r}")
                if len(d.must_contain) > 5:
                    lines.append(f"      • (and {len(d.must_contain) - 5} more constraints)")
                if d.acceptance_cmd:
                    lines.append(f"      • acceptance: `{d.acceptance_cmd}`")
        lines.append("")
        lines.append(
            "When done, call task_complete. The framework verifies every "
            "deliverable's must_contain + min_lines automatically — "
            "task_complete is REJECTED until all pass."
        )
        return "\n".join(lines)


# ── Persistence (SQLite) ────────────────────────────────────────────

class TaskAssignmentStore:
    """SQLite-backed store. One per hub. Created lazily."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS task_assignments (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL DEFAULT '',
        from_agent TEXT NOT NULL DEFAULT '',
        to_agent TEXT NOT NULL DEFAULT '',
        brief TEXT NOT NULL DEFAULT '',
        body_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER DEFAULT 0,
        deadline REAL DEFAULT 0,
        project_task_id TEXT DEFAULT '',
        created_at REAL NOT NULL,
        accepted_at REAL DEFAULT 0,
        done_at REAL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_ta_to_status
        ON task_assignments(to_agent, status);
    CREATE INDEX IF NOT EXISTS idx_ta_project
        ON task_assignments(project_id);
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(os.path.dirname(db_path)).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def insert(self, ta: TaskAssignment) -> None:
        self._conn.execute(
            """INSERT INTO task_assignments
               (id, project_id, from_agent, to_agent, brief, body_json,
                status, priority, deadline, project_task_id,
                created_at, accepted_at, done_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ta.id, ta.project_id, ta.from_agent, ta.to_agent,
             ta.brief, json.dumps(ta.to_dict(), ensure_ascii=False),
             ta.status, ta.priority, ta.deadline, ta.project_task_id,
             ta.created_at, ta.accepted_at, ta.done_at),
        )
        self._conn.commit()

    def get(self, ta_id: str) -> Optional[TaskAssignment]:
        row = self._conn.execute(
            "SELECT body_json FROM task_assignments WHERE id=?",
            (ta_id,),
        ).fetchone()
        if not row:
            return None
        try:
            return TaskAssignment.from_dict(json.loads(row["body_json"]))
        except Exception:
            return None

    def list_inbox(self, agent_id: str,
                   status: str = "pending",
                   limit: int = 20) -> list[TaskAssignment]:
        rows = self._conn.execute(
            """SELECT body_json FROM task_assignments
               WHERE to_agent=? AND status=?
               ORDER BY priority DESC, created_at ASC
               LIMIT ?""",
            (agent_id, status, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(TaskAssignment.from_dict(json.loads(r["body_json"])))
            except Exception:
                continue
        return out

    def update_status(self, ta_id: str, status: str,
                      now: float | None = None) -> bool:
        now = now or time.time()
        ta = self.get(ta_id)
        if ta is None:
            return False
        ta.status = status
        if status == "accepted" and not ta.accepted_at:
            ta.accepted_at = now
        elif status in ("done", "cancelled") and not ta.done_at:
            ta.done_at = now
        self._conn.execute(
            """UPDATE task_assignments
               SET status=?, body_json=?, accepted_at=?, done_at=?
               WHERE id=?""",
            (status, json.dumps(ta.to_dict(), ensure_ascii=False),
             ta.accepted_at, ta.done_at, ta_id),
        )
        self._conn.commit()
        return True


# Singleton accessor — one store per hub data dir
_STORE: Optional[TaskAssignmentStore] = None


def get_store() -> TaskAssignmentStore:
    """Lazily instantiate the global store. The data dir comes from
    TUDOU_HOME or defaults to ~/.tudou_claw."""
    global _STORE
    if _STORE is not None:
        return _STORE
    home = os.environ.get("TUDOU_HOME") or os.path.expanduser("~/.tudou_claw")
    db_path = os.path.join(home, "task_assignments.db")
    _STORE = TaskAssignmentStore(db_path)
    return _STORE
