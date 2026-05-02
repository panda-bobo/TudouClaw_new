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


# 2026-05-02: removed "tool" from the type set. The tool node was a
# deterministic shortcut that bypassed the agent's autonomy — but in
# practice the agent + its granted skills covers all the same use
# cases via natural-language prompts ("请用 drawio-skill 把 X 画了").
# Existing workflows with tool nodes get migrated to agent nodes by
# ``_migrate_tool_to_agent`` in ``save()`` below.
VALID_NODE_TYPES = {"start", "agent", "decision", "parallel", "end"}

# A workflow's executable status — drives whether the (future) execution
# engine will pick it up, and what badge the UI shows on list cards.
#
#   draft     — author is still editing; engine ignores it (default for new
#               workflows, also the safe fallback when validation fails).
#   ready     — author has marked it executable; structural validation
#               passed at the time of marking. Engine treats this as the
#               source of truth for "what can run".
#   disabled  — author/admin paused it; engine refuses to start new runs
#               but doesn't garbage-collect the file. Useful for retiring
#               old workflows without losing history.
#
# `last_validated_at` records the timestamp of the most recent transition
# to `ready` so the UI can warn if the workflow has been edited since.
EXECUTABLE_STATUSES = ("draft", "ready", "disabled")


_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _safe_id(raw: str) -> str:
    """Filesystem-safe slug for the on-disk filename. Workflow ids
    coming from the UI are already uuid-prefixed, but defensive
    sanitisation prevents path-escape attacks."""
    s = _ID_SAFE_RE.sub("-", raw or "").strip("-")
    return s[:120] or "wf-anonymous"


