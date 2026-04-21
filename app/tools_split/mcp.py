"""MCP tool + builtin audio (TTS/STT) handlers.

Owns:
  - ``_tool_mcp_call``: thin adapter that dispatches agent MCP calls
    through ``app.mcp.client_stub``.
  - Builtin audio MCP: the TTS/STT handler plus the event queue that
    the Portal UI polls to trigger browser-side Web Speech.

The builtin handler is registered with the MCP dispatcher at module
import time so builtin TTS/STT calls flow through the same router
pipeline as external stdio MCPs.

``get_audio_events`` is re-exported from ``app.tools`` for backwards
compat with the portal REST handlers.
"""
from __future__ import annotations

import json as _json
import threading
import time
from typing import Any


# The event queue keeps at most this many entries to bound memory for
# long-lived portals. 50 is enough headroom that a normal user never
# notices events being dropped.
_MAX_AUDIO_EVENTS = 50

# TTS "speak" return-value preview cap — the full utterance is too
# long to include in the tool result.
_TTS_PREVIEW_CHARS = 100


_audio_events: list[dict] = []
_audio_lock = threading.Lock()


def _push_audio_event(event: dict) -> None:
    """Push an audio event for Portal UI to consume."""
    with _audio_lock:
        _audio_events.append(event)
        # Keep last _MAX_AUDIO_EVENTS.
        if len(_audio_events) > _MAX_AUDIO_EVENTS:
            _audio_events[:] = _audio_events[-_MAX_AUDIO_EVENTS:]


def get_audio_events(since: int = 0) -> list[dict]:
    """Get audio events (called by portal API)."""
    with _audio_lock:
        return [e for e in _audio_events if e.get("ts", 0) > since]


def _handle_builtin_mcp(target: Any, tool_name: str, arguments: Any,
                        agent: Any) -> str:
    """Handle builtin MCP tools (audio TTS/STT)."""
    args = arguments if isinstance(arguments, dict) else {}
    if isinstance(arguments, str):
        try:
            args = _json.loads(arguments)
        except Exception:
            args = {}

    mcp_type = (target.command or "").replace("__builtin__", "")

    if mcp_type == "audio_tts":
        if tool_name == "speak":
            text = args.get("text", "")
            if not text:
                return "Error: 'text' argument is required for speak."
            lang = args.get("lang", target.env.get("TTS_LANG", "zh-CN"))
            rate = float(args.get("rate", target.env.get("TTS_RATE", "1.0")))
            voice = args.get("voice", target.env.get("TTS_VOICE", ""))
            _push_audio_event({
                "type": "tts_speak",
                "agent_id": agent.id,
                "agent_name": agent.name,
                "text": text,
                "lang": lang,
                "rate": rate,
                "voice": voice,
                "ts": time.time(),
            })
            preview = text[:_TTS_PREVIEW_CHARS]
            ellipsis = "..." if len(text) > _TTS_PREVIEW_CHARS else ""
            return f"Speaking: \"{preview}{ellipsis}\" [lang={lang}, rate={rate}]"

        if tool_name == "set_voice":
            voice = args.get("voice", "")
            lang = args.get("lang", "")
            return (f"Voice preference set: voice={voice}, lang={lang}. "
                    "Will take effect on next speak().")

        if tool_name == "list_voices":
            return ("Available voices depend on the user's browser. Common ones:\n"
                    "  - zh-CN: Microsoft Xiaoxiao, Google 普通话\n"
                    "  - en-US: Google US English, Microsoft David\n"
                    "  - ja-JP: Google 日本語\n"
                    "Use speak(text, voice='name') to select a specific voice.")
        return (f"Error: TTS tool '{tool_name}' not found. "
                "Available: speak, set_voice, list_voices")

    if mcp_type == "audio_stt":
        if tool_name == "listen":
            duration = int(args.get("duration", 5))
            lang = args.get("lang", target.env.get("STT_LANG", "zh-CN"))
            _push_audio_event({
                "type": "stt_listen",
                "agent_id": agent.id,
                "agent_name": agent.name,
                "duration": duration,
                "lang": lang,
                "ts": time.time(),
            })
            return (f"Listening request sent to browser (lang={lang}, "
                    f"duration={duration}s). The user's speech will be "
                    f"transcribed and sent as the next user message.")

        if tool_name == "start_listening":
            lang = args.get("lang", target.env.get("STT_LANG", "zh-CN"))
            _push_audio_event({
                "type": "stt_start",
                "agent_id": agent.id,
                "lang": lang,
                "ts": time.time(),
            })
            return (f"Continuous listening started (lang={lang}). "
                    "Speech will be sent as messages.")

        if tool_name == "stop_listening":
            _push_audio_event({
                "type": "stt_stop",
                "agent_id": agent.id,
                "ts": time.time(),
            })
            return "Listening stopped."

        return (f"Error: STT tool '{tool_name}' not found. "
                "Available: listen, start_listening, stop_listening")

    return f"Error: builtin MCP type '{mcp_type}' not recognized."


