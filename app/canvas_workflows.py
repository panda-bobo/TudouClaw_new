"""
Workflow Store — visual orchestration canvas persistence.

Saves user-authored workflow graphs (node + edge collections) to disk so
the orchestration canvas can reload them across sessions and the (future)
execution engine can pick them up.

Schema (v1)
===========

```
{
  "id": "wf-<uuid>",
  "name": "GPU 集群方案 PPT 流程",
  "description": "...",
  "version": 1,                     # schema version, not user version
  "created_at": 1714560000.0,
  "updated_at": 1714560300.0,
  "created_by": "<user_id>",
  "nodes": [
    {
      "id": "n1",
      "type": "agent",              # start | agent | tool | decision | parallel | end
      "x": 120, "y": 80,
      "label": "画 GPU 拓扑图",
      "config": {                   # type-specific bag — opaque to the store
        "agent_id": "a16c2710acb6",
        "prompt": "用 drawio-skill 画...",
        "timeout": 300, "retry": 2
      }
    }
  ],
  "edges": [
    {"id": "e1", "from": "n1", "to": "n2", "label": "完成", "condition": ""}
  ]
}
```

Storage
=======

One JSON file per workflow under `<data_dir>/workflows/<id>.json`. Keeps
each workflow self-contained so users can dump / share / git-track them
individually if they want.

Validation is intentionally minimal at this layer — we trust the
canvas UI to produce sane payloads, and the future execution engine
will do the real semantic checks. The only invariants we enforce here:

  * `id` must be a non-empty string after the first save
  * `nodes` and `edges` must be lists
  * each node has an `id`; each edge's `from`/`to` reference real node ids
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tudou.workflows")


VALID_NODE_TYPES = {"start", "agent", "tool", "decision", "parallel", "end"}


_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _safe_id(raw: str) -> str:
    """Filesystem-safe slug for the on-disk filename. Workflow ids
    coming from the UI are already uuid-prefixed, but defensive
    sanitisation prevents path-escape attacks."""
    s = _ID_SAFE_RE.sub("-", raw or "").strip("-")
    return s[:120] or "wf-anonymous"


@dataclass
class WorkflowMeta:
    """Lightweight summary used by the list endpoint — reading every
    file's full nodes/edges is expensive when there are 100+ workflows,
    so the list view shows just metadata + counts."""
    id: str
    name: str
    description: str
    created_at: float
    updated_at: float
    created_by: str
    node_count: int
    edge_count: int

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "created_by": self.created_by,
            "node_count": self.node_count, "edge_count": self.edge_count,
        }


class WorkflowStore:
    """One JSON-per-workflow store. Thread-safe via per-store lock —
    fine-grained per-id locking would be overkill at this scale (we
    don't expect concurrent writes to the same workflow)."""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── Path resolution ──────────────────────────────────

    def _file_for(self, wf_id: str) -> Path:
        return self.root_dir / f"{_safe_id(wf_id)}.json"

    # ── List (cheap; reads only metadata) ───────────────

    def list_meta(self) -> list[WorkflowMeta]:
        """Return summary of every workflow on disk. Skips malformed
        files with a warning — they don't crash the listing."""
        out = []
        for f in sorted(self.root_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("skipping malformed workflow %s: %s", f.name, e)
                continue
            out.append(WorkflowMeta(
                id=str(d.get("id", "")),
                name=str(d.get("name", "(unnamed)")),
                description=str(d.get("description", "")),
                created_at=float(d.get("created_at", 0) or 0),
                updated_at=float(d.get("updated_at", 0) or 0),
                created_by=str(d.get("created_by", "")),
                node_count=len(d.get("nodes") or []),
                edge_count=len(d.get("edges") or []),
            ))
        # Most-recently-updated first — matches user expectation
        out.sort(key=lambda m: -m.updated_at)
        return out

    # ── Read one ────────────────────────────────────────

    def get(self, wf_id: str) -> dict | None:
        path = self._file_for(wf_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("failed to read workflow %s: %s", wf_id, e)
            return None

    # ── Save (create or overwrite) ──────────────────────

    def save(self, payload: dict, *, created_by: str = "") -> dict:
        """Validate, normalize, and persist a workflow. Returns the
        stored dict (with id/timestamps filled in by the server, not
        trusted from the client)."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("nodes/edges must be lists")

        # Validate node basics
        node_ids = set()
        for n in nodes:
            if not isinstance(n, dict): raise ValueError("each node must be a dict")
            nid = str(n.get("id", "")).strip()
            if not nid: raise ValueError("each node needs a non-empty id")
            if nid in node_ids: raise ValueError(f"duplicate node id: {nid}")
            node_ids.add(nid)
            ntype = str(n.get("type", "")).strip()
            if ntype and ntype not in VALID_NODE_TYPES:
                raise ValueError(f"unknown node type {ntype!r} (allowed: {sorted(VALID_NODE_TYPES)})")

        # Validate edge endpoints
        for e in edges:
            if not isinstance(e, dict): raise ValueError("each edge must be a dict")
            if not e.get("from") or not e.get("to"):
                raise ValueError("each edge needs from/to")
            if e["from"] not in node_ids:
                raise ValueError(f"edge.from {e['from']} not a known node")
            if e["to"] not in node_ids:
                raise ValueError(f"edge.to {e['to']} not a known node")

        with self._lock:
            now = time.time()
            wf_id = str(payload.get("id") or "").strip() or f"wf-{uuid.uuid4().hex[:12]}"
            existing = self.get(wf_id) or {}
            stored = {
                "id": wf_id,
                "name": str(payload.get("name", "") or existing.get("name", "Untitled"))[:120],
                "description": str(payload.get("description", "") or existing.get("description", ""))[:1000],
                "version": 1,
                "created_at": float(existing.get("created_at") or now),
                "updated_at": now,
                "created_by": str(existing.get("created_by") or created_by or ""),
                "nodes": nodes,
                "edges": edges,
            }
            path = self._file_for(wf_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return stored

    # ── Delete ──────────────────────────────────────────

    def delete(self, wf_id: str) -> bool:
        path = self._file_for(wf_id)
        if not path.exists():
            return False
        with self._lock:
            try:
                path.unlink()
                return True
            except Exception as e:
                logger.error("failed to delete workflow %s: %s", wf_id, e)
                return False


# ─────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────

_GLOBAL_STORE: WorkflowStore | None = None


def init_store(root_dir: str | Path) -> WorkflowStore:
    global _GLOBAL_STORE
    _GLOBAL_STORE = WorkflowStore(root_dir)
    return _GLOBAL_STORE


def get_store() -> WorkflowStore | None:
    return _GLOBAL_STORE