def _migrate_tool_nodes_inplace(nodes: list) -> int:
    """Convert legacy ``type: tool`` nodes to ``type: agent``.

    Mutates the list in place. Returns the count of migrated nodes.

    Tool nodes were removed 2026-05-02 — the agent + granted-skills
    paradigm covers the same use case via natural-language prompts
    ("请用 drawio-skill 把 X 画了"). A tool node's old config (tool_name,
    args) gets folded into an agent prompt so authors don't lose info.
    """
    migrated = 0
    for n in nodes:
        if not isinstance(n, dict) or n.get("type") != "tool":
            continue
        old_cfg = n.get("config") or {}
        old_tool = str(old_cfg.get("tool_name") or "").strip()
        old_args = old_cfg.get("args") or {}
        # Build a prompt that tells the agent what the tool node would
        # have done. If old config was empty (just-added node never
        # configured), leave prompt empty so author can fill in.
        prompt_parts = []
        if old_tool:
            prompt_parts.append(f"请调用 {old_tool} skill 完成本步。")
        if old_args:
            try:
                import json as _json
                prompt_parts.append(
                    f"建议参数: {_json.dumps(old_args, ensure_ascii=False)}"
                )
            except Exception:
                pass
        new_prompt = "\n".join(prompt_parts)
        n["type"] = "agent"
        n["config"] = {
            "agent_id": "",
            "prompt": new_prompt,
            "timeout": old_cfg.get("timeout", 300),
            "retry": old_cfg.get("retry", 0),
            # Preserve original config under a private key for one
            # release in case anyone needs to recover. Front-end
            # ignores unknown keys.
            "_migrated_from_tool": old_cfg,
        }
        # Update the visible label only if it's the default "工具调用"
        # — preserve any user-customised label.
        if (n.get("label") or "").strip() in ("", "工具调用", "Tool"):
            n["label"] = old_tool or "Agent 节点"
        migrated += 1
    return migrated


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
    executable_status: str = "draft"   # draft | ready | disabled
    last_validated_at: float = 0.0     # 0 = never validated as ready

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "created_by": self.created_by,
            "node_count": self.node_count, "edge_count": self.edge_count,
            "executable_status": self.executable_status,
            "last_validated_at": self.last_validated_at,
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
                executable_status=str(d.get("executable_status", "draft") or "draft"),
                last_validated_at=float(d.get("last_validated_at", 0) or 0),
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
            wf = json.loads(path.read_text(encoding="utf-8"))
            # Forward-compat: silently migrate legacy "tool" nodes to
            # "agent" nodes on read, so the front-end never sees a
            # node type it can't render. Persisted file isn't rewritten
            # here; the next save() will lock in the migrated form.
            _migrate_tool_nodes_inplace(wf.get("nodes") or [])
            return wf
        except Exception as e:
            logger.error("failed to read workflow %s: %s", wf_id, e)
            return None

    # ── Validation (semantic, beyond structural) ────────

    @staticmethod
    def validate_for_execution(wf: dict) -> list[str]:
        """Return a list of human-readable issues that prevent the
        workflow from being marked `ready`. Empty list = OK to run.

        Checks (MVP — extend as the execution engine grows):
          * exactly one `start` node
          * at least one `end` node
          * all nodes reachable from `start` (no orphans)
          * agent nodes don't have to specify agent_id (engine picks)
          * tool nodes must have config.tool_name
          * decision nodes must have config.condition
          * no cycles (MVP — loop semantics deferred)
          * every non-end node has at least one outgoing edge
        """
        issues: list[str] = []
        nodes = wf.get("nodes") or []
        edges = wf.get("edges") or []
        if not nodes:
            return ["workflow is empty (no nodes)"]

        by_id = {n["id"]: n for n in nodes}
        starts = [n for n in nodes if n.get("type") == "start"]
        ends   = [n for n in nodes if n.get("type") == "end"]
        if len(starts) == 0:
            issues.append("missing a start node")
        elif len(starts) > 1:
            issues.append(f"multiple start nodes ({len(starts)}); exactly one allowed")
        if len(ends) == 0:
            issues.append("missing at least one end node")

        # Adjacency map for reachability + cycle check
        adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for e in edges:
            adj.get(e.get("from"), []).append(e.get("to"))

        # Per-node config sanity
        for n in nodes:
            t = n.get("type")
            cfg = n.get("config") or {}
            if t == "decision":
                if not cfg.get("condition"):
                    issues.append(f"decision node '{n.get('label') or n['id']}' missing config.condition")
            if t == "agent":
                # success_when (optional) must be a dict; if file_glob is
                # set it must be a non-empty string. Saves the user from
                # discovering the bug at runtime when nothing matches.
                sw = cfg.get("success_when")
                if sw is not None and not isinstance(sw, dict):
                    issues.append(f"agent node '{n.get('label') or n['id']}' has success_when that isn't a dict")
                elif isinstance(sw, dict):
                    fg = sw.get("file_glob")
                    if fg is not None and not (isinstance(fg, str) and fg.strip()):
                        issues.append(f"agent node '{n.get('label') or n['id']}' has empty success_when.file_glob")
            # Non-end nodes need at least one outgoing edge
            if t != "end" and not adj.get(n["id"]):
                issues.append(f"node '{n.get('label') or n['id']}' ({t}) has no outgoing edge — execution would dead-end here")

        # Reachability from start
        if starts:
            seen = set()
            stack = [starts[0]["id"]]
            while stack:
                cur = stack.pop()
                if cur in seen: continue
                seen.add(cur)
                for nxt in adj.get(cur, []):
                    if nxt in by_id and nxt not in seen:
                        stack.append(nxt)
            orphans = [nid for nid in by_id if nid not in seen]
            for nid in orphans:
                issues.append(f"node '{by_id[nid].get('label') or nid}' is unreachable from start")

        # Cycle detection (DFS three-color)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in by_id}
        def _dfs(u: str) -> bool:
            color[u] = GRAY
            for v in adj.get(u, []):
                if v not in color: continue
                if color[v] == GRAY:
                    return True
                if color[v] == WHITE and _dfs(v):
                    return True
            color[u] = BLACK
            return False
        for nid in by_id:
            if color[nid] == WHITE and _dfs(nid):
                issues.append("workflow contains a cycle (loops not yet supported in MVP)")
                break

        return issues

    # ── Status transition (draft ↔ ready ↔ disabled) ────

    def set_status(self, wf_id: str, new_status: str) -> dict:
        """Change executable_status. Transition to `ready` runs the full
        execution validation; failures raise ValueError with the issues
        list joined into the message so the API surfaces them.

        Going draft / disabled never validates — those are safe states."""
        if new_status not in EXECUTABLE_STATUSES:
            raise ValueError(f"status must be one of {EXECUTABLE_STATUSES}")
        with self._lock:
            existing = self.get(wf_id)
            if not existing:
                raise FileNotFoundError(f"workflow {wf_id} not found")
            if new_status == "ready":
                issues = self.validate_for_execution(existing)
                if issues:
                    raise ValueError("cannot mark ready — issues:\n  • " + "\n  • ".join(issues))
            existing["executable_status"] = new_status
            existing["last_validated_at"] = time.time() if new_status == "ready" else existing.get("last_validated_at", 0.0)
            existing["updated_at"] = time.time()
            path = self._file_for(wf_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return existing

    # ── Save (create or overwrite) ──────────────────────

    def save(self, payload: dict, *, created_by: str = "") -> dict:
        """Validate, normalize, and persist a workflow. Returns the
        stored dict (with id/timestamps filled in by the server, not
        trusted from the client).

        Editing a `ready` workflow auto-demotes to `draft` — author must
        explicitly re-validate by calling set_status('ready') again. This
        prevents an in-progress edit from being silently picked up by
        the engine."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("nodes/edges must be lists")

        # Forward-compat: migrate legacy "tool" nodes to "agent" nodes
        # so the post-removal validation below doesn't reject them.
        # Front-end also runs this migration on load, so by the time a
        # save reaches us nodes should already be agent-type — this is
        # belt-and-suspenders for direct API callers.
        _migrate_tool_nodes_inplace(nodes)

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
            # Auto-demote to draft when an existing `ready` workflow gets
            # any structural edit. Author must re-validate to put it back.
            prior_status = str(existing.get("executable_status") or "draft")
            new_status = prior_status
            if existing and prior_status == "ready":
                # Compare nodes/edges dicts deeply — any change demotes
                if (nodes != existing.get("nodes")) or (edges != existing.get("edges")):
                    new_status = "draft"
                    logger.info("demoting %s ready→draft due to structural edit", wf_id)
            elif not existing:
                new_status = "draft"   # new workflows always start as draft
            stored = {
                "id": wf_id,
                "name": str(payload.get("name", "") or existing.get("name", "Untitled"))[:120],
                "description": str(payload.get("description", "") or existing.get("description", ""))[:1000],
                "version": 1,
                "created_at": float(existing.get("created_at") or now),
                "updated_at": now,
                "created_by": str(existing.get("created_by") or created_by or ""),
                "executable_status": new_status,
                "last_validated_at": float(existing.get("last_validated_at") or 0.0),
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
