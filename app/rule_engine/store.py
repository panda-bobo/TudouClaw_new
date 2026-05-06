"""File-backed PolicyStore — persistent rule list with versioned revisions.

Layout under <data_dir>/rules/:
    rules.json              — current rule set (full snapshot)
    revisions.jsonl         — append-only history (one JSON per line:
                              {ts, by, action: "add|update|delete|enable",
                               rule_id, rule_before?, rule_after?})

In-memory index: scope_kind → trigger → list[Rule] (priority desc, then
created_at asc). Rebuilt on every load() / save().

Thread-safe: all mutations under a single RLock.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .types import Rule, TRIGGERS

logger = logging.getLogger("tudou.rule_engine.store")


class PolicyStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._dir = self.data_dir / "rules"
        self._snapshot_path = self._dir / "rules.json"
        self._revisions_path = self._dir / "revisions.jsonl"
        self._lock = threading.RLock()
        self._rules: dict[str, Rule] = {}     # id → Rule
        # Index: (scope_kind, trigger) → sorted list of Rule
        # Rebuilt eagerly after every mutation; reads stay lock-free.
        self._index: dict[tuple[str, str], list[Rule]] = {}
        self._load()

    # ── public API ─────────────────────────────────────────────────

    def add(self, rule: Rule, by: str = "") -> Rule:
        with self._lock:
            self._rules[rule.id] = rule
            self._reindex()
            self._save_snapshot()
            self._append_revision({
                "ts": time.time(),
                "by": by or rule.created_by,
                "action": "add",
                "rule_id": rule.id,
                "rule_after": rule.to_dict(),
            })
        return rule

    def update(self, rule_id: str, patch: dict, by: str = "",
               revision_note: str = "") -> Optional[Rule]:
        with self._lock:
            existing = self._rules.get(rule_id)
            if not existing:
                return None
            before = existing.to_dict()
            merged = {**before, **(patch or {})}
            merged["id"] = existing.id
            merged["created_at"] = existing.created_at
            merged["created_by"] = existing.created_by
            merged["revision"] = (existing.revision or 1) + 1
            merged["revision_note"] = revision_note or merged.get("revision_note", "")
            new_rule = Rule.from_dict(merged)
            self._rules[rule_id] = new_rule
            self._reindex()
            self._save_snapshot()
            self._append_revision({
                "ts": time.time(),
                "by": by,
                "action": "update",
                "rule_id": rule_id,
                "rule_before": before,
                "rule_after": new_rule.to_dict(),
                "revision_note": revision_note,
            })
            return new_rule

    def delete(self, rule_id: str, by: str = "") -> bool:
        with self._lock:
            existing = self._rules.pop(rule_id, None)
            if not existing:
                return False
            self._reindex()
            self._save_snapshot()
            self._append_revision({
                "ts": time.time(),
                "by": by,
                "action": "delete",
                "rule_id": rule_id,
                "rule_before": existing.to_dict(),
            })
            return True

    def set_enabled(self, rule_id: str, enabled: bool, by: str = "") -> bool:
        return self.update(rule_id, {"enabled": bool(enabled)}, by=by,
                           revision_note=f"toggle → {enabled}") is not None

    def get(self, rule_id: str) -> Optional[Rule]:
        return self._rules.get(rule_id)

    def all(self) -> list[Rule]:
        with self._lock:
            return list(self._rules.values())

    def for_trigger(self, trigger: str, ctx_scope: dict) -> list[Rule]:
        """Return enabled rules that apply to this trigger and the
        request context scope, ordered by priority (desc) then created_at.

        ``ctx_scope`` keys: kind ("global"|"project"|"meeting"|"solo"),
        plus the matching id (project_id / meeting_id / agent_id).
        """
        # Two index lookups: rules scoped specifically to ctx_scope.kind,
        # plus globally-scoped rules. When the request context IS global,
        # the specific lookup IS the global one — don't dedup wrongly by
        # concatenating; one lookup covers it.
        kind = ctx_scope.get("kind") or "global"
        with self._lock:
            if kind == "global":
                candidates = list(self._index.get(("global", trigger), []))
            else:
                specific = self._index.get((kind, trigger), [])
                globals_ = self._index.get(("global", trigger), [])
                candidates = list(specific) + list(globals_)
        out: list[Rule] = []
        for r in candidates:
            if not r.enabled:
                continue
            if not r.scope.matches(ctx_scope):
                continue
            out.append(r)
        # Re-sort the merged list by priority desc, then created_at asc.
        out.sort(key=lambda r: (-r.priority, r.created_at))
        return out

    # ── persistence + indexing ─────────────────────────────────────

    def _load(self) -> None:
        if not self._snapshot_path.is_file():
            self._reindex()
            return
        try:
            data = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("rules.json unreadable (%s); starting empty", e)
            self._reindex()
            return
        for d in (data.get("rules") or []):
            try:
                rule = Rule.from_dict(d)
                self._rules[rule.id] = rule
            except Exception as e:
                logger.warning("dropped malformed rule: %s", e)
        self._reindex()

    def _save_snapshot(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._snapshot_path.with_suffix(".json.tmp")
        payload = {
            "schema_version": 1,
            "saved_at": time.time(),
            "rules": [r.to_dict() for r in self._rules.values()],
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._snapshot_path)

    def _append_revision(self, entry: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._revisions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("failed to append revision: %s", e)

    def _reindex(self) -> None:
        idx: dict[tuple[str, str], list[Rule]] = {}
        for rule in self._rules.values():
            if rule.trigger not in TRIGGERS:
                continue
            key = (rule.scope.kind, rule.trigger)
            idx.setdefault(key, []).append(rule)
        # Sort each bucket by priority desc, then created_at asc for stability
        for k in idx:
            idx[k].sort(key=lambda r: (-r.priority, r.created_at))
        self._index = idx


# ── Module singleton ───────────────────────────────────────────────

_STORE: PolicyStore | None = None
_STORE_LOCK = threading.Lock()


def init_store(data_dir: str | Path) -> PolicyStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = PolicyStore(data_dir)
    return _STORE


def get_store() -> PolicyStore | None:
    return _STORE