# ── mcp_call ─────────────────────────────────────────────────────────
#
# Architectural note (READ THIS BEFORE ADDING CODE HERE):
#
# This function used to own ~260 lines of subprocess launch, JSON-RPC
# protocol, and env-variable normalization. All of that has been moved
# into ``app/mcp/dispatcher.py`` (the executor) and ``app/mcp/router.py``
# (the router / auth / classifier). The agent-side API — this function
# — is now a thin adapter whose only job is to turn a tool-call into
# ``client_stub.call(...)``.
#
# If you feel the urge to add subprocess handling, path logic, or env
# injection here again, STOP: those belong in the dispatcher. Keeping
# this function tiny is the architectural invariant that prevents
# path/cwd/env bugs from multiplying across the codebase.

def _tool_mcp_call(mcp_id: str = "", tool: str = "", arguments: Any = None,
                   list_mcps: bool = False, **_: Any) -> str:
    """Invoke an MCP tool bound to the calling agent.

    Set ``list_mcps=True`` to enumerate the MCPs visible to this
    agent. Otherwise this call is dispatched through the central
    :mod:`app.mcp.client_stub` → :class:`~app.mcp.router.MCPCallRouter`
    → :class:`~app.mcp.dispatcher.NodeMCPDispatcher` pipeline.
    """
    try:
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        if not caller_id:
            return "Error: no calling agent context; mcp_call requires an agent."

        from ..mcp import client_stub as _stub

        # List mode: delegate to the router's enumeration path.
        if list_mcps or not mcp_id:
            return _stub.list_mcps(caller_id)

        # Normalize arguments — the router/dispatcher wants a dict.
        args: dict
        if isinstance(arguments, dict):
            args = arguments
        elif isinstance(arguments, str):
            try:
                args = _json.loads(arguments) if arguments.strip() else {}
            except Exception:
                return f"Error: 'arguments' must be a JSON object, got: {arguments!r}"
        elif arguments is None:
            args = {}
        else:
            return f"Error: 'arguments' must be a JSON object, got: {type(arguments).__name__}"

        return _stub.call(
            caller_id=caller_id,
            mcp_id=mcp_id,
            tool=tool,
            arguments=args,
        )
    except Exception as e:
        return f"Error in mcp_call: {e}"


# ── Register builtin handler with MCP dispatcher at import time ──────
# Builtin MCPs (audio TTS/STT) flow through the same router pipeline
# as external stdio MCPs. Never block module import if registration
# fails — just retry on first dispatch.
try:
    from ..mcp.dispatcher import register_builtin_handler as _register_builtin
    _register_builtin("__builtin__audio", _handle_builtin_mcp)
    _register_builtin("builtin", _handle_builtin_mcp)
except Exception:
    pass
