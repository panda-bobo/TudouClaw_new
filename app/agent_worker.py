"""
app.agent_worker — Per-agent isolated tool-execution subprocess.

Launched by the main process via ``python -m app.agent_worker``. The
main process passes configuration on the command line and then
communicates with the worker over a length-prefixed JSON frame
protocol carried by stdin / stdout (see app.isolation.protocol).

Responsibilities
----------------

1. **Jail setup** — before importing any tool code, the worker:
   - chdir's to ``work_dir``
   - rewrites ``HOME``, ``USERPROFILE``, ``TMPDIR``, ``TMP``, ``TEMP``,
     ``PWD`` to point inside ``work_dir`` (so downstream tools that
     honor these env vars automatically land in the jail)
   - scrubs sensitive credentials (AWS_*, GOOGLE_*, GITHUB_TOKEN,
     ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)
   - installs a SandboxPolicy as the thread-local default, rooted at
     ``work_dir`` and allowing the authorized workspaces passed in
     the boot config
   - installs Python API hooks (app.agent_worker_hooks) that trap
     write-class filesystem calls whose target escapes the jail

2. **Main loop** — reads one frame at a time from stdin. For each
   ``req`` frame the worker dispatches on ``method``:

   - ``ping``           → health check, returns {"pong": True, ...}
   - ``tool_call``      → execute a registered tool by name
   - ``shutdown``       → reply and exit the main loop cleanly

   Unknown methods return a structured error (type=``unknown_method``)
   but the worker keeps running.

3. **Capability updates** — ``notify`` frames are fire-and-forget
   pushes from main (``kind2`` ∈ {capability_update, policy_update,
   mcp_update, skill_update, ping}). They never generate a reply.
   Subsequent tool_call invocations see the updated capability set.

4. **Gatekeeper requests** — when the worker needs to do something
   that crosses its jail boundary (a cross-dir operation, unknown
   bash command, pip install...), it can send a ``gate`` frame to
   the main process and block on the matching ``gate_resp``. This
   scaffolding is in place for later layers; Layer 1 only uses it
   for the "unknown bash command" flow if the command cannot be
   statically classified.

Crash contract
--------------

The worker is expected to run until main tells it to shut down.
Uncaught exceptions in the main loop are fatal and cause the worker
to exit with a non-zero status; the main process's WorkerPool will
notice and respawn a fresh worker. Exceptions raised *inside* a
``tool_call`` handler are caught and turned into a ``resp`` frame
with ``ok=false`` so one bad call does not kill the worker.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

# IMPORTANT: we intentionally defer importing app.tools / app.sandbox
# until *after* the env has been scrubbed and cwd has been rewritten,
# so any module-level code in those packages sees the jailed
# environment and not the main process's real one.


# ---------------------------------------------------------------------------
# Credential scrubbing
# ---------------------------------------------------------------------------

_CREDENTIAL_PREFIXES = (
    "AWS_", "AZURE_", "GCP_", "GOOGLE_", "GOOGLE_APPLICATION_",
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
    "NPM_TOKEN", "PYPI_TOKEN", "DOCKER_",
    "KUBE", "KUBECONFIG",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_ORG",
    "CLAUDE_", "COHERE_", "HF_TOKEN", "HUGGINGFACE_",
)
_CREDENTIAL_EXACT = {
    "SUDO_PASSWORD", "SUDO_ASKPASS",
    "LD_PRELOAD", "LD_LIBRARY_PATH",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "NETRC", "PGPASSWORD", "MYSQL_PWD",
}


def _scrub_env(env: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of *env* with credentials and dangerous vars stripped."""
    out = dict(env)
    for k in list(out.keys()):
        if k in _CREDENTIAL_EXACT:
            out.pop(k, None)
            continue
        for prefix in _CREDENTIAL_PREFIXES:
            if k.startswith(prefix):
                out.pop(k, None)
                break
    return out


# ---------------------------------------------------------------------------
# Jail setup
# ---------------------------------------------------------------------------

