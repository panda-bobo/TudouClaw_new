"""
SystemSettingsStore — admin-editable runtime configuration.

JSON-backed (one file under <data_dir>/system_settings.json), thread-
safe, with dotted-path access and deep-merge updates. Modeled on
``app/branding.py`` pattern.

DEFAULTS is the source of truth for keys + fallback values.
``get()`` walks the persisted dict first; absent keys (at any depth)
return either the matching default or the caller-supplied override.

This module does NOT validate values at write time — that's the API
layer's job. Store-level operations are unconditional.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.system_settings")


# Source of truth for defaults. Any new system-level knob lives here.
DEFAULTS: dict[str, Any] = {
    "canvas": {
        # Per-run cap on concurrent canvas nodes. ThreadPoolExecutor
        # size when _drive_loop spawns ready nodes in parallel.
        "max_parallel_nodes": 6,
    },
    "delegate": {
        # Per-call cap on concurrent children spawned by
        # Agent.delegate_parallel.
        "max_parallel_children": 6,
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict = base recursively merged with patch."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SystemSettingsStore:
    """Read-mostly JSON file with single-write lock."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "system_settings.json"
        self._lock = threading.Lock()
        self._cache: dict | None = None

    def _load_unlocked(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception as e:
            logger.warning("system_settings.json read failed: %s — using defaults", e)
            d = {}
        self._cache = d
        return d

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path lookup. Falls back to DEFAULTS at the same path,
        then to the caller-supplied ``default``."""
        if not path:
            raise ValueError("empty path")
        with self._lock:
            current = self._load_unlocked()
            walk_persisted = current
            walk_defaults = DEFAULTS
            for part in path.split("."):
                if isinstance(walk_persisted, dict) and part in walk_persisted:
                    walk_persisted = walk_persisted[part]
                else:
                    walk_persisted = _MISSING
                if isinstance(walk_defaults, dict) and part in walk_defaults:
                    walk_defaults = walk_defaults[part]
                else:
                    walk_defaults = _MISSING
            if walk_persisted is not _MISSING:
                return walk_persisted
            if walk_defaults is not _MISSING:
                return walk_defaults
            return default

    def set(self, path: str, value: Any) -> dict:
        """Dotted-path write. Atomic file replace. Returns full state."""
        if not path:
            raise ValueError("empty path")
        with self._lock:
            current = dict(self._load_unlocked())
            parts = path.split(".")
            cursor = current
            for part in parts[:-1]:
                if not isinstance(cursor.get(part), dict):
                    cursor[part] = {}
                cursor = cursor[part]
            cursor[parts[-1]] = value
            self._write_unlocked(current)
            self._cache = current
            return dict(current)

    def update(self, patch: dict) -> dict:
        """Deep-merge patch into current state. Atomic write."""
        if not isinstance(patch, dict):
            raise ValueError("patch must be a dict")
        with self._lock:
            current = self._load_unlocked()
            merged = _deep_merge(current, patch)
            self._write_unlocked(merged)
            self._cache = merged
            return dict(merged)

    def all(self) -> dict:
        """Snapshot — defaults filled in for unset keys, persisted
        values overlaid on top. Useful for the Settings UI."""
        with self._lock:
            persisted = self._load_unlocked()
            return _deep_merge(DEFAULTS, persisted)

    def _write_unlocked(self, data: dict) -> None:
        """Atomic tmp+replace. Caller holds self._lock."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._path)


# Sentinel for missing keys during dotted-path walk
_MISSING = object()


# ── Module-level singleton ──────────────────────────────────────────────

_STORE: SystemSettingsStore | None = None
_STORE_LOCK = threading.Lock()


def init_store(data_dir: str | Path) -> SystemSettingsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SystemSettingsStore(data_dir)
    return _STORE


def get_store() -> SystemSettingsStore | None:
    return _STORE
