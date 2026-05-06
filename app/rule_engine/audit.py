"""Append-only audit log for rule engine decisions.

One JSON object per line in <data_dir>/rules/audit.jsonl. Engine writes
on every evaluate() call (one entry per matched rule); UI reads back
for the Audit page (filterable by trigger/rule/agent/decision).

Bounded growth: rotated when file exceeds AUDIT_MAX_BYTES; kept N
historical files. Tail-only API for the UI keeps reads cheap.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tudou.rule_engine.audit")

AUDIT_MAX_BYTES = 10 * 1024 * 1024     # rotate at 10 MB
AUDIT_KEEP_FILES = 5                    # keep audit.jsonl + .1 .. .4


class AuditLog:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._dir = self.data_dir / "rules"
        self._path = self._dir / "audit.jsonl"
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        """Append a single decision to the log. Errors are swallowed —
        a broken audit log must not break a request."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                # Rotate if over the cap
                try:
                    if self._path.exists() and self._path.stat().st_size > AUDIT_MAX_BYTES:
                        self._rotate()
                except Exception:
                    pass
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("audit write failed: %s", e)

    def tail(self, n: int = 200,
             filters: Optional[dict] = None) -> list[dict]:
        """Read the last ``n`` entries, oldest-first. ``filters`` accepts
        any subset of {trigger, rule_id, agent_id, decision} for cheap
        client-side filtering."""
        if not self._path.is_file():
            return []
        f_trigger = (filters or {}).get("trigger")
        f_rule = (filters or {}).get("rule_id")
        f_agent = (filters or {}).get("agent_id")
        f_dec = (filters or {}).get("decision")

        # Read efficiently: use deque(maxlen=n) so memory stays bounded.
        buf: deque[dict] = deque(maxlen=n)
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if f_trigger and entry.get("trigger") != f_trigger:
                        continue
                    if f_rule and entry.get("rule_id") != f_rule:
                        continue
                    if f_agent and (entry.get("agent") or {}).get("id") != f_agent:
                        continue
                    if f_dec and entry.get("decision") != f_dec:
                        continue
                    buf.append(entry)
        except Exception as e:
            logger.debug("audit tail failed: %s", e)
            return []
        return list(buf)

    def _rotate(self) -> None:
        """Rotate audit.jsonl → audit.jsonl.1, push older numbers up."""
        for i in range(AUDIT_KEEP_FILES - 1, 0, -1):
            src = self._dir / f"audit.jsonl.{i}"
            dst = self._dir / f"audit.jsonl.{i + 1}"
            if src.exists():
                try:
                    os.replace(src, dst)
                except Exception:
                    pass
        try:
            os.replace(self._path, self._dir / "audit.jsonl.1")
        except Exception:
            pass


_LOG: AuditLog | None = None
_LOG_LOCK = threading.Lock()


def init_audit(data_dir: str | Path) -> AuditLog:
    global _LOG
    with _LOG_LOCK:
        if _LOG is None:
            _LOG = AuditLog(data_dir)
    return _LOG


def get_audit() -> AuditLog | None:
    return _LOG