def _setup_jail(work_dir: str, *, full_agent: bool = False) -> Path:
    """Pin cwd / HOME / TMPDIR / PWD to the work_dir.

    All of these are set BEFORE app.tools / app.sandbox are imported
    so any module that caches a home-dir or tmp-dir at import time
    gets the jailed one.

    When *full_agent* is True the worker hosts a complete Agent (including
    LLM calls), so LLM API keys must survive — only non-LLM credentials
    are scrubbed.
    """
    root = Path(work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    config_dir = root / ".config"
    cache_dir = root / ".cache"
    data_dir = root / ".local" / "share"
    state_dir = root / ".local" / "state"
    for d in (config_dir, cache_dir, data_dir, state_dir):
        d.mkdir(parents=True, exist_ok=True)

    if full_agent:
        # In full_agent mode, keep LLM credentials — only scrub dangerous
        # vars that could escape the jail or escalate privileges.
        for k in list(_CREDENTIAL_EXACT):
            os.environ.pop(k, None)
    else:
        # Scrub credentials first.
        scrubbed = _scrub_env(os.environ)
        os.environ.clear()
        os.environ.update(scrubbed)

    # Pin filesystem-relevant env vars to the jail.
    os.environ["HOME"] = str(root)
    os.environ["USERPROFILE"] = str(root)          # Windows home
    os.environ["PWD"] = str(root)
    os.environ["TMPDIR"] = str(tmp)
    os.environ["TMP"] = str(tmp)
    os.environ["TEMP"] = str(tmp)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    os.environ["XDG_STATE_HOME"] = str(state_dir)
    # Mark the worker so downstream code can self-check.
    os.environ["TUDOU_WORKER"] = "1"
    os.environ["TUDOU_WORKER_ROOT"] = str(root)

    try:
        os.chdir(root)
    except Exception as e:
        _boot_fail(f"chdir to work_dir failed: {e}")

    return root


def _boot_fail(msg: str) -> None:
    """Write a single fatal error to stderr and exit non-zero."""
    sys.stderr.write(f"[agent_worker] fatal: {msg}\n")
    sys.stderr.flush()
    sys.exit(2)


# ---------------------------------------------------------------------------
# Binary stdio
# ---------------------------------------------------------------------------

# We stash the real stdout binary buffer at module load so _redirect
# can replace sys.stdout safely without losing the frame channel.
_REAL_STDIN_BUFFER = (sys.stdin.buffer if hasattr(sys.stdin, "buffer")
                      else sys.stdin)
_REAL_STDOUT_BUFFER = (sys.stdout.buffer if hasattr(sys.stdout, "buffer")
                       else sys.stdout)


def _get_binary_stdio():
    """Return (stdin_binary, stdout_binary) — the *original* fds, not
    the possibly-redirected Python wrappers."""
    return _REAL_STDIN_BUFFER, _REAL_STDOUT_BUFFER


def _redirect_user_output():
    """Redirect Python-level ``sys.stdout`` to ``sys.stderr`` so any
    accidental ``print`` in downstream tool code does NOT corrupt the
    frame stream.

    The real stdout binary buffer is captured at module load
    (``_REAL_STDOUT_BUFFER``) so the protocol writer keeps working
    after this rebinding.
    """
    sys.stdout = sys.stderr


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------

class WorkerState:
    """Mutable per-worker state. Lives in one subprocess only."""

    def __init__(self, boot: Dict[str, Any]):
        self.agent_id: str = boot.get("agent_id", "")
        self.agent_name: str = boot.get("agent_name", "")
        self.work_dir: str = boot["work_dir"]
        self.sandbox_mode: str = boot.get("sandbox_mode", "restricted")
        self.shared_workspace: Optional[str] = boot.get("shared_workspace")
        self.authorized_workspaces: list[str] = list(boot.get("authorized_workspaces") or [])
        self.allow_list: list[str] = list(boot.get("allow_list") or [])
        self.started_at: float = time.time()
        self.lock = threading.Lock()
        # full_agent mode: worker hosts a complete Agent instance
        self.mode: str = boot.get("mode", "tool_sandbox")  # "tool_sandbox" | "full_agent"
        self.agent_persist_dict: Optional[Dict[str, Any]] = boot.get("agent_persist_dict")
        self.data_dir: str = boot.get("data_dir", "")
        self._agent: Any = None  # lazy-loaded Agent instance (full_agent mode)
        self._agent_lock = threading.Lock()

    def allowed_dirs(self) -> list[str]:
        """All read/write dirs this worker is allowed to touch (in addition to work_dir).

        Layer 1: shared_workspace + any authorized workspaces. Cross-boundary
        reads outside these go through the main-process gatekeeper.
        """
        dirs: list[str] = []
        if self.shared_workspace:
            dirs.append(self.shared_workspace)
        dirs.extend(self.authorized_workspaces)
        return dirs

    def get_agent(self):
        """Lazy-load the Agent instance (full_agent mode only)."""
        if self._agent is not None:
            return self._agent
        with self._agent_lock:
            if self._agent is not None:
                return self._agent
            if self.mode != "full_agent" or not self.agent_persist_dict:
                return None
            from app.agent import Agent
            self._agent = Agent.from_persist_dict(self.agent_persist_dict)
            sys.stderr.write(
                f"[agent_worker] Agent {self.agent_id[:8]} "
                f"({self._agent.name}) loaded in full_agent mode\n")
            sys.stderr.flush()
            return self._agent

    def apply_capability_update(self, payload: Dict[str, Any]) -> None:
        """Merge a capability update pushed by the main process."""
        with self.lock:
            if "sandbox_mode" in payload:
                self.sandbox_mode = str(payload["sandbox_mode"])
            if "shared_workspace" in payload:
                self.shared_workspace = payload.get("shared_workspace") or None
            if "authorized_workspaces" in payload:
                self.authorized_workspaces = list(payload.get("authorized_workspaces") or [])
            if "allow_list" in payload:
                self.allow_list = list(payload.get("allow_list") or [])


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _install_sandbox_policy(state: WorkerState) -> None:
    """Install the thread-local SandboxPolicy for this worker."""
    from app.sandbox import SandboxPolicy, set_current_policy
    policy = SandboxPolicy(
        root=state.work_dir,
        mode=state.sandbox_mode,
        allow_list=state.allow_list,
        agent_id=state.agent_id,
        agent_name=state.agent_name,
        allowed_dirs=state.allowed_dirs(),
    )
    set_current_policy(policy)


def _refresh_sandbox_policy(state: WorkerState) -> None:
    """Re-install the SandboxPolicy after a capability update."""
    _install_sandbox_policy(state)


# ---------------------------------------------------------------------------
# Shared directory gate requests (Phase 2)
# ---------------------------------------------------------------------------

# Gate request counter (per-worker, thread-safe)
_gate_counter_lock = threading.Lock()
_gate_counter = 0


def _next_gate_id() -> str:
    global _gate_counter
    with _gate_counter_lock:
        _gate_counter += 1
        return f"g-{_gate_counter}"


def is_shared_path(state: WorkerState, path: str) -> bool:
    """Check if a path falls inside the shared workspace (not private)."""
    if not state.shared_workspace:
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
        shared = os.path.realpath(state.shared_workspace)
        return resolved == shared or resolved.startswith(shared + os.sep)
    except Exception:
        return False


def gate_shared_write(state: WorkerState, path: str, content: str) -> Dict:
    """Request the Hub to write to a shared directory via gate protocol.

    Called from agent_worker_hooks when a write targets the shared workspace.
    The Hub's SharedFileRouter handles locking and atomic writes.

    Returns the gate response payload dict or raises on error.
    """
    from app.isolation.protocol import Frame, DEFAULT_CHAN_ID, write_frame, FrameKind

    gate_id = _next_gate_id()
    _, stdout_b = _get_binary_stdio()
    stdin_b, _ = _get_binary_stdio()

    gate_frame = Frame.gate(gate_id, "shared_write", {
        "path": path,
        "content": content,
        "agent_id": state.agent_id,
    })
    write_frame(stdout_b, gate_frame, DEFAULT_CHAN_ID)

    # Block until gate_resp arrives — the main loop handles routing,
    # but we're calling from a tool thread, not the main loop.
    # For full_agent mode, the tool runs inside agent.chat() which is
    # in the main loop's request handler thread, so we need a different
    # mechanism. For now, shared writes in full_agent mode go through
    # the Agent's own file operations (hooks redirect if needed).
    # The gate mechanism is primarily for tool_sandbox mode.
    return {"ok": True, "note": "gate request sent"}


def gate_shared_append(state: WorkerState, path: str, content: str) -> Dict:
    """Request the Hub to append to a shared file via gate."""
    from app.isolation.protocol import Frame, DEFAULT_CHAN_ID, write_frame

    gate_id = _next_gate_id()
    _, stdout_b = _get_binary_stdio()
    gate_frame = Frame.gate(gate_id, "shared_append", {
        "path": path,
        "content": content,
        "agent_id": state.agent_id,
    })
    write_frame(stdout_b, gate_frame, DEFAULT_CHAN_ID)
    return {"ok": True, "note": "gate request sent"}


def _dispatch_tool_call(state: WorkerState, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one tool call inside the worker and return a result dict.

    The caller turns this into either a resp_ok(result) or a
    resp_err(error) frame. We never raise out of this function; any
    exception becomes a structured error.
    """
    name = params.get("tool")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        return {"_worker_error": {
            "type": "bad_request",
            "message": "tool_call missing 'tool' field",
        }}
    if not isinstance(arguments, dict):
        return {"_worker_error": {
            "type": "bad_request",
            "message": "tool_call 'arguments' must be an object",
        }}

    # Late import so env scrubbing has already happened.
    from app.tools import tool_registry
    from app.sandbox import SandboxViolation

    _install_sandbox_policy(state)
    try:
        output = tool_registry.dispatch(name, arguments)
    except SandboxViolation as e:
        return {"_worker_error": {
            "type": "sandbox_violation",
            "message": str(e),
        }}
    except Exception as e:
        return {"_worker_error": {
            "type": "tool_exception",
            "message": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(limit=6),
        }}
    # tool_registry.dispatch returns a string; wrap it uniformly.
    return {"tool": name, "output": output if isinstance(output, str) else str(output)}


# ---------------------------------------------------------------------------
# Agent-level handlers (full_agent mode)
# ---------------------------------------------------------------------------

def _handle_delegate(state: WorkerState, req_id: str,
                     params: Dict[str, Any]) -> "object":
    """Synchronous delegate — blocks until the agent finishes."""
    from app.isolation.protocol import Frame

    agent = state.get_agent()
    if agent is None:
        return Frame.response_err(req_id, "not_full_agent",
                                  "Worker not in full_agent mode")
    content = params.get("content", "")
    from_agent = params.get("from_agent", "hub")
    try:
        result = agent.delegate(content, from_agent=from_agent)
        return Frame.response_ok(req_id, {"result": result})
    except Exception as e:
        return Frame.response_err(req_id, "delegate_error",
                                  f"{type(e).__name__}: {e}")


def _handle_chat(state: WorkerState, req_id: str,
                 params: Dict[str, Any]) -> "object":
    """Run agent.chat() with event streaming back to Hub.

    Events are sent as EVENT frames (kind2="chat_event") during execution.
    The final RESPONSE frame carries the result text.
    """
    from app.isolation.protocol import Frame, DEFAULT_CHAN_ID

    agent = state.get_agent()
    if agent is None:
        return Frame.response_err(req_id, "not_full_agent",
                                  "Worker not in full_agent mode")

    content = params.get("content", "")
    source = params.get("source", "admin")
    task_id = params.get("task_id", req_id)
    _, stdout_b = _get_binary_stdio()
    from app.isolation.protocol import write_frame

    def _on_event(evt):
        """Stream agent events back to Hub as EVENT frames."""
        try:
            evt_data = {
                "task_id": task_id,
                "req_id": req_id,
                "kind": evt.kind,
                "data": evt.data if isinstance(evt.data, dict) else {"raw": str(evt.data)},
            }
            write_frame(stdout_b,
                        Frame.event("chat_event", evt_data),
                        DEFAULT_CHAN_ID)
        except Exception:
            pass  # never break the chat loop for event delivery

    try:
        result = agent.chat(content, on_event=_on_event, source=source)
        return Frame.response_ok(req_id, {
            "result": result,
            "task_id": task_id,
        })
    except Exception as e:
        return Frame.response_err(req_id, "chat_error",
                                  f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _handle_request(state: WorkerState, frame) -> "object":
    """Dispatch a REQUEST frame, return the Frame to send back."""
    from app.isolation.protocol import Frame

    method = frame.method or ""
    req_id = frame.id or ""
    params = frame.params or {}

    if method == "ping":
        return Frame.response_ok(req_id, {
            "pong": True,
            "agent_id": state.agent_id,
            "work_dir": state.work_dir,
            "uptime": time.time() - state.started_at,
        })

    if method == "tool_call":
        result = _dispatch_tool_call(state, params)
        err = result.get("_worker_error") if isinstance(result, dict) else None
        if err:
            return Frame.response_err(req_id, err.get("type", "error"),
                                      err.get("message", ""),
                                      {k: v for k, v in err.items()
                                       if k not in ("type", "message")})
        return Frame.response_ok(req_id, result)

    if method == "delegate":
        return _handle_delegate(state, req_id, params)

    if method == "chat":
        # chat is async — runs in a thread, streams events, then sends response
        return _handle_chat(state, req_id, params)

    if method == "get_state":
        agent = state.get_agent()
        if agent is None:
            return Frame.response_err(req_id, "not_full_agent",
                                      "Worker not in full_agent mode")
        return Frame.response_ok(req_id, {
            "agent_id": agent.id,
            "persist_dict": agent.to_persist_dict(),
            "messages_count": len(agent.messages),
        })

    if method == "shutdown":
        return Frame.response_ok(req_id, {"shutdown": True})

    return Frame.response_err(req_id, "unknown_method",
                              f"Worker has no handler for method {method!r}")


def _handle_notify(state: WorkerState, frame) -> None:
    """Apply a NOTIFY frame. No reply."""
    kind = frame.kind2 or ""
    payload = frame.payload or {}
    if kind in ("capability_update", "policy_update",
                "mcp_update", "skill_update"):
        state.apply_capability_update(payload)
        _refresh_sandbox_policy(state)
    elif kind == "ping":
        pass  # ignore
    # Unknown notifies are silently dropped — they are forward-
    # compatible extension points.


def _run(state: WorkerState) -> int:
    """The worker's main I/O loop. Returns the desired exit code.

    The worker always uses chan_id=0 on its outbound frames; if the
    NodeAgent is relaying bytes between main and us, it stamps the
    real chan_id on the way up. We ignore whatever chan_id shows up
    on inbound frames for the same reason — the NodeAgent strips it
    before the bytes reach our stdin.
    """
    from app.isolation.protocol import (
        DEFAULT_CHAN_ID, Frame, FrameKind, ProtocolError,
        read_frame, write_frame,
    )

    stdin_b, stdout_b = _get_binary_stdio()
    _install_sandbox_policy(state)

    # Announce readiness so the parent knows the boot handshake is done.
    write_frame(stdout_b, Frame.event("ready", {
        "agent_id": state.agent_id,
        "work_dir": state.work_dir,
        "pid": os.getpid(),
    }), DEFAULT_CHAN_ID)

    while True:
        try:
            _, frame = read_frame(stdin_b)
        except ProtocolError as e:
            sys.stderr.write(f"[agent_worker] protocol error: {e}\n")
            return 3
        except Exception as e:
            sys.stderr.write(f"[agent_worker] read_frame failed: {e}\n")
            return 4

        try:
            if frame.kind == FrameKind.REQUEST:
                reply = _handle_request(state, frame)
                write_frame(stdout_b, reply, DEFAULT_CHAN_ID)
                if frame.method == "shutdown":
                    return 0
            elif frame.kind == FrameKind.NOTIFY:
                _handle_notify(state, frame)
            elif frame.kind == FrameKind.GATE_RESP:
                # Layer 1 does not yet initiate gate requests from the
                # worker, so an orphan gate_resp is unexpected. Log it.
                sys.stderr.write(
                    f"[agent_worker] unexpected GATE_RESP id={frame.id}\n")
            else:
                # EVENT / RESPONSE / GATE from main -> worker are not
                # allowed in the protocol. Ignore them defensively.
                sys.stderr.write(
                    f"[agent_worker] ignoring unexpected kind={frame.kind}\n")
        except Exception as e:
            sys.stderr.write(
                f"[agent_worker] handler crash: {e}\n"
                f"{traceback.format_exc(limit=8)}\n")
            # Try to send a best-effort error reply if this was a req.
            if frame.kind == FrameKind.REQUEST and frame.id:
                try:
                    write_frame(stdout_b, Frame.response_err(
                        frame.id, "handler_crash", str(e)), DEFAULT_CHAN_ID)
                except Exception:
                    return 5


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def _parse_boot_args(argv: list[str]) -> Dict[str, Any]:
    """Parse the CLI args the parent process passes on spawn.

    Boot config is passed as a single ``--boot-json`` argument
    containing a JSON blob. We use JSON (not a pile of flags) because
    lists (authorized_workspaces, allow_list) are awkward in argv
    and we want exact byte fidelity.
    """
    parser = argparse.ArgumentParser(prog="agent_worker", add_help=False)
    parser.add_argument("--boot-json", required=True,
                        help="JSON-encoded boot config blob")
    parser.add_argument("--boot-file", default="",
                        help="Optional path to read boot-json from "
                             "(avoids argv length limits on Windows).")
    parsed, _ = parser.parse_known_args(argv)

    if parsed.boot_file:
        try:
            data = Path(parsed.boot_file).read_text(encoding="utf-8")
        except Exception as e:
            _boot_fail(f"reading --boot-file {parsed.boot_file}: {e}")
        try:
            return json.loads(data)
        except Exception as e:
            _boot_fail(f"parsing boot-file JSON: {e}")

    try:
        return json.loads(parsed.boot_json)
    except Exception as e:
        _boot_fail(f"parsing --boot-json: {e}")
    return {}  # unreachable


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    boot = _parse_boot_args(argv)

    work_dir = boot.get("work_dir")
    if not work_dir:
        _boot_fail("boot config missing 'work_dir'")

    is_full_agent = boot.get("mode") == "full_agent"

    # IMPORTANT ORDER: jail setup must happen before we import the
    # tools / sandbox modules. That way any module-level code that
    # touches HOME/TMPDIR/os.getcwd() sees the jailed values.
    _setup_jail(work_dir, full_agent=is_full_agent)
    _redirect_user_output()

    # Make sure the app package on disk is importable. The main
    # process passes the project root on sys.path via PYTHONPATH.
    # Fall back to inferring from this file's location.
    if "app" not in sys.modules:
        here = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(here))

    state = WorkerState(boot)

    # agent_worker_hooks is optional (Layer 1 ships a stub); import
    # is best-effort so we can iterate on the hooks without breaking
    # worker boot.
    try:
        from app import agent_worker_hooks  # noqa: F401
        agent_worker_hooks.install(state)
    except Exception as e:
        sys.stderr.write(
            f"[agent_worker] agent_worker_hooks not installed: {e}\n")

    return _run(state)


if __name__ == "__main__":
    sys.exit(main())
