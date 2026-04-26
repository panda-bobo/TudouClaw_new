"""Serializable approval state for cross-process recovery.

Why this exists
===============
``app.core.approval.ApprovalGate`` already serialises individual
``ApprovalRequest`` objects (``to_dict`` / ``from_dict``), but the gate
itself keeps its pending queue in-memory and uses ``threading.Event`` for
caller wake-up. That works for a single-process server: when the process
restarts mid-approval, the request is lost; if the approval UI runs in a
sibling worker, the in-process Event doesn't fire across the pipe.

Borrowed pattern
----------------
openai-agents-python's ``RunState.to_json`` / ``from_json`` (see
``examples/agent_patterns/human_in_the_loop.py``) demonstrates the
pattern: when execution is interrupted, dump the full state to disk;
the resuming process reads it back, applies decisions, then resumes.

We adapt that here for ApprovalGate. This module is **additive** — it
doesn't change ``ApprovalGate``'s in-memory protocol; it only adds:

  * ``ApprovalStateStore`` — JSON-on-disk pending queue.
  * ``snapshot_pending`` / ``restore_pending`` — full-gate dump/load.
  * ``wait_for_decision_file`` — disk-poll wake-up that works across
    processes (the in-process Event still works as before).

A typical disaster-recovery flow:

    # process A: start request, persist before blocking
    req = gate.request(..., blocking=False)
    store.save_pending(req)
    decided = wait_for_decision_file(store, req.id, timeout=600)
    # process A may die here. process B (admin UI) writes decision:
    store.record_decision(req.id, approved=True, decided_by="alice")
    # process A (or any restarted worker) reads disk, resumes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("tudouclaw.v2.approval_state")


# ─────────────────────────────────────────────────────────────────────
# Disk-backed pending queue
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _StoreFiles:
    """Resolved file paths inside the store root."""
    pending_dir: str       # one ``<id>.json`` per pending request
    decisions_dir: str     # one ``<id>.json`` per recorded decision
    history_log: str       # append-only NDJSON of completed requests


class ApprovalStateStore:
    """File-system store for ``ApprovalRequest`` snapshots.

    Layout under ``root_dir`` (created on demand):

        approvals/
          pending/<id>.json     — outstanding requests
          decisions/<id>.json   — decisions written by another process
          history.ndjson        — completed-request audit trail

    All writes use atomic rename. Reads are best-effort — a partial JSON
    is treated as "not yet there" and skipped.
    """

    def __init__(self, root_dir: str):
        self._root = root_dir
        self._files = _StoreFiles(
            pending_dir=os.path.join(root_dir, "pending"),
            decisions_dir=os.path.join(root_dir, "decisions"),
            history_log=os.path.join(root_dir, "history.ndjson"),
        )
        os.makedirs(self._files.pending_dir, exist_ok=True)
        os.makedirs(self._files.decisions_dir, exist_ok=True)
        self._lock = threading.Lock()

    # ── pending ────────────────────────────────────────────────────
    def save_pending(self, req_dict: dict) -> str:
        """Persist a pending ``ApprovalRequest`` (as ``to_dict()`` output).

        Returns the on-disk path. Atomic write: tmp + rename.
        """
        rid = req_dict.get("id")
        if not rid:
            raise ValueError("save_pending: req_dict missing 'id'")
        path = os.path.join(self._files.pending_dir, f"{rid}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def load_pending(self, approval_id: str) -> Optional[dict]:
        path = os.path.join(self._files.pending_dir, f"{approval_id}.json")
        return _load_json(path)

    def list_pending(self) -> list[dict]:
        out: list[dict] = []
        if not os.path.isdir(self._files.pending_dir):
            return out
        for fn in sorted(os.listdir(self._files.pending_dir)):
            if not fn.endswith(".json"):
                continue
            d = _load_json(os.path.join(self._files.pending_dir, fn))
            if d:
                out.append(d)
        return out

    def remove_pending(self, approval_id: str) -> bool:
        path = os.path.join(self._files.pending_dir, f"{approval_id}.json")
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            logger.warning("remove_pending(%s) failed: %s", approval_id, e)
            return False

    # ── decisions (cross-process wake-up channel) ──────────────────
    def record_decision(self, approval_id: str, *, approved: bool,
                        decided_by: str = "admin",
                        reason: str = "") -> str:
        """Write a decision file. Any process polling this approval_id
        will pick it up via ``wait_for_decision_file`` / ``read_decision``.
        """
        d = {
            "approval_id": approval_id,
            "approved": bool(approved),
            "decided_by": decided_by or "admin",
            "reason": reason or ("Approved" if approved else "Denied"),
            "decided_at": time.time(),
        }
        path = os.path.join(self._files.decisions_dir, f"{approval_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def read_decision(self, approval_id: str) -> Optional[dict]:
        path = os.path.join(self._files.decisions_dir, f"{approval_id}.json")
        return _load_json(path)

    def clear_decision(self, approval_id: str) -> bool:
        path = os.path.join(self._files.decisions_dir, f"{approval_id}.json")
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    # ── history (audit trail) ──────────────────────────────────────
    def append_history(self, req_dict: dict) -> None:
        with self._lock:
            try:
                with open(self._files.history_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(req_dict, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("append_history failed: %s", e)


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        # Treat partial / corrupt as "not yet there" rather than crashing
        logger.debug("load_json(%s) skipped: %s", path, e)
        return None


# ─────────────────────────────────────────────────────────────────────
# Cross-process wake-up
# ─────────────────────────────────────────────────────────────────────


def wait_for_decision_file(store: ApprovalStateStore, approval_id: str,
                           *, timeout: float = 300.0,
                           poll_interval: float = 1.0) -> Optional[dict]:
    """Poll the decisions dir until a file for ``approval_id`` appears.

    Returns the decision dict (``{approved, decided_by, reason,
    decided_at}``), or ``None`` on timeout.

    Use this from a worker process that doesn't share memory with the
    process that calls ``ApprovalStateStore.record_decision``.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = store.read_decision(approval_id)
        if d is not None:
            return d
        # Sleep but keep responsiveness reasonable
        remaining = deadline - time.time()
        time.sleep(min(poll_interval, max(0.05, remaining)))
    return None


