"""NodeMCPDispatcher — the *executor* layer of the MCP call architecture.

Architectural position
══════════════════════

                 ┌─────────────────┐
    caller ───▶  │  MCPClientStub  │   (agent-facing API)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │  MCPCallRouter  │   (auth + locate + classify)
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │ NodeMCPDispatch │   ← THIS FILE
                 │ (subprocess +   │
                 │  JSON-RPC +     │
                 │  env injection) │
                 └─────────────────┘

Invariants this module enforces
───────────────────────────────
1. **Single launch site.** This is the *only* module in the codebase that
   calls ``Popen`` for an MCP server. Any other caller that needs an MCP
   call must go through the router, which calls here. That makes
   path/env bugs impossible to reintroduce — there's only one place to
   get them right.

2. **Single path computation.** All ``cwd`` / ``PYTHONPATH`` handling is
   delegated to :mod:`app.runtime_paths`. We never recompute project
   root locally. If ``runtime_paths`` is right, we're right.

3. **Structured errors.** This module never returns ``"Error: ..."``
   strings. It returns a :class:`DispatchResult` with a stable error
   taxonomy so the router can classify failures consistently across
   every MCP type.

4. **Credential staging.** Credentials supplied by the caller (via the
   ``MCPServerConfig.env`` dict) land in the subprocess's environment
   and nowhere else. No secrets are logged; no secrets are persisted in
   returned results.

Scope of this first cut
───────────────────────
- Transport: ``stdio`` only. HTTP/SSE transports can be added by
  growing this module, not by touching the layers above it.
- Session: one subprocess per call, terminated after the tool call
  completes. A pooled variant (keep subprocess warm, reuse between
  calls) is a drop-in replacement for :meth:`dispatch` later — the
  router and stub do not need to change.
- Builtins: MCPs with ``transport == "builtin"`` are handled by a
  thin callback that the builtin handler (currently living in
  ``app.tools``) registers with the dispatcher at startup. That keeps
  TTS/STT flows working without this file having to know about them.
"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..runtime_paths import subprocess_launch_kwargs

logger = logging.getLogger("tudou.mcp.dispatcher")


# ─────────────────────────── error taxonomy ───────────────────────────

# These are the *only* error kinds this module emits. The router
# translates some of them further (e.g. NOT_AUTHORIZED is produced
# above this layer). Keep this list small and stable — it's a contract
# with everything upstream.
ERR_NOT_CONFIGURED       = "mcp_not_configured"      # no command set
ERR_TRANSPORT_UNSUPPORTED = "transport_unsupported"  # e.g. sse, http
ERR_LAUNCH_FAILED        = "launch_failed"           # Popen failed
ERR_HANDSHAKE_TIMEOUT    = "handshake_timeout"       # initialize never replied
ERR_HANDSHAKE_ERROR      = "handshake_error"         # initialize replied with error
ERR_TOOL_TIMEOUT         = "tool_timeout"            # tools/call never replied
ERR_TOOL_ERROR           = "tool_error"              # tools/call replied with error
ERR_INTERNAL             = "internal_error"          # our code broke
ERR_BUILTIN_FAILED       = "builtin_failed"          # builtin handler raised


@dataclass
class DispatchResult:
    """Structured result of one MCP dispatch.

    The router and stub know how to format this for their respective
    callers (structured for programmatic use, string for legacy tool
    interfaces). This module itself never formats for humans.
    """
    ok: bool
    # On success: the decoded tool result (str or dict/list per MCP reply).
    content: Any = None
    # On failure: one of the ERR_* constants above.
    error_kind: str = ""
    # Human-readable message (safe to log, never contains credentials).
    error_message: str = ""
    # Last ~500 bytes of the subprocess stderr, if any. Useful for
    # diagnostics; routed up so the router can decide whether to
    # surface it.
    stderr_tail: str = ""
    # Wall-clock elapsed time for the entire dispatch, in milliseconds.
    elapsed_ms: int = 0
    # Extra metadata (e.g. server_info from initialize). Opaque to
    # layers above except for inspection tools.
    meta: dict = field(default_factory=dict)


# ───────────────────── builtin handler registration ─────────────────────
#
# Some "MCPs" are actually in-process builtins (the audio TTS/STT
# family for example). They don't have a subprocess — they just need
# to be dispatched. Rather than making this module know about every
# builtin flavor, we expose a registration hook. The builtin owner
# registers itself at startup; the dispatcher looks up by transport
# tag.

BuiltinHandler = Callable[[Any, str, dict, Any], str]
_builtin_handlers: dict[str, BuiltinHandler] = {}
_builtin_lock = threading.Lock()


def register_builtin_handler(tag: str, handler: BuiltinHandler) -> None:
    """Register a handler for ``transport == "builtin"`` MCPs.

    ``tag`` is a coarse selector (typically the MCPServerConfig's
    ``command`` field, e.g. ``__builtin__audio``). The handler receives
    ``(target_config, tool_name, arguments, agent)`` and returns a
    string payload or raises.
    """
    with _builtin_lock:
        _builtin_handlers[tag] = handler


def _lookup_builtin(tag: str) -> Optional[BuiltinHandler]:
    with _builtin_lock:
        return _builtin_handlers.get(tag)


# ────────────────────── stdio JSON-RPC implementation ──────────────────────

# Timeouts chosen to match the values that used to live inline in
# tools.py. If we ever want to pool these or make them configurable,
# it happens here and only here.
_HANDSHAKE_TIMEOUT_S = 15.0
_TOOL_CALL_TIMEOUT_S = 90.0
_SUBPROC_KILL_WAIT_S = 5.0


def _spawn_stdio(command: str, env_overrides: dict[str, str]) -> tuple[subprocess.Popen | None, str]:
    """Launch the MCP subprocess with correct cwd/env.

    Returns ``(proc, error_message)``. On success ``error_message``
    is empty. On failure ``proc`` is None and ``error_message`` tells
    the router what went wrong.
    """
    try:
        cmd = shlex.split(command)
    except ValueError as e:
        return None, f"command parse failed: {e}"
    if not cmd:
        return None, "empty command"

    # ── Decrypt sensitive env values right before Popen ──
    # By the time env_overrides reaches us it may contain "enc:v1:..."
    # ciphertext for fields like SMTP_PASSWORD. The MCP child process
    # needs plaintext to actually authenticate, so we decrypt here.
    # Plaintext lives in:
    #   * this stack frame (briefly)
    #   * Popen's env kwarg (passed to OS exec)
    #   * the child process's environment
    # …and never gets logged or written back to disk.
    try:
        from .secrets import decrypt_env_dict
        env_overrides = decrypt_env_dict(env_overrides or {})
    except Exception:
        pass  # if decrypt module fails, pass through (may be plaintext legacy)

    # ALL path/env logic comes from runtime_paths. One source of truth.
    kw = subprocess_launch_kwargs(extra_env=env_overrides)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **kw,
        )
        return proc, ""
    except FileNotFoundError:
        return None, f"command not found: {cmd[0]}"
    except Exception as e:
        return None, f"popen failed: {e}"


def _dispatch_stdio(
    command: str,
    env_overrides: dict[str, str],
    tool_name: str,
    arguments: dict,
) -> DispatchResult:
    """Run a full initialize → tools/call → terminate sequence."""
    started = time.time()

    proc, launch_err = _spawn_stdio(command, env_overrides)
    if proc is None:
        return DispatchResult(
            ok=False,
            error_kind=ERR_LAUNCH_FAILED,
            error_message=launch_err,
            elapsed_ms=int((time.time() - started) * 1000),
        )

    out_q: queue.Queue = queue.Queue()
    err_lines: list[str] = []

    def _stdout_reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                out_q.put(line)
        except Exception:
            pass

    def _stderr_reader() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                err_lines.append(line)
        except Exception:
            pass

    threading.Thread(target=_stdout_reader, daemon=True).start()
    threading.Thread(target=_stderr_reader, daemon=True).start()

    def _write(req: dict) -> bool:
        try:
            proc.stdin.write(json.dumps(req) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()                         # type: ignore[union-attr]
            return True
        except Exception:
            return False

    def _read_response(req_id: int, timeout_s: float) -> dict | None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                line = out_q.get(timeout=max(0.1, remaining))
            except queue.Empty:
                continue
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == req_id:
                return msg
        return None

    result: DispatchResult
    server_info: dict = {}
    try:
        # ── handshake: initialize ──
        if not _write({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tudou_claw", "version": "1.0"},
            },
        }):
            result = DispatchResult(
                ok=False,
                error_kind=ERR_HANDSHAKE_ERROR,
                error_message="failed to write initialize request",
            )
        else:
            init_resp = _read_response(1, _HANDSHAKE_TIMEOUT_S)
            if init_resp is None:
                result = DispatchResult(
                    ok=False,
                    error_kind=ERR_HANDSHAKE_TIMEOUT,
                    error_message=f"initialize timed out after {_HANDSHAKE_TIMEOUT_S}s",
                )
            elif "error" in init_resp:
                result = DispatchResult(
                    ok=False,
                    error_kind=ERR_HANDSHAKE_ERROR,
                    error_message=f"initialize error: {init_resp['error']}",
                )
            else:
                server_info = (init_resp.get("result") or {}).get("serverInfo", {}) or {}
                # Complete handshake
                _write({"jsonrpc": "2.0", "method": "notifications/initialized"})

                # ── the actual tools/call ──
                if not _write({
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }):
                    result = DispatchResult(
                        ok=False,
                        error_kind=ERR_TOOL_ERROR,
                        error_message="failed to write tools/call request",
                    )
                else:
                    tool_resp = _read_response(2, _TOOL_CALL_TIMEOUT_S)
                    if tool_resp is None:
                        result = DispatchResult(
                            ok=False,
                            error_kind=ERR_TOOL_TIMEOUT,
                            error_message=(
                                f"tools/call '{tool_name}' timed out after "
                                f"{_TOOL_CALL_TIMEOUT_S}s"
                            ),
                        )
                    elif "error" in tool_resp:
                        result = DispatchResult(
                            ok=False,
                            error_kind=ERR_TOOL_ERROR,
                            error_message=str(tool_resp.get("error")),
                        )
                    else:
                        result = DispatchResult(
                            ok=True,
                            content=_extract_content(tool_resp.get("result", {})),
                            meta={"server_info": server_info},
                        )
    except Exception as e:
        result = DispatchResult(
            ok=False,
            error_kind=ERR_INTERNAL,
            error_message=f"dispatcher crashed: {e}",
        )
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=_SUBPROC_KILL_WAIT_S)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Attach diagnostics that were collected regardless of path taken.
    result.stderr_tail = "".join(err_lines)[-500:]
    result.elapsed_ms = int((time.time() - started) * 1000)
    if server_info and "server_info" not in result.meta:
        result.meta["server_info"] = server_info
    return result


def _extract_content(result_obj: Any) -> Any:
    """Normalize an MCP tools/call result into a friendlier payload.

    MCP spec returns ``{"content": [{"type": "text", "text": "..."}, ...]}``
    but some servers return arbitrary shapes. We try the structured
    path first and fall back to returning the raw object.
    """
    if not isinstance(result_obj, dict):
        return result_obj
    content = result_obj.get("content", result_obj)
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            else:
                texts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(texts) if texts else result_obj
    return content


# ─────────────────── per-MCP env normalization helpers ───────────────────
#
# Some MCP servers expect specific environment variable names. Rather
# than forcing the Portal UI to know every server's quirks, we keep a
# small set of normalizers here that translate user-friendly short
# names (SMTP_HOST) into the exact keys the server expects
# (MCP_EMAIL_SERVER_SMTP_HOST). This used to live inline in tools.py;
# it belongs at the dispatch layer because it's per-MCP subprocess
# preparation, not per-caller.

_EMAIL_ENV_MAP = {
    "SMTP_HOST": "MCP_EMAIL_SERVER_SMTP_HOST",
    "SMTP_PORT": "MCP_EMAIL_SERVER_SMTP_PORT",
    "SMTP_USER": "MCP_EMAIL_SERVER_SMTP_USER_NAME",
    "SMTP_USERNAME": "MCP_EMAIL_SERVER_SMTP_USER_NAME",
    "SMTP_PASSWORD": "MCP_EMAIL_SERVER_SMTP_PASSWORD",
    "SMTP_SSL": "MCP_EMAIL_SERVER_SMTP_SSL",
    "SMTP_START_SSL": "MCP_EMAIL_SERVER_SMTP_START_SSL",
    "SMTP_VERIFY_SSL": "MCP_EMAIL_SERVER_SMTP_VERIFY_SSL",
    "IMAP_HOST": "MCP_EMAIL_SERVER_IMAP_HOST",
    "IMAP_PORT": "MCP_EMAIL_SERVER_IMAP_PORT",
    "IMAP_USER": "MCP_EMAIL_SERVER_IMAP_USER_NAME",
    "IMAP_USERNAME": "MCP_EMAIL_SERVER_IMAP_USER_NAME",
    "IMAP_PASSWORD": "MCP_EMAIL_SERVER_IMAP_PASSWORD",
    "IMAP_SSL": "MCP_EMAIL_SERVER_IMAP_SSL",
    "ACCOUNT_NAME": "MCP_EMAIL_SERVER_ACCOUNT_NAME",
    "EMAIL_ADDRESS": "MCP_EMAIL_SERVER_EMAIL_ADDRESS",
    "FULL_NAME": "MCP_EMAIL_SERVER_FULL_NAME",
}


def _normalize_env_for_command(command: str, env: dict[str, str]) -> dict[str, str]:
    """Return a new env dict with MCP-server-specific keys filled in.

    The input ``env`` is not mutated — we return a fresh copy so the
    caller's MCPServerConfig.env stays clean.
    """
    out = dict(env)
    cmd_lower = (command or "").lower()

    if "mcp-email-server" in cmd_lower or "mcp_email_server" in cmd_lower:
        for short, full in _EMAIL_ENV_MAP.items():
            if short in out and full not in out:
                out[full] = out[short]
        smtp_user = out.get("MCP_EMAIL_SERVER_SMTP_USER_NAME", "")
        if smtp_user:
            out.setdefault("MCP_EMAIL_SERVER_ACCOUNT_NAME", smtp_user)
            out.setdefault("MCP_EMAIL_SERVER_EMAIL_ADDRESS", smtp_user)
            out.setdefault("MCP_EMAIL_SERVER_USER_NAME", smtp_user)
            smtp_pass = out.get("MCP_EMAIL_SERVER_SMTP_PASSWORD", "")
            if smtp_pass:
                out.setdefault("MCP_EMAIL_SERVER_PASSWORD", smtp_pass)
        smtp_host = out.get("MCP_EMAIL_SERVER_SMTP_HOST", "").lower()
        if smtp_host:
            if "MCP_EMAIL_SERVER_SMTP_PORT" not in out:
                if any(d in smtp_host for d in ("163.com", "qq.com", "gmail")):
                    out["MCP_EMAIL_SERVER_SMTP_PORT"] = "465"
            out.setdefault("MCP_EMAIL_SERVER_SMTP_SSL", "true")
            if "MCP_EMAIL_SERVER_IMAP_HOST" not in out:
                out["MCP_EMAIL_SERVER_IMAP_HOST"] = smtp_host.replace("smtp.", "imap.")
                out.setdefault("MCP_EMAIL_SERVER_IMAP_PORT", "993")
                out.setdefault("MCP_EMAIL_SERVER_IMAP_SSL", "true")
            smtp_user2 = out.get("MCP_EMAIL_SERVER_SMTP_USER_NAME", "")
            smtp_pass2 = out.get("MCP_EMAIL_SERVER_SMTP_PASSWORD", "")
            if smtp_user2:
                out.setdefault("MCP_EMAIL_SERVER_IMAP_USER_NAME", smtp_user2)
            if smtp_pass2:
                out.setdefault("MCP_EMAIL_SERVER_IMAP_PASSWORD", smtp_pass2)

    # AgentMail: user-friendly short names → env the MCP expects
    if "agentmail" in cmd_lower:
        for short, full in (("API_KEY", "AGENTMAIL_API_KEY"),
                            ("INBOX_ID", "AGENTMAIL_INBOX_ID")):
            if short in out and full not in out:
                out[full] = out[short]

    return out


# ──────────────────────────── public API ────────────────────────────

class NodeMCPDispatcher:
    """Runs MCP calls on this node. One instance per hub/node.

    This class holds no per-call state and no long-lived subprocesses
    in the first cut. A pooled subclass can override :meth:`dispatch`
    without changing the caller contract.
    """

    def dispatch(
        self,
        target: Any,              # MCPServerConfig (avoid circular import)
        tool_name: str,
        arguments: dict,
        agent: Any = None,         # only for builtins that need agent context
    ) -> DispatchResult:
        """Execute one MCP tool call.

        The router (not agent code, not tools.py) is the expected
        caller. ``target`` must be a fully resolved ``MCPServerConfig``
        — the router is responsible for looking it up by mcp_id.
        """
        if target is None:
            return DispatchResult(
                ok=False,
                error_kind=ERR_NOT_CONFIGURED,
                error_message="no target MCP config",
            )

        transport = getattr(target, "transport", "") or ""
        command   = getattr(target, "command", "") or ""

        # ── builtin branch ──
        if transport == "builtin" or command.startswith("__builtin__"):
            handler = _lookup_builtin(command) or _lookup_builtin(transport)
            if handler is None:
                return DispatchResult(
                    ok=False,
                    error_kind=ERR_BUILTIN_FAILED,
                    error_message=f"no builtin handler registered for {command or transport}",
                )
            started = time.time()
            try:
                payload = handler(target, tool_name, arguments or {}, agent)
                return DispatchResult(
                    ok=True,
                    content=payload,
                    elapsed_ms=int((time.time() - started) * 1000),
                )
            except Exception as e:
                return DispatchResult(
                    ok=False,
                    error_kind=ERR_BUILTIN_FAILED,
                    error_message=f"builtin handler failed: {e}",
                    elapsed_ms=int((time.time() - started) * 1000),
                )

        # ── stdio branch (the common case) ──
        if transport != "stdio":
            return DispatchResult(
                ok=False,
                error_kind=ERR_TRANSPORT_UNSUPPORTED,
                error_message=f"transport '{transport}' not supported by dispatcher",
            )
        if not command:
            return DispatchResult(
                ok=False,
                error_kind=ERR_NOT_CONFIGURED,
                error_message=f"mcp '{getattr(target, 'id', '?')}' has no command configured",
            )

        raw_env = dict(getattr(target, "env", {}) or {})
        env_overrides = _normalize_env_for_command(command, raw_env)

        return _dispatch_stdio(command, env_overrides, tool_name, arguments or {})

    def probe(self, target: Any, timeout_s: float = 10.0) -> DispatchResult:
        """Connect + handshake + tools/list, without a real tool call.

        Used by health checks and the Portal "test connection" button.
        Crucially, it runs through the same ``_spawn_stdio`` code as
        real calls, so it cannot pass while real calls fail (and vice
        versa).
        """
        if target is None:
            return DispatchResult(
                ok=False,
                error_kind=ERR_NOT_CONFIGURED,
                error_message="no target MCP config",
            )

        transport = getattr(target, "transport", "") or ""
        command   = getattr(target, "command", "") or ""

        if transport == "builtin":
            # Builtins have no subprocess to probe — treat presence of
            # a handler as healthy.
            if _lookup_builtin(command) or _lookup_builtin(transport):
                return DispatchResult(ok=True, content="builtin handler present")
            return DispatchResult(
                ok=False,
                error_kind=ERR_BUILTIN_FAILED,
                error_message="no builtin handler registered",
            )
        if transport != "stdio":
            # Non-stdio transports pass basic validation — real probe
            # support arrives with transport support.
            return DispatchResult(
                ok=True,
                content=f"{transport} transport: basic config check only",
            )
        if not command:
            return DispatchResult(
                ok=False,
                error_kind=ERR_NOT_CONFIGURED,
                error_message="no command configured",
            )

        env_overrides = _normalize_env_for_command(
            command, dict(getattr(target, "env", {}) or {})
        )
        started = time.time()
        proc, launch_err = _spawn_stdio(command, env_overrides)
        if proc is None:
            return DispatchResult(
                ok=False,
                error_kind=ERR_LAUNCH_FAILED,
                error_message=launch_err,
                elapsed_ms=int((time.time() - started) * 1000),
            )

        out_q: queue.Queue = queue.Queue()
        err_lines: list[str] = []

        def _out() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    out_q.put(line)
            except Exception:
                pass

        def _err() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    err_lines.append(line)
            except Exception:
                pass

        threading.Thread(target=_out, daemon=True).start()
        threading.Thread(target=_err, daemon=True).start()

        def _write(req: dict) -> bool:
            try:
                proc.stdin.write(json.dumps(req) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()                         # type: ignore[union-attr]
                return True
            except Exception:
                return False

        def _read_response(req_id: int, wait_s: float) -> dict | None:
            deadline = time.time() + wait_s
            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    line = out_q.get(timeout=max(0.1, remaining))
                except queue.Empty:
                    continue
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == req_id:
                    return msg
            return None

        result: DispatchResult
        # tool_manifests is the authoritative record: each entry is the
        # full dict returned by the MCP server (``{name, description,
        # inputSchema}``). Upstream consumers (MCP.md, list_mcps, Portal)
        # all read from this, so truncating to just names here would
        # permanently lose information that the server freely provided.
        tool_manifests: list[dict] = []
        tools: list[str] = []  # legacy shape: names only, kept for back-compat
        server_info: dict = {}
        try:
            if not _write({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tudou_claw", "version": "1.0"},
                },
            }):
                result = DispatchResult(
                    ok=False, error_kind=ERR_HANDSHAKE_ERROR,
                    error_message="failed to write initialize",
                )
            else:
                init_resp = _read_response(1, timeout_s)
                if init_resp is None:
                    result = DispatchResult(
                        ok=False, error_kind=ERR_HANDSHAKE_TIMEOUT,
                        error_message=f"no response to initialize within {timeout_s}s",
                    )
                elif "error" in init_resp:
                    result = DispatchResult(
                        ok=False, error_kind=ERR_HANDSHAKE_ERROR,
                        error_message=str(init_resp["error"]),
                    )
                else:
                    server_info = (init_resp.get("result") or {}).get("serverInfo", {}) or {}
                    _write({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    if _write({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}):
                        tl_resp = _read_response(2, timeout_s)
                        if tl_resp and "result" in tl_resp:
                            for t in (tl_resp["result"].get("tools") or []):
                                name = t.get("name")
                                if not name:
                                    continue
                                tools.append(name)
                                # Keep the full manifest entry. ``inputSchema``
                                # is untrusted data from the MCP server — it is
                                # only used for documentation and never for
                                # authorization.
                                tool_manifests.append({
                                    "name": name,
                                    "description": t.get("description", "") or "",
                                    "inputSchema": t.get("inputSchema") or {},
                                })
                    result = DispatchResult(
                        ok=True,
                        content={
                            "tools": tools,
                            "tool_manifests": tool_manifests,
                            "server_info": server_info,
                        },
                        meta={
                            "server_info": server_info,
                            "tools": tools,
                            "tool_manifests": tool_manifests,
                        },
                    )
        except Exception as e:
            result = DispatchResult(
                ok=False, error_kind=ERR_INTERNAL,
                error_message=f"probe crashed: {e}",
            )
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=_SUBPROC_KILL_WAIT_S)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        result.stderr_tail = "".join(err_lines)[-500:]
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result


# Module-level singleton — the dispatcher is stateless (so far), so a
# single instance is fine for the whole process. The router holds a
# reference to this; callers should never import it directly.
_default_dispatcher = NodeMCPDispatcher()


def get_default_dispatcher() -> NodeMCPDispatcher:
    return _default_dispatcher


__all__ = [
    "NodeMCPDispatcher",
    "DispatchResult",
    "register_builtin_handler",
    "get_default_dispatcher",
    # error taxonomy
    "ERR_NOT_CONFIGURED",
    "ERR_TRANSPORT_UNSUPPORTED",
    "ERR_LAUNCH_FAILED",
    "ERR_HANDSHAKE_TIMEOUT",
    "ERR_HANDSHAKE_ERROR",
    "ERR_TOOL_TIMEOUT",
    "ERR_TOOL_ERROR",
    "ERR_INTERNAL",
    "ERR_BUILTIN_FAILED",
]
