"""Ephemeral subagent spawn — Claude-Code-style ``Task`` tool.

Lets a parent agent off-load a focused exploration / research task to
a stateless subagent. The subagent runs its own chat loop with its own
budget and context; the parent gets back the subagent's final reply
as a single string. The subagent's intermediate tool calls, file
reads, and reasoning chain stay isolated — they don't pollute the
parent's history or prefix cache.

Why this is its own primitive instead of "just call an existing
subagent system":
  - delegate_parallel exists but is parallel-list oriented and copies
    the parent's full tool surface. Spawn-explore is single-shot and
    deliberately read-only by default.
  - dispatch_task is FOR PEER agents (PM → coder), not for "fork
    yourself for a sub-question". It also writes to project state
    (creates a task record), which is wrong for ephemeral work.
  - The reason this matters: PM/orchestrator burns 20+ tool calls
    self-exploring big codebases when it should be answering "what
    does the team need to do next?". Off-loading the exploration to
    a subagent keeps the parent context lean.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# Read-only tool whitelist for subagents launched in default mode.
# Anything that mutates project state / dispatches tasks / writes
# files is excluded — explore subagents are FOR DISCOVERY, not for
# changes. If the parent really needs a writing subagent, set
# read_only_tools=False explicitly (still bounded by depth limit).
_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # File / search primitives
    "read_file", "search_files", "glob_files",
    # Web research
    "web_search", "web_fetch",
    # Project state lookup (read-only)
    "project_state",
    # Knowledge / memory recall (read paths only)
    "knowledge_lookup", "memory_recall",
    # Skill discovery
    "get_skill_guide",
    # Plan reflection
    "plan_update",
    # System info
    "datetime_calc", "json_process", "text_process",
    # Bash limited to read commands — sandbox layer enforces this
    # separately, but keeping bash out of the default subagent set
    # avoids "I'll just bash cat for everything" anti-pattern.
})


def _get_caller_agent(caller_id: str):
    if not caller_id:
        return None
    try:
        import sys as _sys
        _llm_mod = _sys.modules.get("app.llm")
        hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
        if hub is None:
            return None
        return hub.agents.get(caller_id)
    except Exception:
        return None


def _tool_spawn_explore_subagent(
    prompt: str = "",
    return_format: str = "summary",
    read_only_tools: bool = True,
    timeout_s: int = 180,
    role: str = "",
    **kwargs: Any,
) -> str:
    """Spawn a stateless subagent to handle a focused research /
    exploration task; return its final reply.

    prompt           — REQUIRED. The task for the subagent. Be specific:
                       "Find the file that defines the auth middleware,
                        list the @decorator entry points, and report
                        which export name is the public surface" is
                       MUCH better than "explore the auth code".
    return_format    — 'summary' (default, ≤500 chars), 'full', or
                       'list' (parsed bullet list). Hint to the subagent
                       in the prompt envelope.
    read_only_tools  — Default True. Subagent's allowed_tools are
                       restricted to read-only primitives (file/search/
                       web/project_state/knowledge/memory). Set False
                       only when you specifically want a writing fork.
    timeout_s        — Caller-side wait timeout (default 180s; clamped
                       to [10, 600]).
    role             — Optional role hint (default: inherit parent
                       role). Affects role-preset tool defaults if
                       read_only_tools=False.

    Output is the subagent's final assistant text, prefixed with a
    short metadata line showing tool calls used and elapsed time.
    """
    if not prompt or not prompt.strip():
        return "Error: 'prompt' is required for spawn_explore_subagent."
    caller_id = (kwargs.get("_caller_agent_id") or "").strip()
    parent = _get_caller_agent(caller_id)
    if parent is None:
        return ("Error: spawn_explore_subagent requires a caller agent "
                "context (_caller_agent_id missing or hub not initialised).")

    # Depth check — reuse the same delegate-depth invariant used by
    # delegate_parallel so a subagent can't recursively spawn forever.
    parent_depth = int(getattr(parent, "_delegate_depth", 0) or 0)
    max_depth = int(getattr(parent, "_max_delegate_depth", 3) or 3)
    if parent_depth >= max_depth:
        return (f"Error: subagent depth limit reached "
                f"({parent_depth}/{max_depth}). Parent agent already "
                f"forked enough — handle this branch directly.")

    try:
        timeout_s = max(10, min(int(timeout_s), 600))
    except Exception:
        timeout_s = 180

    # Build the subagent. Reuses delegate_parallel's spawn pattern.
    try:
        from ..agent import Agent
    except Exception as e:
        return f"Error: cannot import Agent class: {e}"

    sub_role = (role or "").strip() or parent.role
    parent_sw = ""
    try:
        if hasattr(parent, "get_active_shared_workspace"):
            parent_sw = parent.get_active_shared_workspace() or ""
    except Exception:
        parent_sw = ""

    try:
        sub_agent = Agent(
            name=f"{parent.name}_explore_{uuid.uuid4().hex[:6]}",
            role=sub_role,
            model=parent.model,
            provider=parent.provider,
            working_dir=parent.working_dir,
            shared_workspace=parent_sw,
            system_prompt=parent.system_prompt,
            profile=parent.profile.__class__.from_dict(parent.profile.to_dict()),
            node_id=getattr(parent, "node_id", "") or "",
            parent_id=parent.id,
            authorized_workspaces=list(getattr(parent, "authorized_workspaces", []) or []),
        )
    except Exception as e:
        return f"Error: failed to spawn subagent: {e}"

    sub_agent._delegate_depth = parent_depth + 1
    sub_agent._max_delegate_depth = max_depth
    # Inherit cancellation event so killing the parent's task also
    # tears down the subagent.
    try:
        sub_agent._cancellation_event = parent._cancellation_event
    except Exception:
        pass

    # Apply read-only tool restriction.
    if read_only_tools:
        # Combine inherited allowed_tools with read-only whitelist —
        # anything not on the whitelist is dropped.
        try:
            inherited = set(sub_agent.profile.allowed_tools or [])
            sub_agent.profile.allowed_tools = sorted(
                inherited & _READ_ONLY_TOOLS
            )
            # If inherited list was empty (rely on role preset), seed
            # explicitly with the whitelist so the role preset's
            # writing tools don't sneak in.
            if not sub_agent.profile.allowed_tools:
                sub_agent.profile.allowed_tools = sorted(_READ_ONLY_TOOLS)
        except Exception as e:
            logger.warning("subagent read-only enforcement failed: %s", e)

    # Track as active child so cancellation cascades work.
    try:
        with parent._active_children_lock:
            parent._active_children.append((sub_agent.id, sub_agent))
    except Exception:
        pass

    # Build the wrapped prompt — gives the subagent context + the
    # return-format constraint inline.
    fmt_hint = ""
    rf = (return_format or "summary").strip().lower()
    if rf == "summary":
        fmt_hint = ("Reply with a concise summary (≤500 chars). "
                     "Lead with the answer, then 1-2 lines of evidence.")
    elif rf == "list":
        fmt_hint = ("Reply with a bullet list. Each bullet a single "
                     "concrete fact discovered.")
    else:
        fmt_hint = "Reply with the full findings."
    wrapped = (
        f"[explore-subagent task | depth={sub_agent._delegate_depth}/"
        f"{sub_agent._max_delegate_depth} | read_only={read_only_tools}]\n"
        f"{prompt.strip()}\n\n"
        f"Format: {fmt_hint}"
    )

    # Run the subagent. We block the calling thread until it returns
    # or the caller-side timeout fires. Subagent's own tool budget is
    # what bounds its work, not this timeout (timeout is just a safety
    # net for genuinely hung calls).
    started_at = time.time()
    try:
        # Use a thread + timeout to enforce timeout_s. chat() can run
        # arbitrarily long otherwise.
        import threading
        result_holder: dict = {}

        def _run():
            try:
                result_holder["text"] = sub_agent.chat(wrapped)
            except Exception as e:
                result_holder["error"] = repr(e)

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            # Best-effort cancel — flip the event the subagent reads
            # in its abort_check. Subagent eventually winds down.
            try:
                sub_agent._cancellation_event.set()
            except Exception:
                pass
            elapsed = time.time() - started_at
            return (f"Error: subagent exceeded timeout {timeout_s}s "
                    f"(elapsed {elapsed:.0f}s). Cancelled.")
        elapsed = time.time() - started_at
        if "error" in result_holder:
            return f"Error: subagent crashed: {result_holder['error']}"
        text = (result_holder.get("text") or "").strip()
    finally:
        # Detach from parent's active children list.
        try:
            with parent._active_children_lock:
                parent._active_children = [
                    (cid, c) for (cid, c) in parent._active_children
                    if cid != sub_agent.id
                ]
        except Exception:
            pass

    # Subagent reply with a small metadata header so the parent LLM
    # sees the cost of this fork.
    tool_calls_used = 0
    try:
        events = getattr(sub_agent, "events", []) or []
        tool_calls_used = sum(
            1 for e in events
            if getattr(e, "kind", "") == "tool_call"
        )
    except Exception:
        pass
    header = (
        f"[subagent reply · elapsed {elapsed:.1f}s · "
        f"tool_calls={tool_calls_used}]"
    )
    return f"{header}\n{text}" if text else f"{header}\n(empty reply)"
