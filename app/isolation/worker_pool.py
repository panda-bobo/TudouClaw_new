"""
app.isolation.worker_pool — Main-process side of the worker channel.

This module is split into three concerns that plug together:

1. ``WorkerChannel`` — frame-level bookkeeping over any Transport.
   Owns the reader thread, pending-call futures, notify fire-and-
   forget, and graceful close. Knows nothing about subprocesses
   or sockets.

2. ``LocalWorkerLauncher`` — boots a local subprocess worker via
   ``python -m app.agent_worker``, wires its stdin/stdout into a
   ``StdioTransport``, and drains stderr into the main-process log.
   Returns a ``WorkerProcess`` facade the caller holds on to.

3. ``WorkerProcess`` — small facade that combines a launcher-owned
   subprocess (or ``None`` for remote workers) with a
   ``WorkerChannel``. The external API (``call``, ``notify``,
   ``shutdown``, ``is_alive``) is the same regardless of whether
   the worker is local or remote.

4. ``WorkerPool`` — hub-wide registry mapping ``agent_id`` →
   ``WorkerProcess``. Lazily spawns on first request, reaps idle
   workers after ``idle_timeout``. In Layer 1a the pool only knows
   how to launch **local** workers; Layer 1b will teach it to
   consult a ``Scheduler`` and pick between local and remote
   launchers.

All the protocol / framing layer is delegated to ``transport.py``
and ``protocol.py``, so the plumbing here is pure orchestration.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .protocol import (
    DEFAULT_CHAN_ID,
    Frame,
    FrameKind,
    ProtocolError,
)
from .transport import StdioTransport, Transport, TransportError


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WorkerError(Exception):
    """Base class for worker-side errors raised in the main process."""


class WorkerTimeoutError(WorkerError):
    """Raised when a call exceeds its per-call timeout."""


class WorkerCrashedError(WorkerError):
    """Raised when the worker process died while a call was in flight."""


class WorkerProtocolError(WorkerError):
    """Raised on framing corruption — worker is assumed to be dead."""


# ---------------------------------------------------------------------------
# Pending-call bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class _PendingCall:
    id: str
    method: str
    event: threading.Event = field(default_factory=threading.Event)
    frame: Optional[Frame] = None
    error: Optional[Exception] = None


# ---------------------------------------------------------------------------
# WorkerChannel — transport-agnostic frame bookkeeping
# ---------------------------------------------------------------------------

class WorkerChannel:
    """Owns one worker's end of a Transport and turns frames into
    blocking ``call`` / fire-and-forget ``notify`` RPCs.

    The channel has **no knowledge of how the worker was spawned**.
    A local subprocess and a remote-over-TLS worker look identical
    through this API. That lets LocalWorkerLauncher and (later)
    RemoteWorkerLauncher share all of the bookkeeping.
    """

    def __init__(
        self,
        agent_id: str,
        transport: Transport,
        *,
        chan_id: int = DEFAULT_CHAN_ID,
        event_handler: Optional[Callable[[Frame], None]] = None,
        gate_handler: Optional[Callable[[Frame], Frame]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.agent_id = agent_id
        self._transport = transport
        self._chan_id = chan_id
        self._event_handler = event_handler
        self._gate_handler = gate_handler
        self._log = logger or (lambda m: None)

        self._pending: Dict[str, _PendingCall] = {}
        self._pending_lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self._last_activity: float = time.time()
        self._ready_event = threading.Event()

    # ------------- lifecycle -------------

    def start_reader(self) -> None:
        """Start the reader thread. Idempotent."""
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"worker-channel-{self.agent_id}",
            daemon=True,
        )
        self._reader.start()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.close()
        except Exception as e:
            self._log(f"[channel:{self.agent_id}] transport close failed: {e}")
        self._cleanup_pending(WorkerCrashedError("channel closed"))

    # ------------- reader loop -------------

    def _reader_loop(self) -> None:
        while not self._closed:
            try:
                chan_id, frame = self._transport.recv()
            except ProtocolError as e:
                self._log(f"[channel:{self.agent_id}] protocol error: {e}")
                self._cleanup_pending(
                    WorkerProtocolError(f"framing broken: {e}"))
                return
            except TransportError as e:
                if not self._closed:
                    self._log(f"[channel:{self.agent_id}] transport EOF: {e}")
                self._cleanup_pending(
                    WorkerCrashedError(f"transport EOF: {e}"))
                return
            except Exception as e:
                if not self._closed:
                    self._log(f"[channel:{self.agent_id}] reader crashed: {e}")
                self._cleanup_pending(
                    WorkerCrashedError(f"reader crashed: {e}"))
                return
            self._last_activity = time.time()
            # In exclusive-transport mode chan_id should always match
            # ours; defensively log any mismatch but still process.
            if chan_id != self._chan_id:
                self._log(
                    f"[channel:{self.agent_id}] chan_id mismatch: "
                    f"got {chan_id}, expected {self._chan_id}")
            self._on_frame(frame)

    def _on_frame(self, frame: Frame) -> None:
        if frame.kind in (FrameKind.RESPONSE, FrameKind.GATE_RESP):
            pending = self._pending_pop(frame.id or "")
            if pending is None:
                self._log(
                    f"[channel:{self.agent_id}] orphan response id={frame.id}")
                return
            pending.frame = frame
            pending.event.set()
            return
        if frame.kind == FrameKind.EVENT:
            if frame.kind2 == "ready":
                self._ready_event.set()
            if self._event_handler is not None:
                try:
                    self._event_handler(frame)
                except Exception as e:
                    self._log(
                        f"[channel:{self.agent_id}] event_handler crash: {e}")
            return
        if frame.kind == FrameKind.GATE:
            reply: Optional[Frame] = None
            if self._gate_handler is not None:
                try:
                    reply = self._gate_handler(frame)
                except Exception as e:
                    reply = Frame.gate_resp_err(
                        frame.id or "", "gate_handler_crash", str(e))
            if reply is None:
                reply = Frame.gate_resp_err(
                    frame.id or "", "no_handler",
                    "main process has no gate_handler installed")
            try:
                self._transport.send(reply, self._chan_id)
            except TransportError as e:
                self._log(
                    f"[channel:{self.agent_id}] gate reply send failed: {e}")
            return
        self._log(
            f"[channel:{self.agent_id}] ignoring unexpected kind={frame.kind}")

    # ------------- send / call / notify -------------

    def _next_id(self) -> str:
        return f"r-{next(self._id_counter)}"

    def _pending_push(self, pending: _PendingCall) -> None:
        with self._pending_lock:
            self._pending[pending.id] = pending

    def _pending_pop(self, id: str) -> Optional[_PendingCall]:
        with self._pending_lock:
            return self._pending.pop(id, None)

    def _cleanup_pending(self, err: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for p in pending:
            p.error = err
            p.event.set()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             *, timeout: float = 60.0) -> Any:
        """Send a request and block until the worker replies."""
        if self._closed:
            raise WorkerError(f"channel {self.agent_id} closed")
        req_id = self._next_id()
        pending = _PendingCall(id=req_id, method=method)
        self._pending_push(pending)
        try:
            self._transport.send(
                Frame.request(req_id, method, params or {}), self._chan_id)
        except TransportError as e:
            self._pending_pop(req_id)
            raise WorkerCrashedError(f"send failed: {e}") from e

        if not pending.event.wait(timeout):
            self._pending_pop(req_id)
            raise WorkerTimeoutError(
                f"worker {self.agent_id}: call {method!r} timed out after "
                f"{timeout}s")
        if pending.error is not None:
            raise pending.error
        frame = pending.frame
        assert frame is not None
        if frame.ok:
            return frame.result
        err = frame.error or {"type": "unknown", "message": ""}
        raise WorkerError(
            f"worker {self.agent_id}: {err.get('type', 'error')}: "
            f"{err.get('message', '')}")

    def notify(self, kind2: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Fire a notify frame, no reply expected."""
        if self._closed:
            return
        try:
            self._transport.send(
                Frame.notify(kind2, payload or {}), self._chan_id)
        except TransportError as e:
            self._log(f"[channel:{self.agent_id}] notify failed: {e}")

    def idle_seconds(self) -> float:
        return time.time() - self._last_activity


