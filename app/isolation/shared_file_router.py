"""
app.isolation.shared_file_router — Concurrent-safe file operations for
shared directories (project workspace, meeting workspace).

When multiple agent workers write to the same shared directory, this
router provides:

  1. **Per-file thread-level locking** — prevents data races within the
     Hub process (gate handler runs in Hub).
  2. **Atomic writes via rename** — readers never see half-written files.
  3. **Advisory file locking (fcntl.flock)** — prevents races across
     processes (belt-and-suspenders with the thread lock).
  4. **Audit trail** — every write is logged with timestamp, agent_id,
     path, and byte count.

Usage from the gate handler::

    router = get_shared_file_router()
    router.write("/path/to/shared/report.md", content, agent_id="abc123")
    data = router.read("/path/to/shared/report.md")
    router.append("/path/to/shared/log.txt", line, agent_id="abc123")
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tudou.shared_file_router")

# Try fcntl for advisory file locking (Unix only)
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False


class SharedFileRouter:
    """Thread-safe, audited file operations for shared directories.

    All write operations acquire a per-path lock to prevent concurrent
    corruption. Reads are lock-free (POSIX atomic-read guarantee for
    reasonable file sizes).
    """

    def __init__(self, data_dir: str, *, max_audit: int = 5000):
        self._data_dir = data_dir
        self._max_audit = max_audit

        # Per-path write locks (LRU-bounded)
        self._locks: OrderedDict[str, threading.Lock] = OrderedDict()
        self._meta_lock = threading.Lock()
        self._max_locks = 2000

        # Audit trail: list of dicts
        self._audit: List[Dict[str, Any]] = []
        self._audit_lock = threading.Lock()

    # ── Lock management ──

    def _get_lock(self, path: str) -> threading.Lock:
        """Return a per-path lock (LRU eviction if too many)."""
        with self._meta_lock:
            if path in self._locks:
                self._locks.move_to_end(path)
                return self._locks[path]
            lock = threading.Lock()
            self._locks[path] = lock
            # Evict oldest if over limit
            while len(self._locks) > self._max_locks:
                self._locks.popitem(last=False)
            return lock

    # ── Write operations ──

    def write(self, path: str, content: str, *,
              agent_id: str = "", encoding: str = "utf-8") -> int:
        """Atomic write: tmp file → os.replace(). Returns bytes written.

        Acquires an exclusive per-path lock so concurrent writes from
        different agents to the same file are serialized.
        """
        resolved = Path(path).resolve()
        lock = self._get_lock(str(resolved))

        with lock:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_name(resolved.name + f".tmp_{os.getpid()}")
            try:
                data = content.encode(encoding)
                with open(tmp, "wb") as f:
                    if _HAS_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(str(tmp), str(resolved))
                self._record_audit("WRITE", str(resolved), agent_id, len(data))
                return len(data)
            except Exception:
                # Clean up tmp on failure
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

    def write_bytes(self, path: str, data: bytes, *,
                    agent_id: str = "") -> int:
        """Atomic binary write. Returns bytes written."""
        resolved = Path(path).resolve()
        lock = self._get_lock(str(resolved))

        with lock:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            tmp = resolved.with_name(resolved.name + f".tmp_{os.getpid()}")
            try:
                with open(tmp, "wb") as f:
                    if _HAS_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(str(tmp), str(resolved))
                self._record_audit("WRITE_BYTES", str(resolved),
                                   agent_id, len(data))
                return len(data)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

    def append(self, path: str, content: str, *,
               agent_id: str = "", encoding: str = "utf-8") -> int:
        """Append to file with exclusive lock. Returns bytes appended."""
        resolved = Path(path).resolve()
        lock = self._get_lock(str(resolved))

        with lock:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode(encoding)
            with open(resolved, "ab") as f:
                if _HAS_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(data)
                f.flush()
            self._record_audit("APPEND", str(resolved), agent_id, len(data))
            return len(data)

    def mkdir(self, path: str, *, agent_id: str = "") -> str:
        """Create directory (including parents). Returns resolved path."""
        resolved = Path(path).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self._record_audit("MKDIR", str(resolved), agent_id, 0)
        return str(resolved)

    def delete(self, path: str, *, agent_id: str = "") -> bool:
        """Delete a file. Returns True if deleted, False if not found."""
        resolved = Path(path).resolve()
        lock = self._get_lock(str(resolved))
        with lock:
            if resolved.exists():
                resolved.unlink()
                self._record_audit("DELETE", str(resolved), agent_id, 0)
                return True
            return False

    # ── Read operations (no exclusive lock) ──

    def read(self, path: str, *, encoding: str = "utf-8") -> str:
        """Read file contents. No lock — POSIX guarantees atomic reads
        for reasonable sizes, and writes use atomic replace."""
        resolved = Path(path).resolve()
        return resolved.read_text(encoding=encoding)

    def read_bytes(self, path: str) -> bytes:
        """Read binary file contents."""
        return Path(path).resolve().read_bytes()

    def exists(self, path: str) -> bool:
        return Path(path).resolve().exists()

    def list_dir(self, path: str, *, recursive: bool = False) -> List[Dict]:
        """List files in directory. Returns list of {name, path, size, mtime}."""
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            return []
        result = []
        entries = resolved.rglob("*") if recursive else resolved.iterdir()
        for entry in entries:
            if entry.is_file():
                try:
                    stat = entry.stat()
                    result.append({
                        "name": entry.name,
                        "path": str(entry),
                        "relative": str(entry.relative_to(resolved)),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
                except OSError:
                    pass
        return sorted(result, key=lambda x: x.get("mtime", 0), reverse=True)

    # ── Audit ──

    def _record_audit(self, op: str, path: str, agent_id: str, size: int):
        entry = {
            "ts": time.time(),
            "op": op,
            "path": path,
            "agent_id": agent_id,
            "size": size,
        }
        with self._audit_lock:
            self._audit.append(entry)
            if len(self._audit) > self._max_audit:
                self._audit = self._audit[-self._max_audit // 2:]
        logger.debug("[%s] %s %s (%d bytes) by %s",
                    op, path, "OK", size, agent_id[:8] if agent_id else "?")

    def get_audit(self, last_n: int = 100,
                  agent_id: str = "") -> List[Dict]:
        """Return recent audit entries, optionally filtered by agent."""
        with self._audit_lock:
            entries = self._audit[-last_n * 2:]  # pre-filter oversample
        if agent_id:
            entries = [e for e in entries if e.get("agent_id") == agent_id]
        return entries[-last_n:]

    # ── Status ──

    def get_status(self) -> Dict[str, Any]:
        return {
            "active_locks": len(self._locks),
            "audit_entries": len(self._audit),
            "has_fcntl": _HAS_FCNTL,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SharedFileRouter] = None
_instance_lock = threading.Lock()


def get_shared_file_router(data_dir: str = "") -> SharedFileRouter:
    """Return the global SharedFileRouter singleton."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        if not data_dir:
            from .. import DEFAULT_DATA_DIR
            data_dir = DEFAULT_DATA_DIR
        _instance = SharedFileRouter(data_dir)
        return _instance
