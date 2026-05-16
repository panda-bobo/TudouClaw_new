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

    # ── Step 2: B's intent-based opt-in → SOFT-DENY (2026-05-16) ──
    # Was: strip tools from the SDK tool list entirely. Legacy could
    # tolerate that — tool_registry.dispatch returns an error string
    # for unknown tools, the LLM adapts. SDK is STRICTER: it validates
    # tool names against the registered list BEFORE dispatch and
    # raises ModelBehaviorError("Tool X not found"), aborting the run.
    #
    # Now: keep the tools registered, but mark them ``opt_in_denied``
    # so the _invoke closure (below) intercepts the call and returns
    # a friendly skip message instead of running the real handler.
    user_text = user_message if isinstance(user_message, str) else ""
    opt_in_deny: set = set()
    try:
        from app.runtime import (
            user_explicitly_requests_retrieval,
            user_explicitly_requests_wiki_write,
        )
        if not user_explicitly_requests_retrieval(user_text):
            opt_in_deny.update({"knowledge_lookup", "memory_recall"})
        if not user_explicitly_requests_wiki_write(user_text):
            opt_in_deny.add("wiki_ingest")
    except Exception as e:
        logger.debug("opt-in gate skipped: %s", e)

    # ── Step 2b: prompt-mentioned-but-ungranted tools (2026-05-16) ──
    # System prompts in app/agent_llm.py / meeting.py / project.py /
    # agent.py hardcode references to certain "infra" tool names
    # (mcp_call, memory_recall, save_experience, etc.) regardless of
    # whether the specific agent role has been granted them. This
    # creates the SAME ModelBehaviorError class of bug as the opt-in
    # path above: LLM reads "you can use mcp_call" in its prompt,
    # tries to call it, SDK aborts because the tool isn't in the
    # registered list for THIS agent.
    #
    # Fix: register every "infra-mentioned" tool as a STUB if it's
    # not in legacy_specs already. Stub returns a clean error string
    # when invoked. SDK no longer aborts; LLM sees the error message
    # and self-corrects.
    _INFRA_MENTIONED_TOOLS = {
        # Tools the system prompts mention by name across roles
        "mcp_call":         "MCP tool invocation",
        "memory_recall":    "L3 memory query",
        "knowledge_lookup": "Knowledge base search",
        "wiki_ingest":      "Wiki write",
        "save_experience":  "Save learning to experience library",
        "web_fetch":        "Fetch a URL",
        "web_search":       "Search the web",
    }
    _granted_names = {
        (s.get("function") or {}).get("name", "")
        for s in legacy_specs
    }
    _prompt_only_stubs = []
    for _name, _desc in _INFRA_MENTIONED_TOOLS.items():
        if _name and _name not in _granted_names:
            _prompt_only_stubs.append({
                "type": "function",
                "function": {
                    "name": _name,
                    "description": _desc + " (NOT AUTHORIZED for this agent — calling returns error)",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": True,
                    },
                },
                "_is_unauthorized_stub": True,
            })
    if _prompt_only_stubs:
        legacy_specs = list(legacy_specs) + _prompt_only_stubs
        # Also mark these as soft-denied so _invoke returns error
        # without trying to dispatch (the real handler doesn't exist
        # for this agent's grant).
        for s in _prompt_only_stubs:
            opt_in_deny.add(s["function"]["name"])
        logger.debug(
            "tool_registry: registered %d unauthorized-stub(s) so "
            "SDK doesn't ModelBehaviorError on prompt-mentioned-but-"
            "ungranted tools (agent=%s, stubs=%s)",
            len(_prompt_only_stubs),
            getattr(tudou_agent, "id", "?")[:8],
            [s["function"]["name"] for s in _prompt_only_stubs])

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
        #
        # 2026-05-16: TudouClaw tools (bash, write_file, edit_file,
        # mcp_call, etc.) are SYNCHRONOUS and can block for seconds
        # to minutes. Running them inline in the SDK's async event
        # loop freezes the loop — the SDK's stream then can't send
        # tool results back to the LLM in time, leaving an orphan
        # asst.tool_calls without matching tool results, which the
        # NEXT LLM call rejects with 400 "insufficient tool
        # messages following tool_calls". Fix: dispatch in a thread
        # via asyncio.to_thread so the sync tool runs without
        # blocking the loop.
        async def _invoke(ctx: Any, args_json: str, _name=name,
                          _deny=opt_in_deny):
            import asyncio as _asyncio
            # ── Soft-deny intercept (2026-05-16) ─────────────────
            # If denied (intent gate OR unauthorized-stub), return
            # CLEAN ERROR string. SDK still gets a tool result so
            # the run continues; LLM sees the error and self-corrects.
            #
            # Two paths land here:
            #   1. Intent-gated: memory_recall/knowledge_lookup/wiki_ingest
            #      stripped because user message lacked trigger phrases
            #   2. Unauthorized-stub: tool mentioned in system prompt
            #      but not in agent's effective tool grants (e.g. coder
            #      role doesn't get mcp_call but prompt talks about it)
            #
            # Without this stub, SDK aborts with ModelBehaviorError on
            # case 2 — fatal. With it, LLM just sees a polite error
            # and tries a different approach.
            if _name in _deny:
                logger.info(
                    "SDK tool denied: %s — returning Error string "
                    "(LLM will see + self-correct)", _name)
                return (
                    f"Error: tool '{_name}' is not authorized for "
                    f"this agent. Use a different tool or skip this "
                    f"step."
                )
            try:
                logger.info(
                    "SDK tool call: %s args=%s",
                    _name, (args_json or "")[:100])
                result = await _asyncio.to_thread(
                    _dispatch_tudou_tool,
                    tudou_agent, _name, args_json)
                logger.info(
                    "SDK tool result: %s len=%d preview=%s",
                    _name, len(result or ""), (result or "")[:100])
                return result
            except Exception as e:
                # MUST return a string, never raise — otherwise the
                # SDK won't add a tool result for this call and the
                # next LLM call will hit the orphan-tool-calls 400.
                logger.warning(
                    "SDK tool wrapper error %s: %s — returning "
                    "error string to keep tool/result pairing",
                    _name, e)
                return f"Error in {_name}: {type(e).__name__}: {e}"

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
