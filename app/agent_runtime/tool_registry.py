"""Tool registry — converts TudouClaw's tools.py registry to SDK
``FunctionTool`` objects (the raw-schema form).

Two-stage filter (matches legacy chat loop):
  1. TudouClaw's ``_get_effective_tools()`` — applies role preset +
     allowed_tools + denied_tools + global denylist + capability
     skill grants
  2. B's intent-based opt-in (``user_explicitly_requests_retrieval``
     / ``user_explicitly_requests_wiki_write``) — strips
     knowledge_lookup / memory_recall / wiki_ingest unless the user
     message contains explicit phrasing

Each surviving TudouClaw tool spec gets wrapped as an SDK
``FunctionTool``. The wrapper:
  - Carries the same JSON schema TudouClaw advertises to the LLM
  - On invoke, dispatches via TudouClaw's ``tools.tool_registry``
    (so we don't reimplement what each tool does)
  - Returns the result string in SDK's expected shape

Lazy SDK import (top of module so ``import app.agent_runtime``
stays clean even without openai-agents installed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def build_sdk_tools(tudou_agent, user_message: Any) -> List[Any]:
    """Return a list of SDK FunctionTool objects for this turn.

    Args:
      tudou_agent: app.agent.Agent instance
      user_message: the inbound user text (used by intent gate)

    Returns:
      List of SDK ``FunctionTool`` objects ready to attach to a
      ``agents.Agent(tools=...)``.

    Returns [] if the SDK isn't installed (caller should already
    have raised before reaching here, but defensive guard).
    """
    try:
        from agents import FunctionTool
    except ImportError:
        return []

    # ── Step 1: TudouClaw's effective tools (role + permissions) ──
    try:
        legacy_specs = tudou_agent._get_effective_tools() or []
    except Exception as e:
        logger.warning("_get_effective_tools failed: %s", e)
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
    except Exception as e:
        logger.debug("opt-in filter skipped: %s", e)

    # ── Step 3: wrap each legacy spec as an SDK FunctionTool ──────
    sdk_tools: List[Any] = []
    for spec in legacy_specs:
        fn_def = spec.get("function") or {}
        name = fn_def.get("name") or ""
        description = fn_def.get("description") or ""
        # OpenAI tool schema → JSON Schema (the SDK accepts the
        # same shape that TudouClaw advertises today).
        params_schema = fn_def.get("parameters") or {
            "type": "object", "properties": {}, "additionalProperties": False
        }

        if not name:
            continue

        # ``on_invoke_tool`` must be an async callable that takes
        # (ctx, args_json_str). We make a closure capturing the
        # tool name so dispatch goes to the right handler.
        async def _invoke(ctx: Any, args_json: str, _name=name):
            return _dispatch_tudou_tool(tudou_agent, _name, args_json)

        try:
            tool = FunctionTool(
                name=name,
                description=description[:1000],  # SDK has a soft cap
                params_json_schema=params_schema,
                on_invoke_tool=_invoke,
                strict_json_schema=False,  # weak models emit slightly
                                           # malformed args; be lenient
            )
            sdk_tools.append(tool)
        except Exception as e:
            logger.warning(
                "skipping tool %r — FunctionTool construction failed: %s",
                name, e)

    logger.info(
        "tool_registry: built %d SDK function_tool(s) for agent=%s "
        "(legacy=%d, opt-in deny=%s)",
        len(sdk_tools),
        getattr(tudou_agent, "id", "?")[:8],
        len(legacy_specs),
        sorted(opt_in_deny) if 'opt_in_deny' in dir() else [],
    )
    return sdk_tools


def _dispatch_tudou_tool(tudou_agent, tool_name: str, args_json: str) -> str:
    """Bridge: call TudouClaw's tool dispatcher with parsed args,
    return the string result the SDK passes back to the LLM.

    Args:
      tudou_agent: the live Agent instance (some tools need access
                   to it via the dispatcher's hub lookup)
      tool_name: the canonical TudouClaw tool name
      args_json: JSON-encoded arguments dict from the SDK

    Returns:
      The tool's result as a string. Errors are caught and stringified
      so the LLM sees a clear error message instead of an SDK crash.
    """
    try:
        args = json.loads(args_json) if args_json else {}
        if not isinstance(args, dict):
            args = {"_raw": args}
    except Exception as e:
        return f"Error: invalid JSON args for {tool_name}: {e}"

    # Inject the caller agent ID so tools that rely on hub-based
    # dispatch (memory_recall, knowledge_lookup, etc.) can resolve
    # the right agent context.
    args.setdefault("_caller_agent_id",
                    getattr(tudou_agent, "id", ""))

    try:
        from app import tools as _tools
        result = _tools.tool_registry.dispatch(tool_name, args)
        # tool_registry.dispatch returns a string; ensure it's
        # actually a string for the SDK.
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return result
    except Exception as e:
        logger.warning(
            "tudou tool dispatch failed: name=%s err=%s",
            tool_name, e)
        return f"Error executing {tool_name}: {e}"