# ---------------------------------------------------------------------------
# LocalWorkerLauncher — spawn a subprocess and wire up a channel
# ---------------------------------------------------------------------------

class LocalWorkerLauncher:
    """Knows how to spawn a subprocess worker on the current host.

    Layer 1b will add ``RemoteWorkerLauncher`` which satisfies the
    same ``launch(agent_id, boot_config) -> WorkerProcess`` duck-
    type but uses a SocketTransport instead of stdio pipes.
    """

    def __init__(
        self,
        *,
        python_executable: Optional[str] = None,
        project_root: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._python = python_executable or sys.executable
        # Single source of truth for project root. Don't recompute dirname
        # hops here — that's exactly the class of bug runtime_paths exists
        # to fix.
        if project_root:
            self._project_root = project_root
        else:
            from ..runtime_paths import get_project_root
            self._project_root = get_project_root()
        self._log = logger or (lambda m: None)

    def launch(
        self,
        agent_id: str,
        boot_config: Dict[str, Any],
        *,
        event_handler: Optional[Callable[[Frame], None]] = None,
        gate_handler: Optional[Callable[[Frame], Frame]] = None,
        boot_timeout: float = 10.0,
    ) -> "WorkerProcess":
        # 1. Write boot config to a tempfile — avoids argv length
        # limits and leaking params into /proc/<pid>/cmdline.
        fd, path = tempfile.mkstemp(prefix=f"tudou-boot-{agent_id}-",
                                    suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(boot_config, f, ensure_ascii=False)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise

        # 2. Build env via runtime_paths — injects PROJECT_ROOT onto
        # PYTHONPATH and TUDOU_PROJECT_ROOT uniformly. Credentials stay
        # scrubbed: worker boot_config carries any keys the agent needs,
        # the ambient process env is not forwarded.
        from ..runtime_paths import build_subprocess_env
        env = build_subprocess_env()

        # Phase 2: in full_agent mode, keep LLM API keys (agent needs
        # them for LLM calls); only scrub in tool_sandbox mode.
        if boot_config.get("mode") != "full_agent":
            for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                      "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                      "GITHUB_TOKEN", "GH_TOKEN"):
                env.pop(k, None)

        # Phase 2: build preexec_fn for UID namespace isolation
        preexec_fn = None
        if boot_config.get("uid_isolation"):
            try:
                from .uid_manager import get_uid_manager
                uid_mgr = get_uid_manager()
                shared_dirs = []
                if boot_config.get("shared_workspace"):
                    shared_dirs.append(boot_config["shared_workspace"])
                preexec_fn = uid_mgr.build_preexec_fn(
                    agent_id=agent_id,
                    work_dir=boot_config.get("work_dir", ""),
                    shared_dirs=shared_dirs,
                )
                if preexec_fn:
                    self._log(f"[launcher:{agent_id}] UID namespace isolation enabled")
            except Exception as e:
                self._log(f"[launcher:{agent_id}] UID isolation setup failed: {e}")

        cmd = [
            self._python, "-u",
            "-m", "app.agent_worker",
            "--boot-file", path,
            "--boot-json", "{}",
        ]
        self._log(f"[launcher:{agent_id}] spawn cmd={cmd}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=boot_config.get("work_dir") or None,
                preexec_fn=preexec_fn,
                bufsize=0,
                close_fds=(os.name != "nt"),
            )
        except Exception as e:
            try:
                os.unlink(path)
            except Exception:
                pass
            raise WorkerError(f"failed to spawn worker: {e}") from e

        # 3. Wrap its stdio in a StdioTransport and build a channel.
        transport = StdioTransport(
            stdin_w=proc.stdin,
            stdout_r=proc.stdout,
            label=f"local:{agent_id}",
        )
        channel = WorkerChannel(
            agent_id=agent_id,
            transport=transport,
            chan_id=DEFAULT_CHAN_ID,
            event_handler=event_handler,
            gate_handler=gate_handler,
            logger=self._log,
        )

        wp = WorkerProcess(
            agent_id=agent_id,
            channel=channel,
            subprocess=proc,
            boot_file=path,
            logger=self._log,
        )
        channel.start_reader()
        wp._start_stderr_reader()

        # 4. Wait for the worker's ready event before returning.
        if not channel.wait_ready(boot_timeout):
            wp._hard_kill()
            raise WorkerError(
                f"worker {agent_id} failed to become ready in "
                f"{boot_timeout}s")
        wp._started_at = time.time()
        return wp


# ---------------------------------------------------------------------------
# WorkerProcess — public facade over a channel + optional subprocess
# ---------------------------------------------------------------------------

class WorkerProcess:
    """A live worker (local subprocess or remote NodeAgent-hosted)
    plus its main-side channel. Same external API in both cases."""

    def __init__(
        self,
        *,
        agent_id: str,
        channel: WorkerChannel,
        subprocess: Optional[subprocess.Popen] = None,
        boot_file: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.agent_id = agent_id
        self._channel = channel
        self._proc = subprocess
        self._boot_file = boot_file
        self._log = logger or (lambda m: None)
        self._closed = False
        self._started_at: float = 0.0
        self._stderr_reader: Optional[threading.Thread] = None

    # ------------- external API -------------

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             *, timeout: float = 60.0) -> Any:
        return self._channel.call(method, params, timeout=timeout)

    def notify(self, kind2: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._channel.notify(kind2, payload or {})

    def is_alive(self) -> bool:
        if self._closed:
            return False
        if self._proc is not None:
            return self._proc.poll() is None
        # Remote workers: alive while their channel is open.
        return not self._channel._closed

    def idle_seconds(self) -> float:
        return self._channel.idle_seconds()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        try:
            self.call("shutdown", {}, timeout=timeout)
        except Exception as e:
            self._log(f"[worker:{self.agent_id}] shutdown call failed: {e}")
        self._closed = True
        if self._proc is not None:
            try:
                self._proc.wait(timeout=timeout)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._channel.close()
        self._cleanup_boot_file()

    def _hard_kill(self) -> None:
        self._closed = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._channel.close()
        self._cleanup_boot_file()

    def _cleanup_boot_file(self) -> None:
        if self._boot_file:
            try:
                os.unlink(self._boot_file)
            except Exception:
                pass
            self._boot_file = None

    def _start_stderr_reader(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop,
            name=f"worker-stderr-{self.agent_id}",
            daemon=True,
        )
        self._stderr_reader.start()

    def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in iter(self._proc.stderr.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                line = repr(raw)
            if line:
                self._log(f"[worker:{self.agent_id}][stderr] {line}")


# ---------------------------------------------------------------------------
# WorkerPool — per-hub registry of live WorkerProcess instances
# ---------------------------------------------------------------------------

class WorkerPool:
    """Per-hub registry of live WorkerProcess instances.

    Layer 1a: only knows how to launch local workers via
    ``LocalWorkerLauncher``.

    Layer 1b will add a Scheduler that, given an ``agent_id`` and
    its requested capabilities, returns either the local launcher
    or a ``RemoteWorkerLauncher`` bound to a specific node.
    """

    def __init__(
        self,
        *,
        launcher: Optional[LocalWorkerLauncher] = None,
        idle_timeout: float = 600.0,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._launcher = launcher or LocalWorkerLauncher(logger=logger)
        self._workers: Dict[str, WorkerProcess] = {}
        self._lock = threading.RLock()
        self._idle_timeout = float(idle_timeout)
        self._log = logger or (lambda m: None)
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start_reaper(self) -> None:
        if self._reaper is not None:
            return
        self._reaper = threading.Thread(
            target=self._reap_loop,
            name="worker-pool-reaper",
            daemon=True,
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(30.0):
            with self._lock:
                dead = [aid for aid, w in self._workers.items()
                        if not w.is_alive()
                        or w.idle_seconds() > self._idle_timeout]
            for aid in dead:
                self._log(f"[worker-pool] reaping idle worker {aid}")
                self.stop_worker(aid)

    def get_or_spawn(
        self,
        agent_id: str,
        boot_config: Dict[str, Any],
        *,
        event_handler: Optional[Callable[[Frame], None]] = None,
        gate_handler: Optional[Callable[[Frame], Frame]] = None,
        boot_timeout: float = 10.0,
    ) -> WorkerProcess:
        with self._lock:
            w = self._workers.get(agent_id)
            if w is not None and w.is_alive():
                return w
            if w is not None:
                self._log(f"[worker-pool] replacing dead worker {agent_id}")
                self._workers.pop(agent_id, None)
            w = self._launcher.launch(
                agent_id=agent_id,
                boot_config=boot_config,
                event_handler=event_handler,
                gate_handler=gate_handler,
                boot_timeout=boot_timeout,
            )
            self._workers[agent_id] = w
            return w

    def get(self, agent_id: str) -> Optional[WorkerProcess]:
        with self._lock:
            w = self._workers.get(agent_id)
            if w and w.is_alive():
                return w
            return None

    def stop_worker(self, agent_id: str) -> None:
        with self._lock:
            w = self._workers.pop(agent_id, None)
        if w is not None:
            try:
                w.shutdown()
            except Exception as e:
                self._log(f"[worker-pool] stop {agent_id} failed: {e}")

    def shutdown_all(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            try:
                w.shutdown()
            except Exception as e:
                self._log(f"[worker-pool] shutdown {w.agent_id} failed: {e}")

    def notify_agent(self, agent_id: str, kind2: str,
                     payload: Optional[Dict[str, Any]] = None) -> None:
        w = self.get(agent_id)
        if w is None:
            return
        w.notify(kind2, payload or {})
