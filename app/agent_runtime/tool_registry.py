"""Tool registry — converts TudouClaw's tools.py registry to SDK
``@function_tool`` decorated callables.

Two-stage filter (matches legacy chat loop):
  1. TudouClaw's ``_get_effective_tools()`` — applies role preset +
     allowed_tools + denied_tools + global denylist + capability
     skill grants
  2. B's intent-based opt-in (``user_explicitly_requests_retrieval``
     / ``user_explicitly_requests_wiki_write``) — strips
     knowledge_lookup / memory_recall / wiki_ingest unless the user
     message contains explicit phrasing

Each surviving TudouClaw tool spec gets wrapped as an SDK
``function_tool``. The wrapper:
  - Forwards the call to the legacy TudouClaw tool dispatcher
    (so we don't reimplement what the tool does)
  - Returns the result in the SDK's expected shape

Lazy SDK import (top of module).
"""
from __future__ import annotations

from typing import Any, List


def build_sdk_tools(tudou_agent, user_message: Any) -> List[Any]:
    """Return a list of SDK function_tool objects for this turn.

    Args:
      tudou_agent: app.agent.Agent instance
      user_message: the inbound user text (used by intent gate)

    Returns:
      List of SDK function_tool objects ready to attach to a
      ``agents.Agent(tools=...)``.

    Raises:
      Nothing on missing SDK — returns []. The caller (sdk_adapter)
      already raised SDKNotInstalledError before reaching here.
    """
    try:
        from agents import function_tool  # noqa: F401
    except ImportError:
        return []

    # ── Step 1: TudouClaw's effective tools (role + permissions) ──
    try:
        legacy_specs = tudou_agent._get_effective_tools() or []
    except Exception:
        legacy_specs = []

    # ── Step 2: B's intent-based opt-in (same as A path) ──────────
    user_text = user_message if isinstance(user_message, str) else ""
    try:
        from app.runtime import (
            user_explicitly_requests_retrieval,
            user_explicitly_requests_wiki_write,
        )
        opt_in_deny: set = set()
        if not user_explicitly_requests_retrieval(user_text):
            opt_in_deny.update({"knowledge_lookup", "memory_recall"})
        if not user_explicitly_requests_wiki_write(user_text):
            opt_in_deny.add("wiki_ingest")

        if opt_in_deny:
            filtered = [
                t for t in legacy_specs
                if (t.get("function", {}).get("name", "") or "")
                not in opt_in_deny
            ]
            # Safety: never empty the tool set (same gate as A)
            if filtered:
                legacy_specs = filtered
    except Exception:
        pass

    # ── Step 3: wrap each legacy spec as an SDK function_tool ─────
    # TODO Phase 1: real implementation. Each TudouClaw tool spec
    # has shape:
    #   {"type": "function", "function": {
    #       "name": "...", "description": "...", "parameters": {...}
    #   }}
    # The SDK function_tool decorator wants a Python callable with
    # type-annotated args + docstring. We need to:
    #   1. Generate a Python wrapper from the JSON-schema parameters
    #   2. The wrapper, when called, dispatches via TudouClaw's
    #      tool runner (app/tools.py:_handle_tool_call equivalent)
    #
    # The SDK also accepts a "raw schema" form via the ToolSchema
    # mechanism — that's the simpler PoC path. Once we validate
    # SDK ↔ mimo round-trip works, fill this in.
    #
    # For PoC scaffold: return [] so the SDK Agent has no tools.
    # That's wrong for real use but lets us validate plumbing.
    return []
