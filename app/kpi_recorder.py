"""KPIRecorder — 把 RolePresetV2 的 KPI 事件写到 SQLite。

表结构：role_kpi_events
  id           INTEGER PK
  ts           REAL      写入时间戳
  role         TEXT      角色 preset id（如 meeting_assistant）
  agent_id     TEXT      实例 agent id
  key          TEXT      KPI 名称（如 first_pass_rate）
  value        REAL      数值
  meta_json    TEXT      附加信息 JSON

查询辅助：rollup(role, key) 返回最近窗口的均值/总数，便于仪表盘展示。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger("tudou.kpi_recorder")

_DB_DIR = os.path.join(os.path.expanduser("~"), ".tudou_claw")
_DB_PATH = os.path.join(_DB_DIR, "tudou.db")


class KPIRecorder:
    """Thread-safe SQLite writer for KPI events."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or _DB_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)

    def _init_schema(self) -> None:
        with self._lock, self._conn() as cx:
            cx.execute("""
                CREATE TABLE IF NOT EXISTS role_kpi_events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        REAL    NOT NULL,
                    role      TEXT    NOT NULL,
                    agent_id  TEXT    NOT NULL,
                    key       TEXT    NOT NULL,
                    value     REAL    NOT NULL,
                    meta_json TEXT
                )
            """)
            cx.execute("CREATE INDEX IF NOT EXISTS idx_kpi_role_key ON role_kpi_events(role, key)")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_kpi_ts ON role_kpi_events(ts)")

    def record(
        self,
        *,
        role: str,
        agent_id: str,
        key: str,
        value: float,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not role or not key:
            return
        try:
            with self._lock, self._conn() as cx:
                cx.execute(
                    "INSERT INTO role_kpi_events (ts, role, agent_id, key, value, meta_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), role, agent_id or "", key, float(value),
                     json.dumps(meta or {}, ensure_ascii=False)),
                )
        except Exception as e:
            logger.warning("KPIRecorder.record failed: %s", e)

    def rollup(self, role: str, key: str, window_s: float = 7 * 86400) -> dict[str, Any]:
        """返回最近 window_s 秒内的 (count, avg, max, min)."""
        since = time.time() - window_s
        try:
            with self._lock, self._conn() as cx:
                row = cx.execute(
                    "SELECT COUNT(*), AVG(value), MAX(value), MIN(value) "
                    "FROM role_kpi_events WHERE role=? AND key=? AND ts>=?",
                    (role, key, since),
                ).fetchone()
                return {
                    "count": int(row[0] or 0),
                    "avg": float(row[1]) if row[1] is not None else None,
                    "max": float(row[2]) if row[2] is not None else None,
                    "min": float(row[3]) if row[3] is not None else None,
                }
        except Exception as e:
            logger.warning("KPIRecorder.rollup failed: %s", e)
            return {"count": 0, "avg": None, "max": None, "min": None}

    def list_by_role(self, role: str, limit: int = 100) -> list[dict]:
        try:
            with self._lock, self._conn() as cx:
                rows = cx.execute(
                    "SELECT ts, role, agent_id, key, value, meta_json "
                    "FROM role_kpi_events WHERE role=? ORDER BY ts DESC LIMIT ?",
                    (role, int(limit)),
                ).fetchall()
                out = []
                for r in rows:
                    try:
                        meta = json.loads(r[5]) if r[5] else {}
                    except Exception:
                        meta = {}
                    out.append({
                        "ts": r[0], "role": r[1], "agent_id": r[2],
                        "key": r[3], "value": r[4], "meta": meta,
                    })
                return out
        except Exception as e:
            logger.warning("KPIRecorder.list_by_role failed: %s", e)
            return []


_recorder: KPIRecorder | None = None


def get_kpi_recorder() -> KPIRecorder:
    global _recorder
    if _recorder is None:
        _recorder = KPIRecorder()
    return _recorder
