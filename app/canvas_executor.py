"""
Canvas workflow execution engine (HANDOFF [D]).

Consumes the schema authored in :mod:`app.canvas_workflows`. One
``WorkflowRun`` per execution; the engine drives the run by walking the
DAG, invoking per-node-type executors, and persisting state + an
append-only event log.

MVP boundaries (HANDOFF [D]):
  * **Implemented node types**: start, end, agent, tool.
  * **Skipped (clear error)**: decision, parallel — the run fails with
    a structured "node type not yet supported" error so the author
    knows what to remove. Will land in a follow-up.
  * **No retries** — first failure terminates the run.
  * **No human-in-the-loop pause nodes**.
  * **Variable substitution** (``{{node_id.key}}``) is implemented here
    in the executor — HANDOFF [H] is now just the UI affordance.

Persistence layout (under ``<data_dir>/canvas_runs/``):

  * ``<run_id>.json`` — run state snapshot (overwritten on each
    transition; tiny, ~1KB).
  * ``<run_id>.events.jsonl`` — append-only event log, one JSON object
    per line. Drives the SSE event stream that the canvas UI ([E])
    consumes for live highlighting.

Concurrency: each run executes on its own daemon thread; multiple runs
of the same workflow can race in the engine, but each run owns its own
state file. The engine itself is thread-safe (lock around the run
registry).
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
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("tudou.canvas_executor")


# ── State enums ─────────────────────────────────────────────────────────


class RunState(str, Enum):
    PENDING = "pending"      # created, not yet started
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class NodeState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_RUN_STATES = {RunState.SUCCEEDED, RunState.FAILED, RunState.ABORTED}
TERMINAL_NODE_STATES = {NodeState.SUCCEEDED, NodeState.FAILED,
                        NodeState.SKIPPED}


# ── Variable substitution ───────────────────────────────────────────────


_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _has_real_deliverable(node_dir: "Path | str") -> bool:
    """Return True iff the agent node's subdir contains any file
    other than _meta.json. Used by _exec_agent to fail-fast on
    EMPTY_DELIVERABLE per spec 2026-05-02 — protects against the
    silent "LLM said it wrote a file but actually didn't" failure
    mode.

    A nonexistent path returns False (caller wants the same
    "no real output" semantics).
    """
    from pathlib import Path as _P
    base = _P(str(node_dir))
    if not base.is_dir():
        return False
    try:
        for p in base.rglob("*"):
            if p.is_file() and p.name != "_meta.json":
                return True
    except OSError:
        return False
    return False


def _scan_for_marker_file(shared_dir: "Path | str",
                          file_glob: str,
                          pre_snapshot: dict[str, float]) -> str | None:
    """Find a NEW file under ``shared_dir`` whose basename or relative
    path matches ``file_glob`` (fnmatch syntax). "New" = its rel-path
    is NOT in ``pre_snapshot`` (the dir snapshot taken before the
    agent's chat turn started).

    Used by the agent node executor's success_when.file_glob path so
    we can declare the node done as soon as the deliverable lands,
    without waiting for the LLM to also finish narrating.

    Returns the relative path string, or None if nothing matches.
    Catches OSError silently (e.g. shared_dir vanished) — caller will
    just keep polling until timeout.
    """
    from fnmatch import fnmatch
    from pathlib import Path as _P
    base = _P(str(shared_dir))
    if not base.is_dir():
        return None
    try:
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                continue
            if rel in pre_snapshot:
                continue
            if fnmatch(p.name, file_glob) or fnmatch(rel, file_glob):
                return rel
    except OSError:
        return None
    return None


def _substitute_vars(template: Any, vars_dict: dict[str, Any]) -> Any:
    """Replace ``{{node_id.key}}`` references in any string field.

    Recurses into dicts and lists. Non-string scalars (int, bool, None)
    pass through unchanged. Missing variables raise ``KeyError`` with a
    clear message — silently substituting empty string would mask bugs.
    """
    if isinstance(template, str):
        def _repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in vars_dict:
                raise KeyError(
                    f"workflow variable not defined: {{{{{key}}}}} "
                    f"(available: {sorted(vars_dict.keys())[:5]}...)"
                )
            return str(vars_dict[key])
        return _VAR_PATTERN.sub(_repl, template)
    if isinstance(template, dict):
        return {k: _substitute_vars(v, vars_dict) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute_vars(v, vars_dict) for v in template]
    return template


# ── Run model ───────────────────────────────────────────────────────────


@dataclass
class WorkflowRun:
    """One execution instance of a canvas workflow."""
    id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    workflow_id: str = ""
    workflow_name: str = ""
    started_by: str = ""
    state: RunState = RunState.PENDING
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    # Per-node state — keyed by node id from the workflow definition.
    node_states: dict[str, NodeState] = field(default_factory=dict)
    node_started: dict[str, float] = field(default_factory=dict)
    node_finished: dict[str, float] = field(default_factory=dict)
    node_errors: dict[str, str] = field(default_factory=dict)
    # Variable store — populated as nodes complete. Keys are
    # ``{node_id}.{output_key}`` strings.
    vars: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "started_by": self.started_by,
            "state": self.state.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "node_states": {k: v.value for k, v in self.node_states.items()},
            "node_started": dict(self.node_started),
            "node_finished": dict(self.node_finished),
            "node_errors": dict(self.node_errors),
            "vars": dict(self.vars),
        }


# ── Persistence ─────────────────────────────────────────────────────────


class RunStore:
    """Persists run state + append-only event log to disk.

    Layout (2026-05-02 — was flat ``<root>/<run_id>.json``):

        <root>/<run_id>/state.json          ← run state snapshot
        <root>/<run_id>/events.jsonl        ← append-only event stream
        <root>/<run_id>/shared/             ← per-run workspace (artifacts)
        <root>/<run_id>/artifacts.json      ← artifact index
        <root>/<run_id>/audit.jsonl         ← artifact audit log

    Per-run dir keeps everything related to that run in one place.
    Migration from the old flat layout runs once at hub startup —
    see :func:`migrate_old_run_layout` below.
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._registry_lock:
            lk = self._locks.get(run_id)
            if lk is None:
                lk = threading.Lock()
                self._locks[run_id] = lk
            return lk

    def _run_dir(self, run_id: str) -> Path:
        d = self.root_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _state_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "state.json"

    def _events_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.jsonl"

    def save_state(self, run: WorkflowRun) -> None:
        path = self._state_path(run.id)
        tmp = path.with_suffix(".json.tmp")
        with self._lock_for(run.id):
            tmp.write_text(json.dumps(run.to_dict(), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def load_state(self, run_id: str) -> dict | None:
        path = self._state_path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("failed to read run %s: %s", run_id, e)
            return None

    def append_event(self, run_id: str, event: dict) -> None:
        """Append one event to the run's JSONL log. Each event must
        already contain ``ts`` (unix epoch seconds) and ``type``."""
        path = self._events_path(run_id)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock_for(run_id):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_events(self, run_id: str, since_offset: int = 0
                    ) -> tuple[list[dict], int]:
        """Read events from byte offset ``since_offset``.

        Returns ``(events, new_offset)``. Used by the SSE endpoint to
        stream incremental progress without re-sending older events.
        """
        path = self._events_path(run_id)
        if not path.exists():
            return [], 0
        with self._lock_for(run_id):
            try:
                with open(path, "rb") as f:
                    f.seek(since_offset)
                    raw = f.read()
                    new_offset = since_offset + len(raw)
            except OSError as e:
                logger.warning("read_events %s failed: %s", run_id, e)
                return [], since_offset
        events = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, new_offset

    def list_runs_for_workflow(self, workflow_id: str) -> list[dict]:
        """Return run summaries for one workflow, newest first.

        Walks per-run subdirectories under root; reads ``state.json``
        in each. Skips dirs that don't have a state.json yet (run was
        created but never completed first save) or that don't match
        ``workflow_id``.
        """
        out = []
        if not self.root_dir.exists():
            return out
        for d in self.root_dir.iterdir():
            if not d.is_dir() or not d.name.startswith("run-"):
                continue
            state_file = d / "state.json"
            if not state_file.is_file():
                continue
            try:
                payload = json.loads(state_file.read_text(encoding="utf-8"))
                if payload.get("workflow_id") == workflow_id:
                    out.append({
                        "id": payload.get("id"),
                        "state": payload.get("state"),
                        "started_at": payload.get("started_at"),
                        "finished_at": payload.get("finished_at"),
                        "error": payload.get("error", "")[:200],
                    })
            except Exception:
                continue
        out.sort(key=lambda r: -(r.get("started_at") or 0))
        return out


def migrate_old_run_layout(root_dir: str | Path) -> int:
    """Move legacy flat-layout run files into per-run subdirectories.

    Old:  <root>/run-X.json + run-X.events.jsonl
    New:  <root>/run-X/state.json + run-X/events.jsonl

    Idempotent: if a per-run dir already exists with the target file,
    skip rather than overwrite. Returns count of migrated runs.
    """
    root = Path(root_dir)
    if not root.exists():
        return 0
    moved = 0
    for f in list(root.iterdir()):
        if not f.is_file() or not f.name.startswith("run-") or not f.name.endswith(".json"):
            continue
        # Skip the new-layout state.json that doesn't sit at top level
        # (defensive — shouldn't happen since we glob top-level only)
        run_id = f.stem
        target_dir = root / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        new_state = target_dir / "state.json"
        if not new_state.exists():
            try:
                f.rename(new_state)
                moved += 1
            except OSError as e:
                logger.warning("migrate state file failed for %s: %s", run_id, e)
                continue
        else:
            # New layout already populated — drop the stale flat file
            try:
                f.unlink()
            except OSError:
                pass
        # Also move the events file
        old_events = root / f"{run_id}.events.jsonl"
        new_events = target_dir / "events.jsonl"
        if old_events.is_file():
            if not new_events.exists():
                try:
                    old_events.rename(new_events)
                except OSError as e:
                    logger.warning("migrate events file failed for %s: %s", run_id, e)
            else:
                try:
                    old_events.unlink()
                except OSError:
                    pass
    if moved:
        logger.info("Migrated %d canvas run(s) to per-run-dir layout under %s", moved, root)
    return moved


# ── Engine ──────────────────────────────────────────────────────────────


class WorkflowEngine:
    """Drives one or more ``WorkflowRun`` instances to completion.

    The engine holds no per-workflow scheduling state — runs are
    triggered by an external caller (typically the canvas API). Each
    triggered run gets its own daemon thread.
    """

    def __init__(self, store: RunStore, hub: Any = None):
        self.store = store
        self.hub = hub
        self._runs: dict[str, WorkflowRun] = {}
        self._lock = threading.RLock()

    # ── Triggering / lookup ──

    def trigger(self, workflow: dict, started_by: str = "") -> WorkflowRun:
        """Kick off a new run of ``workflow``.

        ``workflow`` is the full dict from
        :meth:`canvas_workflows.WorkflowStore.get`. The engine does NOT
        re-check ``executable_status == "ready"`` here — that's the
        caller's job (the API layer does it).

        Returns the ``WorkflowRun`` so the caller can poll / surface
        its id.
        """
        # Pre-flight validation: the same checks the store does on
        # status transition. Catches the "marked ready then someone
        # deleted a referenced agent" case.
        from .canvas_workflows import WorkflowStore
        issues = WorkflowStore.validate_for_execution(workflow)
        if issues:
            raise ValueError(
                "workflow failed pre-flight validation:\n  • "
                + "\n  • ".join(issues)
            )

        run = WorkflowRun(
            workflow_id=str(workflow.get("id", "")),
            workflow_name=str(workflow.get("name", "")),
            started_by=started_by,
            state=RunState.PENDING,
            started_at=time.time(),
        )
        # Initialize per-node state
        for n in workflow.get("nodes") or []:
            run.node_states[n["id"]] = NodeState.PENDING

        with self._lock:
            self._runs[run.id] = run
        self.store.save_state(run)
        self._emit(run, "run_created", {})

        t = threading.Thread(
            target=self._drive,
            args=(run, workflow),
            name=f"canvas-run-{run.id[:8]}",
            daemon=True,
        )
        t.start()
        return run

    def get_run(self, run_id: str) -> WorkflowRun | None:
        with self._lock:
            return self._runs.get(run_id)

    # ── Retry ──

    def resume_run(self, run_id: str, workflow: dict) -> WorkflowRun:
        """Resume a failed/aborted run from its failed nodes.

        Reuses the SAME ``run_id`` (and therefore the same shared dir,
        artifact index, audit log) — appends new events to the existing
        events.jsonl. SUCCEEDED nodes keep their state + outputs vars
        intact so downstream nodes can still ``{{...}}``-reference
        them.

        Reset semantics:
          * FAILED  → PENDING, clear node_errors[id], clear node_finished[id]
          * SKIPPED → PENDING, clear node_finished[id]
          * RUNNING (stale, e.g. crashed) → PENDING
          * SUCCEEDED → unchanged
          * PENDING → unchanged

        Run-level fields:
          * state         → RUNNING
          * error         → ""
          * finished_at   → 0.0
          * started_at    → unchanged (preserves original start time;
                            individual node started_at gets refreshed
                            when the driver picks them up)

        Spawns a fresh driver thread. Caller doesn't await — UI polls
        via /runs/{id} or the SSE events stream.
        """
        from .canvas_workflows import WorkflowStore
        # Pre-flight: re-validate workflow (agent could have been
        # deleted between original run and retry).
        issues = WorkflowStore.validate_for_execution(workflow)
        if issues:
            raise ValueError(
                "workflow failed pre-flight validation:\n  • "
                + "\n  • ".join(issues)
            )

        state = self.store.load_state(run_id)
        if not state:
            raise ValueError(f"run {run_id} not found on disk")
        cur_state = state.get("state")
        if cur_state in ("running",):
            raise ValueError(
                f"run {run_id} is currently {cur_state} — abort it first "
                f"before retrying"
            )
        if cur_state == "succeeded":
            raise ValueError(
                f"run {run_id} already succeeded — nothing to retry"
            )

        # Rebuild WorkflowRun from on-disk state
        run = WorkflowRun(
            id=state.get("id", run_id),
            workflow_id=state.get("workflow_id", ""),
            workflow_name=state.get("workflow_name", ""),
            started_by=state.get("started_by", ""),
            state=RunState.RUNNING,
            started_at=float(state.get("started_at") or time.time()),
            finished_at=0.0,
            error="",
            node_states={k: NodeState(v) for k, v in (state.get("node_states") or {}).items()},
            node_started=dict(state.get("node_started") or {}),
            node_finished=dict(state.get("node_finished") or {}),
            node_errors=dict(state.get("node_errors") or {}),
            vars=dict(state.get("vars") or {}),
        )

        # Reset non-success nodes
        reset_count = 0
        for nid, st in list(run.node_states.items()):
            if st in (NodeState.FAILED, NodeState.SKIPPED, NodeState.RUNNING):
                run.node_states[nid] = NodeState.PENDING
                run.node_errors.pop(nid, None)
                run.node_finished.pop(nid, None)
                reset_count += 1

        # Pre-flight: nothing to do if no nodes were reset (e.g. all
        # SUCCEEDED but state was somehow FAILED). Caller handled the
        # only-succeeded case above; this is a safety net.
        if reset_count == 0:
            raise ValueError(
                f"run {run_id} has no failed/skipped nodes to retry"
            )

        with self._lock:
            self._runs[run.id] = run
        self.store.save_state(run)
        self._emit(run, "run_resumed", {
            "reset_count": reset_count,
            "kept_succeeded": sum(1 for s in run.node_states.values() if s == NodeState.SUCCEEDED),
        })

        t = threading.Thread(
            target=self._drive_resumed,
            args=(run, workflow),
            name=f"canvas-resume-{run.id[:8]}",
            daemon=True,
        )
        t.start()
        return run

    def _drive_resumed(self, run: WorkflowRun, workflow: dict) -> None:
        """Like ``_drive`` but doesn't emit ``run_started`` (use
        ``run_resumed`` instead) and doesn't reset run.started_at."""
        # Don't re-emit run_started; resume_run already emitted run_resumed.
        # Just call the main driver loop body. To minimize duplication,
        # we inline only the differences and delegate to a shared helper.
        try:
            self._drive_loop(run, workflow)
        except Exception as e:
            logger.exception("resumed run %s crashed", run.id)
            self._finish(run, RunState.FAILED,
                         f"engine crashed during resume: {type(e).__name__}: {e}")

    # ── Event emission ──

    def _emit(self, run: WorkflowRun, event_type: str,
              payload: dict | None = None) -> None:
        evt = {
            "ts": time.time(),
            "run_id": run.id,
            "type": event_type,
            "data": payload or {},
        }
        try:
            self.store.append_event(run.id, evt)
        except Exception as e:
            logger.warning("append_event failed for %s: %s", run.id, e)

    # ── Run driver ──

    def _drive(self, run: WorkflowRun, workflow: dict) -> None:
        """Walk the DAG and execute nodes in topological order.

        MVP scheduling: pick any node whose deps are all SUCCEEDED, run
        it, repeat. Single-threaded execution — parallel fan-out is
        deferred (when the parallel node type lands, this driver will
        spawn children and wait).
        """
        run.state = RunState.RUNNING
        run.started_at = time.time()
        self.store.save_state(run)
        self._emit(run, "run_started", {
            "workflow_id": run.workflow_id,
            "workflow_name": run.workflow_name,
        })
        try:
            self._drive_loop(run, workflow)
        except Exception as e:
            logger.exception("run %s crashed", run.id)
            self._finish(run, RunState.FAILED,
                         f"engine crashed: {type(e).__name__}: {e}")

    def _drive_loop(self, run: WorkflowRun, workflow: dict) -> None:
        """Common driver loop body. Shared by ``_drive`` (fresh runs)
        and ``_drive_resumed`` (retries). Iterates until all nodes are
        in terminal state or no more progress is possible."""
        nodes_by_id = {n["id"]: n for n in workflow.get("nodes") or []}
        edges = workflow.get("edges") or []

        # Build deps map — node N depends on every node that has an
        # outgoing edge into N.
        deps: dict[str, list[str]] = {nid: [] for nid in nodes_by_id}
        for e in edges:
            src, dst = e.get("from"), e.get("to")
            if src in nodes_by_id and dst in nodes_by_id:
                deps[dst].append(src)

        while True:
            ready = self._pick_ready(run, nodes_by_id, deps)
            if ready is None:
                # Nothing ready — either we're done, or we've stalled.
                if all(s in TERMINAL_NODE_STATES
                       for s in run.node_states.values()):
                    # All nodes terminal → run done.
                    any_failed = any(
                        s == NodeState.FAILED
                        for s in run.node_states.values()
                    )
                    if any_failed:
                        self._finish(run, RunState.FAILED,
                                     "one or more nodes failed")
                    else:
                        self._finish(run, RunState.SUCCEEDED, "")
                    return
                # Stalled — some nodes pending but no deps satisfied.
                # Means a cycle (validate should've caught this) or a
                # disconnected subgraph. Bail.
                pending = [nid for nid, s in run.node_states.items()
                           if s == NodeState.PENDING]
                self._finish(run, RunState.FAILED,
                             f"stalled — pending nodes have unsatisfied "
                             f"deps: {pending[:5]}")
                return

            node = nodes_by_id[ready]
            self._execute_node(run, node, edges)

    def _pick_ready(self, run: WorkflowRun,
                    nodes_by_id: dict[str, dict],
                    deps: dict[str, list[str]]) -> str | None:
        """Return the id of one node that's ready to run, or None."""
        for nid, node in nodes_by_id.items():
            if run.node_states.get(nid) != NodeState.PENDING:
                continue
            # All deps must be SUCCEEDED to proceed. If any dep is
            # FAILED or SKIPPED, we mark this node SKIPPED (not FAILED
            # — its inputs never landed, so it never had a chance).
            dep_states = [run.node_states.get(d, NodeState.PENDING)
                          for d in deps.get(nid, [])]
            if any(s in (NodeState.FAILED, NodeState.SKIPPED)
                   for s in dep_states):
                # Skip this node — upstream is broken
                run.node_states[nid] = NodeState.SKIPPED
                run.node_finished[nid] = time.time()
                self.store.save_state(run)
                self._emit(run, "node_skipped", {
                    "node_id": nid, "node_type": node.get("type"),
                    "reason": "upstream failed or skipped",
                })
                # Loop again to pick the next one
                continue
            if all(s == NodeState.SUCCEEDED for s in dep_states):
                return nid
        return None

    def _execute_node(self, run: WorkflowRun, node: dict,
                      edges: list[dict]) -> None:
        """Execute one node end-to-end: state transitions + dispatch +
        event emission + variable capture.

        Failure of an individual node does NOT raise — it's recorded
        as NodeState.FAILED so the driver can decide whether to skip
        downstream or stop.

        ``edges`` is the workflow's edge list — needed for the decision
        node's post-execution hook (skip non-chosen branches).
        """
        nid = node["id"]
        ntype = node.get("type", "")
        run.node_states[nid] = NodeState.RUNNING
        run.node_started[nid] = time.time()
        self.store.save_state(run)
        self._emit(run, "node_started", {
            "node_id": nid, "node_type": ntype,
            "label": node.get("label", ""),
        })

        try:
            executor = _NODE_EXECUTORS.get(ntype)
            if executor is None:
                raise NotImplementedError(
                    f"node type {ntype!r} not supported by this engine"
                )

            # Variable substitution on the config bag before dispatch.
            raw_config = node.get("config") or {}
            try:
                config = _substitute_vars(raw_config, run.vars)
            except KeyError as ke:
                raise RuntimeError(f"variable substitution failed: {ke}")

            outputs = executor(self, run, node, config)
            # Capture outputs as variables {node_id.key}
            if isinstance(outputs, dict):
                for k, v in outputs.items():
                    run.vars[f"{nid}.{k}"] = v
            run.node_states[nid] = NodeState.SUCCEEDED
            run.node_finished[nid] = time.time()
            self.store.save_state(run)
            self._emit(run, "node_succeeded", {
                "node_id": nid, "node_type": ntype,
                "outputs": outputs if isinstance(outputs, dict) else {},
            })

            # Post-hook: decision nodes mark non-chosen branches as
            # SKIPPED so the driver doesn't try to execute them.
            if ntype == "decision" and isinstance(outputs, dict):
                self._skip_unchosen_branches(
                    run, nid, str(outputs.get("branch", "")), edges
                )
        except Exception as e:
            run.node_states[nid] = NodeState.FAILED
            run.node_finished[nid] = time.time()
            run.node_errors[nid] = f"{type(e).__name__}: {e}"
            self.store.save_state(run)
            self._emit(run, "node_failed", {
                "node_id": nid, "node_type": ntype,
                "error": run.node_errors[nid],
            })

    def _skip_unchosen_branches(self, run: WorkflowRun, decision_nid: str,
                                 chosen_branch: str,
                                 edges: list[dict]) -> None:
        """Walk edges out of a decision node; mark targets of
        non-matching edges as SKIPPED so the driver won't run them.

        Edge-label semantics: each outgoing edge from a decision node
        carries a ``label`` of "yes" / "no" / etc. The decision node's
        executor returns ``branch`` (e.g., "yes" if the condition was
        truthy). Edges whose label != chosen_branch get their target
        skipped. Edges with no label fall through to the chosen branch
        (so authors who didn't label edges still get linear flow).
        """
        chosen_lower = chosen_branch.strip().lower()
        for e in edges:
            if e.get("from") != decision_nid:
                continue
            edge_label = (e.get("label") or "").strip().lower()
            if not edge_label or edge_label == chosen_lower:
                continue   # edge is taken — don't skip
            target = e.get("to")
            if target and run.node_states.get(target) == NodeState.PENDING:
                run.node_states[target] = NodeState.SKIPPED
                run.node_finished[target] = time.time()
                self.store.save_state(run)
                self._emit(run, "node_skipped", {
                    "node_id": target,
                    "reason": (
                        f"decision '{decision_nid}' chose '{chosen_branch}', "
                        f"edge label '{edge_label}' not taken"
                    ),
                })

    def _finish(self, run: WorkflowRun, state: RunState, error: str) -> None:
        run.state = state
        run.finished_at = time.time()
        run.error = error
        self.store.save_state(run)
        self._emit(run, f"run_{state.value}", {
            "duration_s": run.finished_at - run.started_at,
            "error": error,
        })


# ── Per-type node executors ─────────────────────────────────────────────
#
# Signature: ``executor(engine, run, node, resolved_config) -> dict``
# where the returned dict's keys become variables under ``{node_id.key}``.
# Raise an exception to mark the node failed; the message goes into the
# event stream as the failure reason.


def _exec_start(engine: WorkflowEngine, run: WorkflowRun,
                node: dict, config: dict) -> dict:
    """Start node: no-op success. Marks the run as actively executing."""
    return {"started_at": time.time()}


def _exec_end(engine: WorkflowEngine, run: WorkflowRun,
              node: dict, config: dict) -> dict:
    """End node: no-op success. The driver detects all-terminal-states
    and finalizes the run; this just lets the end node show as succeeded
    in the UI."""
    return {"finished_at": time.time()}


def _exec_agent(engine: WorkflowEngine, run: WorkflowRun,
                node: dict, config: dict) -> dict:
    """Agent node: posts a chat turn to the configured agent and waits
    for completion.

    Required config:
      * ``agent_id`` — which agent to talk to
      * ``prompt``  — the user-message-equivalent text to send

    Optional config:
      * ``timeout`` — seconds to wait for completion (default 300)

    Side effects (HANDOFF artifact closed-loop, 2026-05-02):
      * Before invoking, snapshot ``<run>/shared/`` so the post-scan
        can diff for newly-produced files.
      * Set ``agent.working_dir`` to ``<run>/shared/`` so relative
        write_file paths land in the workflow shared dir (visible
        to downstream nodes).
      * Add ``<run>/shared/`` to the agent's sandbox ``allowed_dirs``
        for the duration of the call.
      * After completion, register every new/modified file as an
        artifact and inject ``{node_id.file_<sanitized_name>}`` into
        ``run.vars`` so downstream nodes can ``{{...}}``-reference
        the path.
    """
    if engine.hub is None:
        raise RuntimeError(
            "agent node requires hub to be wired into the engine"
        )

    agent_id = config.get("agent_id", "")
    prompt = config.get("prompt", "")
    timeout = float(config.get("timeout", 300))
    if not agent_id:
        raise ValueError("agent node missing config.agent_id")
    if not prompt or not str(prompt).strip():
        raise ValueError("agent node missing config.prompt")

    # ── success_when: optional early-termination by file marker ──
    # When configured, executor polls the node's subdir for a NEW
    # file matching the glob. First match → task.abort() + node
    # SUCCEEDED, even if the LLM is still narrating. Solves the
    # "finished 9s after timeout" race seen on 2026-05-02.
    # The deliverable variable is unaffected — always points at
    # the whole subdir per the design spec.
    success_when = config.get("success_when") or {}
    if not isinstance(success_when, dict):
        success_when = {}
    success_file_glob = str(success_when.get("file_glob") or "").strip()

    agent = engine.hub.get_agent(agent_id) if hasattr(
        engine.hub, "get_agent") else None
    if agent is None:
        raise RuntimeError(f"agent {agent_id!r} not found in hub")

    # ── Per-run isolated chat context ──
    # Each canvas-run-of-this-node gets a FRESH conversation bucket so
    # the agent's solo / project chat history can't leak in. Without
    # this, `agent.chat_async(prompt)` appends to whatever the agent
    # was last talking about — we hit a real contamination case where
    # an agent asked to "search AI hot news" returned content from a
    # previous unrelated Gumroad / gcplist conversation that lived in
    # solo. Switch back + drop the bucket in the finally below so the
    # agent's persisted message dict doesn't accumulate one entry per
    # canvas run.
    canvas_ctx_id = f"canvas:{run.id[:12]}:{node.get('id','x')}"
    prior_ctx_id = getattr(agent, "_active_context_id", "solo") or "solo"
    try:
        with agent._lock:
            # Replace any leftover bucket so retries also start fresh
            agent._messages_by_context[canvas_ctx_id] = []
            agent._switch_context(canvas_ctx_id)
    except Exception as _ce:
        logger.warning("canvas: failed to switch agent context: %s", _ce)
        canvas_ctx_id = None  # signal: nothing to restore

    # ── Artifact closed-loop setup ──
    # Each agent node owns a private subdirectory under shared/. Write
    # isolation: working_dir = shared/<node_id>/ so files this agent
    # produces can't collide with sibling nodes. Read isolation NOT
    # enforced: sandbox covers the whole shared/ so downstream nodes
    # can still reach upstream outputs via {{nid.deliverable}}.
    # On retry of the same node we wipe the subdir per the "直接覆盖"
    # decision (2026-05-02) — audit log retains the history.
    from . import canvas_artifacts as _ca
    artifact_store = _ca.get_store()
    pre_snapshot: dict[str, float] = {}
    prior_working_dir = None
    sandbox_extended = False
    node_id = node.get("id", "")
    node_dir = None
    if artifact_store is not None:
        shared_dir_root = artifact_store.shared_dir(run.id)
        # fresh=True: wipe any leftover from a previous attempt of THIS
        # same node (retry / resume). Other nodes' subdirs are untouched.
        node_dir = artifact_store.node_dir(run.id, node_id, fresh=True)
        # Pre-snapshot: the wiped node dir is empty, so this is just {}.
        # Keeping the call so the contract stays "diff registers only
        # what changed since pre" rather than "registers everything".
        pre_snapshot = artifact_store.snapshot_dir(run.id, subdir=node_id)
        # Set the agent's working_dir for the duration of this turn.
        prior_working_dir = getattr(agent, "working_dir", None)
        try:
            agent.working_dir = str(node_dir)
        except Exception:
            prior_working_dir = None   # couldn't set; don't try to restore
        # Extend sandbox to allow the WHOLE shared/ tree (not just the
        # node subdir) so the agent can READ sibling nodes' deliverables
        # via {{upstream.deliverable}}. Writes naturally land in
        # working_dir = node subdir.
        try:
            from . import sandbox as _sb
            pol = _sb.get_current_policy()
            if pol is not None and str(shared_dir_root) not in pol.allowed_dirs:
                pol.allowed_dirs.append(str(shared_dir_root))
                sandbox_extended = True
        except Exception as _se:
            logger.debug("sandbox extension failed: %s", _se)
        # Drop _meta.json with the in-flight values (finished_at + etc
        # filled in after the chat completes below). Lets a debugger
        # grep `find shared -name _meta.json` and reconstruct the run.
        try:
            artifact_store.write_node_meta(run.id, node_id, {
                "node_id": node_id,
                "node_label": node.get("label", ""),
                "agent_id": agent_id,
                "agent_name": getattr(agent, "name", ""),
                "started_at": time.time(),
                "success_when": success_when if success_file_glob else {},
            })
        except Exception:
            pass
        artifact_store.record_audit(
            run.id,
            actor_agent_id=agent_id,
            actor_node_id=node_id,
            action="scan_start",
            extra={"pre_files": len(pre_snapshot), "node_dir": str(node_dir)},
        )

    try:
        # Submit chat turn — chat_async returns a ChatTask we can poll.
        task = agent.chat_async(str(prompt),
                                source=f"canvas:{run.workflow_id}:{node['id']}")

        # Poll until terminal OR success_when matcher fires. ChatTaskStatus
        # enum has terminal members COMPLETED / FAILED / ABORTED. If
        # the matcher fires first we abort the task and treat as success.
        from .chat_task import ChatTaskStatus
        deadline = time.time() + timeout
        marker_file: str | None = None
        # When polling for a marker we go a bit faster so the agent
        # doesn't keep grinding for several seconds after producing
        # the file. 0.5s is fine when only checking task.status.
        poll_interval = 0.3 if success_file_glob else 0.5

        while time.time() < deadline:
            if task.status in (ChatTaskStatus.COMPLETED,
                               ChatTaskStatus.FAILED,
                               ChatTaskStatus.ABORTED):
                break
            if success_file_glob and artifact_store is not None and node_dir is not None:
                # Scope the marker scan to THIS node's subdir only —
                # otherwise an unrelated sibling node landing a matching
                # file would falsely close us. Pre-snapshot is also
                # subdir-scoped so the rel_path keys line up.
                marker_file = _scan_for_marker_file(
                    node_dir,
                    success_file_glob,
                    {k.split("/", 1)[-1] if "/" in k else k: v
                     for k, v in pre_snapshot.items()},
                )
                if marker_file:
                    # Stop the LLM loop ASAP — the deliverable is in.
                    try:
                        task.abort()
                    except Exception:
                        pass
                    break
            time.sleep(poll_interval)
        else:
            # Timeout — try to abort the task so we don't leave it running
            try:
                task.abort()
            except Exception:
                pass
            raise TimeoutError(
                f"agent node timed out after {timeout}s "
                f"(task {task.id}, status {task.status.value})"
            )

        # If success_when fired, treat as success regardless of LLM state.
        # Otherwise require the chat task to have ended in COMPLETED.
        if marker_file:
            logger.info(
                "agent node %s closed early by file marker: %s "
                "(LLM state was %s)", node.get("id", ""),
                marker_file, task.status.value,
            )
        elif task.status != ChatTaskStatus.COMPLETED:
            raise RuntimeError(
                f"agent task {task.id} ended in {task.status.value}: "
                f"{(task.error or task.result or '')[:200]}"
            )

        outputs: dict[str, Any] = {
            "output": task.result or (
                f"(closed early by file marker: {marker_file})"
                if marker_file else ""
            ),
            "task_id": task.id,
            "duration_s": task.updated_at - task.created_at,
        }
        # ── deliverable variable ──
        # The deliverable is ALWAYS the node's subdir (per spec
        # 2026-05-02 — file vs directory distinction is YAGNI).
        # If success_when.file_glob fired, the matched file is just
        # one item inside that subdir; downstream LLM finds it via
        # ls / read_file.
        if node_dir is not None:
            outputs["deliverable"] = str(node_dir)
            outputs["deliverable_relative"] = f"{node_id}/"

        # ── Artifact closed-loop post-scan ──
        # Diff the node's subdir against the pre-snapshot. Scoping to
        # the subdir means we don't re-register sibling nodes' files
        # on every run (since the artifact index is keyed by rel_path
        # relative to shared/, those sibling files are already there
        # under their own producer_node_id).
        if artifact_store is not None and node_dir is not None:
            try:
                new_artifacts = artifact_store.diff_and_register(
                    run.id,
                    pre_snapshot=pre_snapshot,
                    producer_node_id=node_id,
                    producer_agent_id=agent_id,
                    subdir=node_id,
                )
                for art in new_artifacts:
                    # Absolute path so downstream {{...}} substitution
                    # gives the agent something it can read directly.
                    abs_path = str(artifact_store.shared_dir(run.id) / art.rel_path)
                    # vars_key = "{node_id}.file_{sanitized_name}"; the
                    # caller of this executor strips the "{node_id}." prefix
                    # when storing in run.vars (see _execute_node), so we
                    # return the suffix only.
                    suffix = art.vars_key.split(".", 1)[-1]
                    outputs[suffix] = abs_path
                outputs["artifact_ids"] = [a.id for a in new_artifacts]
                outputs["artifact_count"] = len(new_artifacts)
                artifact_store.record_audit(
                    run.id,
                    actor_agent_id=agent_id,
                    actor_node_id=node_id,
                    action="scan_end",
                    extra={"new_artifacts": len(new_artifacts),
                           "artifact_ids": [a.id for a in new_artifacts]},
                )
                # Update _meta.json with the resolved deliverable + finished_at
                try:
                    artifact_store.write_node_meta(run.id, node_id, {
                        "node_id": node_id,
                        "node_label": node.get("label", ""),
                        "agent_id": agent_id,
                        "agent_name": getattr(agent, "name", ""),
                        "task_id": task.id,
                        "started_at": task.created_at,
                        "finished_at": time.time(),
                        "deliverable": {
                            "rel_path": outputs.get("deliverable_relative", f"{node_id}/"),
                            "abs_path": outputs.get("deliverable", str(node_dir)),
                        },
                        "artifact_count": len(new_artifacts),
                    })
                except Exception:
                    pass
            except Exception as _pse:
                logger.warning("artifact post-scan failed for %s/%s: %s",
                               run.id, node_id, _pse)

        # EMPTY_DELIVERABLE check (spec 2026-05-02): even if the
        # LLM reported COMPLETED or success_when fired, if the agent
        # produced no real files we treat the node as FAILED. The
        # error code is grep-friendly so the UI can color it
        # specifically and the user knows to retry the agent (rather
        # than blame timeout / LLM).
        if node_dir is not None and not _has_real_deliverable(node_dir):
            raise RuntimeError(
                f"agent {agent_id} produced no deliverable in "
                f"shared/{node_id}/ (error_code: EMPTY_DELIVERABLE)"
            )

        return outputs
    finally:
        # Restore agent state regardless of outcome.
        if prior_working_dir is not None:
            try:
                agent.working_dir = prior_working_dir
            except Exception:
                pass
        if sandbox_extended:
            try:
                from . import sandbox as _sb
                pol = _sb.get_current_policy()
                if pol is not None and artifact_store is not None:
                    sd = str(artifact_store.shared_dir(run.id))
                    pol.allowed_dirs[:] = [d for d in pol.allowed_dirs if d != sd]
            except Exception:
                pass
        if canvas_ctx_id:
            # Restore prior context + drop the canvas bucket. We don't
            # want one entry per canvas run accumulating in
            # _messages_by_context (would bloat the agent.json on disk
            # over weeks of running). The conversation log is also
            # captured in the run's events.jsonl, so dropping is lossless
            # for audit purposes.
            try:
                with agent._lock:
                    agent._switch_context(prior_ctx_id)
                    agent._messages_by_context.pop(canvas_ctx_id, None)
            except Exception:
                pass


def _exec_tool(engine: WorkflowEngine, run: WorkflowRun,
               node: dict, config: dict) -> dict:
    """Tool node: invokes a granted skill via the registry.

    Required config:
      * ``tool_name`` — the skill id (validated by the canvas store)

    Optional config:
      * ``agent_id`` — which agent's grant context to invoke under
                       (the registry tracks per-agent grants).
      * ``inputs``   — dict of inputs forwarded to the skill.
    """
    if engine.hub is None:
        raise RuntimeError(
            "tool node requires hub to be wired into the engine"
        )

    tool_name = config.get("tool_name", "")
    agent_id = config.get("agent_id", "")
    inputs = config.get("inputs") or {}
    if not tool_name:
        raise ValueError("tool node missing config.tool_name")
    if not isinstance(inputs, dict):
        raise ValueError("tool node config.inputs must be a dict")

    reg = getattr(engine.hub, "skill_registry", None)
    if reg is None:
        raise RuntimeError("hub.skill_registry not available")

    result = reg.invoke(tool_name, agent_id, inputs)
    # Skills can return arbitrary shapes — wrap scalar/list returns
    # into a dict so downstream {{node.output}} substitution always
    # finds something.
    if isinstance(result, dict):
        out = dict(result)
        out.setdefault("output", result)
        return out
    return {"output": result}


def _exec_decision(engine: WorkflowEngine, run: WorkflowRun,
                   node: dict, config: dict) -> dict:
    """Decision node: evaluates ``config.condition`` against the run
    variables and returns the branch name to take.

    Required config:
      * ``condition`` — a Python boolean expression. Has access to:
        - ``vars["node_id.key"]`` — full vars dict
        - flattened identifiers: ``vars["n1.output"]`` is also exposed
          as ``n1_output`` (dots/dashes → underscores) for ergonomics
        - basic literals/builtins: True, False, None, int, str, float,
          len, bool

    Edges out of this node carry ``label`` = "yes" / "no" / etc; the
    driver's post-hook (``_skip_unchosen_branches``) marks the
    non-matching branches as SKIPPED.

    Trust note: this uses Python ``eval`` with restricted builtins.
    Workflow authors are admin-authenticated (canvas API requires
    admin auth) and already have arbitrary command execution via the
    bash tool, so the trust boundary is "admin = trusted code". The
    restrictions here prevent accidental footguns, not deliberate
    attacks.
    """
    expr = config.get("condition", "")
    if not expr or not isinstance(expr, str):
        raise ValueError("decision node missing config.condition")

    safe_locals: dict[str, Any] = {"vars": dict(run.vars)}
    for k, v in run.vars.items():
        flat = k.replace(".", "_").replace("-", "_")
        if flat.isidentifier() and flat not in safe_locals:
            safe_locals[flat] = v

    safe_globals = {
        "__builtins__": {
            "True": True, "False": False, "None": None,
            "int": int, "str": str, "float": float,
            "len": len, "bool": bool, "abs": abs,
            "min": min, "max": max,
        }
    }
    try:
        result = eval(expr, safe_globals, safe_locals)
    except Exception as e:
        raise RuntimeError(
            f"decision condition {expr!r} eval failed: "
            f"{type(e).__name__}: {e}"
        )

    branch = "yes" if bool(result) else "no"
    # Store raw_result as a primitive (str/int/bool/float). Anything
    # exotic (objects, lists) gets stringified so JSON serialization
    # of run state doesn't break.
    raw = result if isinstance(result, (int, str, bool, float)) else str(result)
    return {"branch": branch, "raw_result": raw}


def _exec_parallel(engine: WorkflowEngine, run: WorkflowRun,
                   node: dict, config: dict) -> dict:
    """Parallel node: no-op success.

    The DAG semantics already give us fan-out for free — once this
    node completes, every downstream node with this as its only
    upstream becomes ready, and the driver picks them up one at a
    time. The implicit "join" happens because any node that has
    multiple upstreams will wait for all of them via the existing
    ``_pick_ready`` deps check.

    True concurrency (running the fan-out branches on parallel
    threads) is a perf optimization, not a correctness requirement —
    deferred. Documented behavior: "parallel" is currently a
    visual/logical fan-out marker.
    """
    return {"fanned_out_at": time.time()}


_NODE_EXECUTORS: dict[str, Callable] = {
    "start":    _exec_start,
    "end":      _exec_end,
    "agent":    _exec_agent,
    "decision": _exec_decision,
    "parallel": _exec_parallel,
    # Note: "tool" removed 2026-05-02. Use agent + granted skills
    # instead — see canvas_workflows._migrate_tool_nodes_inplace.
    # _exec_tool function kept below as dead code in case the
    # migration needs to be reversed; can be deleted in a follow-up
    # after the migration has been in production for a release.
}


# ── Module-level singleton ──────────────────────────────────────────────


_ENGINE: WorkflowEngine | None = None
_ENGINE_LOCK = threading.Lock()


def init_engine(store_root: str | Path, hub: Any = None) -> WorkflowEngine:
    """Initialize and stash the singleton engine. Idempotent."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            store = RunStore(store_root)
            _ENGINE = WorkflowEngine(store, hub=hub)
        elif hub is not None and _ENGINE.hub is None:
            _ENGINE.hub = hub
    return _ENGINE


def get_engine() -> WorkflowEngine | None:
    return _ENGINE