# ─────────────────────────────────────────────────────────────────────
# Whole-gate snapshot / restore (RunState-style)
# ─────────────────────────────────────────────────────────────────────


def snapshot_pending(gate, store: ApprovalStateStore) -> int:
    """Persist every pending request from a live ``ApprovalGate`` to disk.

    Returns the number of pending requests written. Existing on-disk
    pending entries for the same id are overwritten (last-write-wins).

    The gate's in-memory state is unchanged — this is a one-way export.
    """
    count = 0
    # ApprovalGate doesn't expose _pending directly; we go through
    # list_pending() (returns dicts) — same shape ``save_pending`` wants.
    pendings = gate.list_pending() if hasattr(gate, "list_pending") else []
    for d in pendings:
        try:
            store.save_pending(d)
            count += 1
        except (OSError, ValueError) as e:
            logger.warning("snapshot_pending: skip %s: %s",
                           d.get("id"), e)
    return count


def restore_pending(gate, store: ApprovalStateStore) -> int:
    """Re-inject on-disk pending requests back into a fresh ``ApprovalGate``.

    Walks the ``pending/`` dir, parses each JSON via
    ``ApprovalRequest.from_dict``, and registers it in the gate's
    in-memory ``_pending`` map so callers can decide via the normal
    ``gate.decide(approval_id, ...)`` API.

    Note: the original caller's ``threading.Event`` is gone (it lived
    in the dead process). New callers in the restored process should
    use ``wait_for_decision_file`` instead, or implement their own
    waiting strategy.

    Returns the number of requests rehydrated.
    """
    try:
        from ...core.approval import ApprovalRequest
    except ImportError as e:
        logger.error("restore_pending: cannot import ApprovalRequest: %s", e)
        return 0

    count = 0
    for d in store.list_pending():
        try:
            req = ApprovalRequest.from_dict(d)
        except (KeyError, ValueError) as e:
            logger.warning("restore_pending: bad record id=%s: %s",
                           d.get("id"), e)
            continue
        # Direct injection. ApprovalGate doesn't expose a public
        # "register existing pending" API, so we touch the private map
        # under its lock — the alternative would be re-issuing the
        # request, which would mint a new id and break correlation.
        try:
            with gate._lock:
                gate._pending[req.id] = req
            count += 1
        except AttributeError:
            logger.error("restore_pending: gate has no _pending/_lock — "
                         "incompatible ApprovalGate version")
            return count
    return count


__all__ = [
    "ApprovalStateStore",
    "wait_for_decision_file",
    "snapshot_pending",
    "restore_pending",
]
