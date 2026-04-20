"""
mcp_bridge — V2 adapter for ``app.mcp.manager`` + ``app.mcp.dispatcher``
(PRD §10.5.3).

Responsibilities:
    * Expose OpenAI-compatible tool schemas for every effective MCP of
      an agent (converting from MCP's inputSchema form).
    * Route a tool-name back to its owning MCPServerConfig and dispatch
      the call. The manager's cache is mcp_id → tools; the reverse index
      (tool_name → mcp_id) is built on the fly — fast enough for the
      V2 turn rate (~1 call/sec).
"""
from __future__ import annotations

from typing import Any

# V1 Layer-1 imports — explicitly allowed by isolation check (PRD §13.1).
from app.mcp import manager as _mgr
from app.mcp import dispatcher as _disp


_NODE_ID = "local"


# ── public API ─────────────────────────────────────────────────────────

def get_mcp_tools_for_agent(agent_v2_id: str) -> list[dict]:
    """Return OpenAI tool schemas for every MCP tool this agent can use."""
    out: list[dict] = []
    mgr = _mgr.get_mcp_manager()
    try:
        mcps = mgr.get_agent_effective_mcps(_NODE_ID, agent_v2_id)
    except Exception:
        return out

    for mcp in mcps:
        entry = mgr.tool_manifests.get(mcp.id)
        if entry is None or not getattr(entry, "tools", None):
            continue
        for raw in entry.tools:
            try:
                out.append(_mcp_tool_to_openai_schema(raw, mcp_id=mcp.id))
            except Exception:
                continue
    return out


def invoke_mcp(agent_v2_id: str, tool_name: str, args: dict) -> str:
    """Dispatch an MCP tool call; return result as string."""
    mgr = _mgr.get_mcp_manager()
    # Find which MCP owns ``tool_name`` among this agent's effective MCPs.
    try:
        mcps = mgr.get_agent_effective_mcps(_NODE_ID, agent_v2_id)
    except Exception as e:
        return f"[mcp_bridge] failed to list agent MCPs: {e}"

    target_config = None
    raw_tool_name = tool_name
    # V2 may prefix tool names with the mcp_id (e.g. "mcp_id::tool") in future;
    # support both the prefixed and bare form.
    if "::" in tool_name:
        wanted_mcp_id, raw_tool_name = tool_name.split("::", 1)
        target_config = next((m for m in mcps if m.id == wanted_mcp_id), None)
    else:
        for mcp in mcps:
            entry = mgr.tool_manifests.get(mcp.id)
            if entry and any(
                (t.get("name") == tool_name) for t in (entry.tools or [])
            ):
                target_config = mcp
                break

    if target_config is None:
        return f"[mcp_bridge] no MCP in agent {agent_v2_id} exposes tool {tool_name!r}"

    try:
        result = _disp.get_default_dispatcher().dispatch(
            target_config,
            raw_tool_name,
            dict(args or {}),
        )
    except Exception as e:
        return f"[mcp_bridge] dispatcher raised: {type(e).__name__}: {e}"

    if not getattr(result, "ok", False):
        msg = getattr(result, "error_message", "") or "unknown error"
        return f"[mcp_bridge] tool error: {msg}"

    content = getattr(result, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        import json
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def bindings_for(agent_v2_id: str, mcp_ids: list[str]) -> list[str]:
    """Return binding_ids for the agent's declared MCPs, ordered.

    Binding here is identified by the MCPServerConfig.id, which is the
    mcp_id after the manager has applied per-agent overrides (env,
    command, …). Unknown ids come back as empty strings.
    """
    mgr = _mgr.get_mcp_manager()
    try:
        mcps = mgr.get_agent_effective_mcps(_NODE_ID, agent_v2_id)
    except Exception:
        return ["" for _ in mcp_ids]
    by_id = {m.id: m.id for m in mcps}
    return [by_id.get(mid, "") for mid in mcp_ids]


# ── schema conversion ─────────────────────────────────────────────────

def _mcp_tool_to_openai_schema(raw: dict, *, mcp_id: str) -> dict:
    """Convert one MCP tool descriptor into an OpenAI function-tool schema.

    MCP tools shape: ``{"name": ..., "description": ..., "inputSchema": {...}}``
    where ``inputSchema`` is already a JSON-schema fragment. We pass it
    through as ``parameters`` with minimal normalization.
    """
    name = raw.get("name") or ""
    description = raw.get("description") or ""
    params = raw.get("inputSchema") or {"type": "object", "properties": {}}
    # Ensure it's a well-formed JSON-schema object.
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
            # Not part of OpenAI spec, but useful for routing / observability.
            "x-mcp-id": mcp_id,
        },
    }


__all__ = [
    "get_mcp_tools_for_agent",
    "invoke_mcp",
    "bindings_for",
]
