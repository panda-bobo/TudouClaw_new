"""SharedContextStore — SQLite-backed multi-agent shared state.

Sits on top of the existing ``tudou_claw.db`` (via ``app.infra.database``)
so we share the connection pool and WAL journaling. Tables are
namespaced ``sc_*`` to avoid colliding with the main schema.

Design tenets (from the architecture review):
  * Schema-first, not free-form blobs — every column is typed.
  * Query, don't dump — callers filter by ``project_id``, status, since_ts.
  * Append-only event semantics for ``sc_handoffs`` / ``sc_pending_qs``;
    ``sc_artifacts`` and ``sc_decisions`` are versioned via
    ``supersedes_id`` so old rows survive for audit.
  * ``project_summary()`` returns *counts + recent activity*, not row dumps —
    it's the prompt-injection format (~300-500 tokens regardless of project size).

Best-effort everywhere: callers (agent tools, prompt builder) treat
failures as "no shared context available" rather than letting them break
the task pipeline.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

from ..infra.database import get_database

logger = logging.getLogger("tudou.shared_context")


# ─── valid enum-like values ────────────────────────────────────────────
ARTIFACT_KINDS = ("document", "code", "data", "image", "report", "config", "other")
ARTIFACT_STATUS = ("active", "archived", "superseded")
DECISION_STATUS = ("final", "proposed", "overridden")
MILESTONE_STATUS = ("pending", "in_progress", "done", "blocked")
HANDOFF_STATUS = ("pending", "acknowledged", "resolved", "expired")
PENDING_Q_STATUS = ("open", "answered", "abandoned")


def _new_id(prefix: str) -> str:
    """Short-id factory: ``art_a1b2c3d4``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _row(r) -> dict:
    """SQLite row → dict (handles both ``sqlite3.Row`` and ``None``)."""
    return dict(r) if r is not None else {}


# ─── singleton accessor ────────────────────────────────────────────────
_store: Optional["SharedContextStore"] = None
_lock = threading.Lock()


