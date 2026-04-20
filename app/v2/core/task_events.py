"""
TaskEventBus — PRD §6.4 / §9.

Contract:
    - Event type is a closed set (Literal); freely adding types is discouraged.
    - Critical events (task lifecycle + user-facing clarification + phase_error)
      persist synchronously and dispatch immediately.
    - Normal events buffer with BATCH_SIZE=50 / BATCH_FLUSH_MS=200 and are
      dispatched to in-process subscribers immediately (at-least-once to DB
      via background flusher).
    - Subscribers MUST be non-blocking; handler exceptions are swallowed.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, TYPE_CHECKING

from .task import TaskPhase

if TYPE_CHECKING:
    from .task_store import TaskStore


EventType = Literal[
    "task_submitted",
    "phase_enter",
    "phase_exit",
    "phase_retry",
    "phase_error",
    "intake_slots_filled",
    "intake_clarification",
    "plan_draft",
    "plan_approved",
    "step_enter",
    "step_exit",
    "tool_call",
    "tool_result",
    "progress",
    "artifact_created",
    "verify_check",
    "verify_retry",
    "lesson_recorded",
    "task_completed",
    "task_failed",
    "task_paused",
    "task_resumed",
]


@dataclass
class TaskEvent:
    task_id: str
    ts: float
    phase: str          # TaskPhase.value
    type: str           # EventType
    payload: dict


class TaskEventBus:
    """Event bus with buffered batch persistence (PRD §6.4)."""

    BATCH_SIZE = 50
    BATCH_FLUSH_MS = 200
    CRITICAL_EVENT_TYPES: frozenset[str] = frozenset({
        "task_submitted",
        "task_completed",
        "task_failed",
        "task_paused",
        "task_resumed",
        "phase_error",
        "intake_clarification",
    })

    def __init__(self, store: "TaskStore"):
        self.store = store
        # task_id -> list of handlers
        self._subscribers: dict[str, list[Callable[[TaskEvent], None]]] = {}
        self._sub_lock = threading.Lock()
        self._buf: list[TaskEvent] = []
        self._buf_lock = threading.Lock()
        self._stopped = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="TaskEventBus-Flusher",
        )
        self._flush_thread.start()

    # ── publication ────────────────────────────────────────────────────

    def publish(
        self,
        task_id: str,
        phase,
        event_type: str,
        payload: dict,
    ) -> None:
        phase_val = phase.value if isinstance(phase, TaskPhase) else str(phase)
        evt = TaskEvent(
            task_id=task_id,
            ts=time.time(),
            phase=phase_val,
            type=event_type,
            payload=dict(payload or {}),
        )

        if event_type in self.CRITICAL_EVENT_TYPES:
            # Critical path: sync-persist then dispatch.
            try:
                self.store.append_event(evt)
            except Exception:
                # Retry once via buffer if SQLite is momentarily busy.
                with self._buf_lock:
                    self._buf.append(evt)
            self._dispatch(evt)
            return

        # Normal path: buffer, dispatch immediately to subscribers.
        should_flush = False
        with self._buf_lock:
            self._buf.append(evt)
            if len(self._buf) >= self.BATCH_SIZE:
                should_flush = True
        self._dispatch(evt)
        if should_flush:
            self._flush_now()

    # ── subscription ───────────────────────────────────────────────────

    def subscribe(
        self,
        task_id: str,
        handler: Callable[[TaskEvent], None],
    ) -> Callable[[], None]:
        """Register a handler for a task's events. Returns an unsubscribe fn."""
        with self._sub_lock:
            self._subscribers.setdefault(task_id, []).append(handler)

        def _unsubscribe() -> None:
            with self._sub_lock:
                lst = self._subscribers.get(task_id, [])
                try:
                    lst.remove(handler)
                except ValueError:
                    pass
                if not lst:
                    self._subscribers.pop(task_id, None)

        return _unsubscribe

    def replay(self, task_id: str, since_ts: float = 0.0) -> list[TaskEvent]:
        """Load persisted events for SSE resume (Last-Event-ID / ?since=ts)."""
        return self.store.load_events(task_id, since_ts=since_ts)

    # ── lifecycle ──────────────────────────────────────────────────────

    def flush_and_close(self, task_id: str | None = None) -> None:
        """Force flush of all buffered events. Call on task terminal state."""
        self._flush_now()

    def stop(self) -> None:
        """Stop the background flusher. Final flush is attempted."""
        self._stopped.set()
        self._flush_now()

    # ── internals ──────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        while not self._stopped.is_set():
            time.sleep(self.BATCH_FLUSH_MS / 1000.0)
            try:
                self._flush_now()
            except Exception:
                # Flusher must never die.
                pass

    def _flush_now(self) -> None:
        with self._buf_lock:
            if not self._buf:
                return
            batch, self._buf = self._buf, []
        try:
            self.store.append_events_batch(batch)
        except Exception:
            # Re-enqueue at the front to preserve order & at-least-once.
            with self._buf_lock:
                self._buf[:0] = batch

    def _dispatch(self, evt: TaskEvent) -> None:
        with self._sub_lock:
            handlers = list(self._subscribers.get(evt.task_id, []))
        for h in handlers:
            try:
                h(evt)
            except Exception:
                # Subscribers are best-effort; persistence is authoritative.
                pass


__all__ = ["TaskEvent", "TaskEventBus", "EventType"]