def get_shared_context_store() -> "SharedContextStore":
    """Process-wide singleton."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = SharedContextStore()
    return _store


# ─── store ─────────────────────────────────────────────────────────────
class SharedContextStore:
    """All operations are thread-safe (the underlying ``TudouDatabase``
    serialises writes via its RLock; reads are concurrent under WAL)."""

    def __init__(self, db=None):
        self.db = db or get_database()
        self._ensure_tables()
        self._rlock = threading.RLock()

    # ── schema ────────────────────────────────────────────────────────
    def _ensure_tables(self) -> None:
        """Idempotent ``CREATE TABLE IF NOT EXISTS``. Safe to call repeatedly."""
        ddl = [
            # ── Artifacts (file refs with summary) ──────────────────────
            """CREATE TABLE IF NOT EXISTS sc_artifacts (
                id            TEXT PRIMARY KEY,
                project_id    TEXT NOT NULL,
                agent_id      TEXT NOT NULL,
                path          TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT 'document',
                title         TEXT NOT NULL,
                summary       TEXT NOT NULL DEFAULT '',
                token_count   INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'active',
                version       INTEGER NOT NULL DEFAULT 1,
                supersedes_id TEXT,
                vector_id     TEXT NOT NULL DEFAULT '',
                produced_at   REAL NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sc_art_proj ON sc_artifacts(project_id, status, produced_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sc_art_agent ON sc_artifacts(agent_id, project_id)",
            "CREATE INDEX IF NOT EXISTS idx_sc_art_kind ON sc_artifacts(project_id, kind, status)",

            # ── Decisions (structured log) ──────────────────────────────
            """CREATE TABLE IF NOT EXISTS sc_decisions (
                id            TEXT PRIMARY KEY,
                project_id    TEXT NOT NULL,
                topic         TEXT NOT NULL,
                decision      TEXT NOT NULL,
                rationale     TEXT NOT NULL DEFAULT '',
                decided_by    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'final',
                supersedes_id TEXT,
                decided_at    REAL NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sc_dec_proj ON sc_decisions(project_id, decided_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sc_dec_topic ON sc_decisions(project_id, topic, status)",

            # ── Milestones ──────────────────────────────────────────────
            """CREATE TABLE IF NOT EXISTS sc_milestones (
                id            TEXT PRIMARY KEY,
                project_id    TEXT NOT NULL,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'pending',
                owner_agent   TEXT NOT NULL DEFAULT '',
                due_at        REAL,
                completed_at  REAL,
                blocked_by    TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sc_ms_proj ON sc_milestones(project_id, status)",

            # ── Handoffs (pull, not push) ───────────────────────────────
            """CREATE TABLE IF NOT EXISTS sc_handoffs (
                id            TEXT PRIMARY KEY,
                project_id    TEXT NOT NULL,
                src_agent     TEXT NOT NULL,
                dst_agent     TEXT NOT NULL,
                intent        TEXT NOT NULL,
                summary       TEXT NOT NULL DEFAULT '',
                artifact_refs TEXT NOT NULL DEFAULT '[]',
                status        TEXT NOT NULL DEFAULT 'pending',
                ts            REAL NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sc_ho_dst ON sc_handoffs(dst_agent, status, ts DESC)",
            "CREATE INDEX IF NOT EXISTS idx_sc_ho_proj ON sc_handoffs(project_id, ts DESC)",

            # ── Pending Q&A between agents ─────────────────────────────
            """CREATE TABLE IF NOT EXISTS sc_pending_qs (
                id            TEXT PRIMARY KEY,
                project_id    TEXT NOT NULL,
                asked_by      TEXT NOT NULL,
                asked_to      TEXT NOT NULL,
                question      TEXT NOT NULL,
                answer        TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'open',
                asked_at      REAL NOT NULL,
                answered_at   REAL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sc_pq_to ON sc_pending_qs(asked_to, status, asked_at)",
            "CREATE INDEX IF NOT EXISTS idx_sc_pq_proj ON sc_pending_qs(project_id, status, asked_at DESC)",

            # ── Per-agent cursor for incremental sync ──────────────────
            """CREATE TABLE IF NOT EXISTS sc_agent_view (
                agent_id      TEXT NOT NULL,
                project_id    TEXT NOT NULL,
                table_name    TEXT NOT NULL,
                last_seen_ts  REAL NOT NULL,
                PRIMARY KEY (agent_id, project_id, table_name)
            )""",
        ]
        with self.db._tx() as conn:
            for stmt in ddl:
                conn.execute(stmt)
        logger.info("SharedContextStore tables ready (sc_*)")

    # ════════════════════════════════════════════════════════════════════
    # Artifacts
    # ════════════════════════════════════════════════════════════════════
    def register_artifact(
        self, *,
        project_id: str,
        agent_id: str,
        path: str,
        title: str,
        kind: str = "document",
        summary: str = "",
        token_count: int = 0,
        vector_id: str = "",
        supersedes_id: str = "",
    ) -> str:
        """Record an artifact reference. Returns the new ``art_*`` id.

        ``summary`` is truncated to 200 chars (we keep the *card*, not the
        content). If ``supersedes_id`` is given, the old artifact is
        marked ``superseded`` in the same transaction.
        """
        if kind not in ARTIFACT_KINDS:
            kind = "other"
        summary = (summary or "")[:200]
        title = (title or path)[:120]
        aid = _new_id("art")
        now = time.time()
        version = 1
        with self._rlock, self.db._tx() as conn:
            if supersedes_id:
                # Look up old version + mark superseded
                row = conn.execute(
                    "SELECT version FROM sc_artifacts WHERE id = ?",
                    (supersedes_id,),
                ).fetchone()
                if row:
                    version = int(row["version"]) + 1
                    conn.execute(
                        "UPDATE sc_artifacts SET status = 'superseded' WHERE id = ?",
                        (supersedes_id,),
                    )
            conn.execute(
                """INSERT INTO sc_artifacts
                   (id, project_id, agent_id, path, kind, title, summary,
                    token_count, status, version, supersedes_id, vector_id,
                    produced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (aid, project_id, agent_id, path, kind, title, summary,
                 int(token_count or 0), "active", version,
                 supersedes_id or None, vector_id, now),
            )
        return aid

    def get_artifact(self, artifact_id: str) -> Optional[dict]:
        row = self.db._conn.execute(
            "SELECT * FROM sc_artifacts WHERE id = ?", (artifact_id,),
        ).fetchone()
        return _row(row) if row else None

    def list_artifacts(
        self, *,
        project_id: str,
        agent_id: str = "",
        kind: str = "",
        status: str = "active",
        since_ts: float = 0,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM sc_artifacts WHERE project_id = ?"
        args: list = [project_id]
        if agent_id:
            sql += " AND agent_id = ?"; args.append(agent_id)
        if kind:
            sql += " AND kind = ?"; args.append(kind)
        if status:
            sql += " AND status = ?"; args.append(status)
        if since_ts > 0:
            sql += " AND produced_at >= ?"; args.append(since_ts)
        sql += " ORDER BY produced_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db._conn.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]

    def archive_artifact(self, artifact_id: str) -> bool:
        with self._rlock, self.db._tx() as conn:
            cur = conn.execute(
                "UPDATE sc_artifacts SET status = 'archived' WHERE id = ?",
                (artifact_id,),
            )
            return cur.rowcount > 0

    # ════════════════════════════════════════════════════════════════════
    # Decisions
    # ════════════════════════════════════════════════════════════════════
    def record_decision(
        self, *,
        project_id: str,
        topic: str,
        decision: str,
        decided_by: str,
        rationale: str = "",
        supersedes_id: str = "",
    ) -> str:
        """Append a decision. If ``supersedes_id`` is given, the old
        decision is marked ``overridden`` in the same transaction."""
        did = _new_id("dec")
        now = time.time()
        with self._rlock, self.db._tx() as conn:
            if supersedes_id:
                conn.execute(
                    "UPDATE sc_decisions SET status = 'overridden' WHERE id = ?",
                    (supersedes_id,),
                )
            conn.execute(
                """INSERT INTO sc_decisions
                   (id, project_id, topic, decision, rationale, decided_by,
                    status, supersedes_id, decided_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (did, project_id, topic[:120], decision[:500],
                 (rationale or "")[:1000], decided_by,
                 "final", supersedes_id or None, now),
            )
        return did

    def list_decisions(
        self, *,
        project_id: str,
        status: str = "final",
        since_ts: float = 0,
        limit: int = 20,
    ) -> list[dict]:
        sql = "SELECT * FROM sc_decisions WHERE project_id = ?"
        args: list = [project_id]
        if status:
            sql += " AND status = ?"; args.append(status)
        if since_ts > 0:
            sql += " AND decided_at >= ?"; args.append(since_ts)
        sql += " ORDER BY decided_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db._conn.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]

    # ════════════════════════════════════════════════════════════════════
    # Milestones
    # ════════════════════════════════════════════════════════════════════
    def create_milestone(
        self, *,
        project_id: str,
        name: str,
        description: str = "",
        owner_agent: str = "",
        due_at: Optional[float] = None,
    ) -> str:
        mid = _new_id("ms")
        now = time.time()
        with self._rlock, self.db._tx() as conn:
            conn.execute(
                """INSERT INTO sc_milestones
                   (id, project_id, name, description, status, owner_agent,
                    due_at, completed_at, blocked_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, project_id, name[:120], (description or "")[:500],
                 "pending", owner_agent, due_at, None, "", now, now),
            )
        return mid

    def update_milestone(
        self, milestone_id: str, *,
        status: str = "",
        owner_agent: Optional[str] = None,
        blocked_by: Optional[str] = None,
    ) -> bool:
        sets: list[str] = ["updated_at = ?"]
        args: list = [time.time()]
        if status:
            if status not in MILESTONE_STATUS:
                return False
            sets.append("status = ?"); args.append(status)
            if status == "done":
                sets.append("completed_at = ?"); args.append(time.time())
        if owner_agent is not None:
            sets.append("owner_agent = ?"); args.append(owner_agent)
        if blocked_by is not None:
            sets.append("blocked_by = ?"); args.append(blocked_by)
        args.append(milestone_id)
        with self._rlock, self.db._tx() as conn:
            cur = conn.execute(
                f"UPDATE sc_milestones SET {', '.join(sets)} WHERE id = ?",
                tuple(args),
            )
            return cur.rowcount > 0

    def list_milestones(
        self, *,
        project_id: str,
        status: str = "",
        limit: int = 20,
    ) -> list[dict]:
        sql = "SELECT * FROM sc_milestones WHERE project_id = ?"
        args: list = [project_id]
        if status:
            sql += " AND status = ?"; args.append(status)
        sql += " ORDER BY (status='blocked') DESC, (status='in_progress') DESC, updated_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db._conn.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]

    # ════════════════════════════════════════════════════════════════════
    # Handoffs (pull, not push)
    # ════════════════════════════════════════════════════════════════════
    def write_handoff(
        self, *,
        project_id: str,
        src_agent: str,
        dst_agent: str,
        intent: str,
        summary: str = "",
        artifact_refs: Optional[list[str]] = None,
    ) -> str:
        hid = _new_id("ho")
        now = time.time()
        refs_json = json.dumps(artifact_refs or [])
        with self._rlock, self.db._tx() as conn:
            conn.execute(
                """INSERT INTO sc_handoffs
                   (id, project_id, src_agent, dst_agent, intent, summary,
                    artifact_refs, status, ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (hid, project_id, src_agent, dst_agent, intent[:500],
                 (summary or "")[:300], refs_json, "pending", now),
            )
        return hid

    def list_handoffs(
        self, *,
        project_id: str = "",
        dst_agent: str = "",
        src_agent: str = "",
        status: str = "pending",
        since_ts: float = 0,
        limit: int = 20,
    ) -> list[dict]:
        clauses: list[str] = []
        args: list = []
        if project_id:
            clauses.append("project_id = ?"); args.append(project_id)
        if dst_agent:
            clauses.append("dst_agent = ?"); args.append(dst_agent)
        if src_agent:
            clauses.append("src_agent = ?"); args.append(src_agent)
        if status:
            clauses.append("status = ?"); args.append(status)
        if since_ts > 0:
            clauses.append("ts >= ?"); args.append(since_ts)
        sql = "SELECT * FROM sc_handoffs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db._conn.execute(sql, tuple(args)).fetchall()
        out = []
        for r in rows:
            d = _row(r)
            try:
                d["artifact_refs"] = json.loads(d.get("artifact_refs") or "[]")
            except Exception:
                d["artifact_refs"] = []
            out.append(d)
        return out

    def acknowledge_handoff(self, handoff_id: str, status: str = "acknowledged") -> bool:
        if status not in HANDOFF_STATUS:
            return False
        with self._rlock, self.db._tx() as conn:
            cur = conn.execute(
                "UPDATE sc_handoffs SET status = ? WHERE id = ?",
                (status, handoff_id),
            )
            return cur.rowcount > 0

    # ════════════════════════════════════════════════════════════════════
    # Pending Q&A
    # ════════════════════════════════════════════════════════════════════
    def ask_question(
        self, *,
        project_id: str,
        asked_by: str,
        asked_to: str,
        question: str,
    ) -> str:
        qid = _new_id("q")
        now = time.time()
        with self._rlock, self.db._tx() as conn:
            conn.execute(
                """INSERT INTO sc_pending_qs
                   (id, project_id, asked_by, asked_to, question, answer,
                    status, asked_at, answered_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (qid, project_id, asked_by, asked_to, question[:1000],
                 "", "open", now, None),
            )
        return qid

    def answer_question(self, question_id: str, answer: str) -> bool:
        with self._rlock, self.db._tx() as conn:
            cur = conn.execute(
                """UPDATE sc_pending_qs
                   SET answer = ?, status = 'answered', answered_at = ?
                   WHERE id = ? AND status = 'open'""",
                ((answer or "")[:2000], time.time(), question_id),
            )
            return cur.rowcount > 0

    def list_pending_questions(
        self, *,
        asked_to: str = "",
        project_id: str = "",
        status: str = "open",
        limit: int = 20,
    ) -> list[dict]:
        clauses: list[str] = []
        args: list = []
        if asked_to:
            clauses.append("asked_to = ?"); args.append(asked_to)
        if project_id:
            clauses.append("project_id = ?"); args.append(project_id)
        if status:
            clauses.append("status = ?"); args.append(status)
        sql = "SELECT * FROM sc_pending_qs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY asked_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db._conn.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]

    # ════════════════════════════════════════════════════════════════════
    # Project state summary (the prompt-injection format)
    # ════════════════════════════════════════════════════════════════════
    def project_summary(self, project_id: str) -> dict:
        """Compact project state for prompt injection (~300-500 token).

        Returns counts + 3 most recent items per category. Callers
        format this into a system prompt block; the underlying details
        are pulled on demand via the list_* / get_* APIs.
        """
        if not project_id:
            return {}
        c = self.db._conn
        # Counts
        ms_counts = {row["status"]: row["n"] for row in c.execute(
            "SELECT status, COUNT(*) AS n FROM sc_milestones "
            "WHERE project_id = ? GROUP BY status", (project_id,),
        ).fetchall()}
        art_counts = {row["status"]: row["n"] for row in c.execute(
            "SELECT status, COUNT(*) AS n FROM sc_artifacts "
            "WHERE project_id = ? GROUP BY status", (project_id,),
        ).fetchall()}
        dec_count = c.execute(
            "SELECT COUNT(*) AS n FROM sc_decisions "
            "WHERE project_id = ? AND status = 'final'", (project_id,),
        ).fetchone()["n"]
        ho_count = c.execute(
            "SELECT COUNT(*) AS n FROM sc_handoffs "
            "WHERE project_id = ? AND status = 'pending'", (project_id,),
        ).fetchone()["n"]
        pq_count = c.execute(
            "SELECT COUNT(*) AS n FROM sc_pending_qs "
            "WHERE project_id = ? AND status = 'open'", (project_id,),
        ).fetchone()["n"]

        # Recent activity (3 most recent of each)
        recent_artifacts = self.list_artifacts(project_id=project_id, limit=3)
        recent_decisions = self.list_decisions(project_id=project_id, limit=3)
        recent_milestones = self.list_milestones(project_id=project_id, limit=5)

        return {
            "project_id": project_id,
            "counts": {
                "artifacts": dict(art_counts),
                "milestones": dict(ms_counts),
                "decisions_final": dec_count,
                "handoffs_pending": ho_count,
                "pending_questions": pq_count,
            },
            "recent": {
                "artifacts": [
                    {"id": a["id"], "title": a["title"], "kind": a["kind"],
                     "agent": a["agent_id"], "summary": a["summary"]}
                    for a in recent_artifacts
                ],
                "decisions": [
                    {"id": d["id"], "topic": d["topic"], "decision": d["decision"],
                     "by": d["decided_by"]}
                    for d in recent_decisions
                ],
                "milestones": [
                    {"id": m["id"], "name": m["name"], "status": m["status"],
                     "owner": m["owner_agent"]}
                    for m in recent_milestones
                ],
            },
            "ts": time.time(),
        }

    def project_summary_markdown(self, project_id: str, *, max_chars: int = 2400) -> str:
        """Same as ``project_summary`` but pre-formatted as markdown for
        direct prompt-block injection. Caps total length so the block is
        bounded regardless of project size."""
        s = self.project_summary(project_id)
        if not s or not s.get("counts"):
            return ""
        c = s["counts"]
        ms = c["milestones"]
        art = c["artifacts"]
        lines = ["[项目共享状态]"]

        ms_parts = []
        for st, label in (("done", "完成"), ("in_progress", "进行中"),
                          ("pending", "待办"), ("blocked", "阻塞")):
            n = ms.get(st, 0)
            if n:
                ms_parts.append(f"{label} {n}")
        if ms_parts:
            lines.append(f"- Milestones: {' · '.join(ms_parts)}")

        art_active = art.get("active", 0)
        if art_active:
            lines.append(f"- Artifacts: {art_active} 个产出物")
        if c.get("decisions_final"):
            lines.append(f"- Decisions: {c['decisions_final']} 条已确认决策")
        if c.get("handoffs_pending"):
            lines.append(f"- Pending handoffs: {c['handoffs_pending']}")
        if c.get("pending_questions"):
            lines.append(f"- Open questions: {c['pending_questions']}")

        # Recent activity (compact)
        recent_arts = s["recent"]["artifacts"]
        if recent_arts:
            lines.append("")
            lines.append("[最近 artifacts]")
            for a in recent_arts:
                lines.append(
                    f"- `{a['id']}` ({a['kind']}) by {a['agent']}: "
                    f"{(a['title'])[:40]} — {a['summary'][:60]}"
                )

        recent_decs = s["recent"]["decisions"]
        if recent_decs:
            lines.append("")
            lines.append("[最近决策]")
            for d in recent_decs:
                lines.append(f"- {d['topic']} → **{d['decision']}** (by {d['by']})")

        active_ms = [m for m in s["recent"]["milestones"]
                     if m["status"] in ("in_progress", "blocked")]
        if active_ms:
            lines.append("")
            lines.append("[活跃 milestones]")
            for m in active_ms:
                lines.append(f"- [{m['status']}] {m['name']} (owner: {m['owner'] or '-'})")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars - 20] + "\n…(truncated)"
        return text

    # ════════════════════════════════════════════════════════════════════
    # Per-agent cursor (incremental sync)
    # ════════════════════════════════════════════════════════════════════
    def get_cursor(self, agent_id: str, project_id: str, table_name: str) -> float:
        row = self.db._conn.execute(
            "SELECT last_seen_ts FROM sc_agent_view "
            "WHERE agent_id = ? AND project_id = ? AND table_name = ?",
            (agent_id, project_id, table_name),
        ).fetchone()
        return float(row["last_seen_ts"]) if row else 0.0

    def set_cursor(self, agent_id: str, project_id: str, table_name: str,
                   ts: float) -> None:
        with self._rlock, self.db._tx() as conn:
            conn.execute(
                """INSERT INTO sc_agent_view (agent_id, project_id, table_name, last_seen_ts)
                   VALUES (?,?,?,?)
                   ON CONFLICT(agent_id, project_id, table_name)
                   DO UPDATE SET last_seen_ts = excluded.last_seen_ts""",
                (agent_id, project_id, table_name, float(ts)),
            )
