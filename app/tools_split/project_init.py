"""``/init`` equivalent — auto-generate PROJECT_CONTEXT.md for a project.

Borrowed from Claude Code's `/init` slash command: lets the agent
explore a project once at the start, distill the structure / tech
stack / entry points / conventions into a single markdown document,
and save it under the shared workspace so future agents can read it
in one shot instead of re-exploring from scratch every turn.

Implementation: spawn a subagent (similar to spawn_explore_subagent
but with WRITE permission) and hand it a structured prompt; the
subagent does the discovery work and writes the file. Idempotent —
re-running with force=False returns the existing path; force=True
re-generates.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# Tools the init subagent gets — read primitives + write_file +
# project_state / knowledge_lookup. Deliberately NOT include
# dispatch_task / submit_deliverable / update_milestone — init is
# READING + DOCUMENT GENERATION, not project state mutation.
_INIT_SUBAGENT_TOOLS: frozenset[str] = frozenset({
    "read_file", "search_files", "glob_files",
    "write_file",  # the one mutating tool — required to save the doc
    "project_state",
    "knowledge_lookup", "memory_recall",
    "get_skill_guide",
    "datetime_calc", "json_process", "text_process",
    "bash",  # for `git log -3` / `head -1 package.json` style probes
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


def _resolve_shared_root(project_id: str) -> str:
    """Resolve the canonical shared dir for a project. Mirrors the
    convention used by submit_deliverable.
    """
    try:
        from ..agent import Agent as _Agent
        return _Agent.get_shared_workspace_path(project_id)
    except Exception:
        return os.path.expanduser(
            f"~/.tudou_claw/workspaces/shared/{project_id}")


def _tool_init_project_context(
    project_id: str = "",
    force: bool = False,
    timeout_s: int = 300,
    extra_focus: str = "",
    **kwargs: Any,
) -> str:
    """Generate (or refresh) the project's ``PROJECT_CONTEXT.md`` file.

    project_id   — REQUIRED. The project to initialise.
    force        — Default False. If False and PROJECT_CONTEXT.md
                   already exists, return its path without
                   regenerating. Set True to overwrite.
    timeout_s    — Caller-side wait timeout (default 300s; clamped
                   to [60, 900]). Init is exploration-heavy so the
                   default is higher than spawn_explore_subagent.
    extra_focus  — Optional free-text instructing the init subagent
                   to pay extra attention to a particular area
                   ("focus on the auth flow" / "include a section on
                   the test framework" / etc.).

    The init subagent will:
      1. List the shared dir tree (top 2 levels).
      2. Read README.md / package.json / requirements.txt /
         pyproject.toml / Cargo.toml etc. if present.
      3. Probe the entry point file (best guess from above).
      4. Skim recent git log if the dir is a repo.
      5. Write a structured PROJECT_CONTEXT.md with sections:
         - Overview (1-2 sentences)
         - Tech stack (detected)
         - Directory layout (annotated tree)
         - Entry points / how to run
         - Conventions discovered (file naming, test layout, etc.)
         - Open milestones / goals (from project_state)
    """
    if not project_id or not project_id.strip():
        return "Error: 'project_id' is required for init_project_context."
    project_id = project_id.strip()
    caller_id = (kwargs.get("_caller_agent_id") or "").strip()
    parent = _get_caller_agent(caller_id)
    if parent is None:
        return ("Error: init_project_context requires a caller agent "
                "context (_caller_agent_id missing or hub not initialised).")

    # Idempotency check
    shared_root = _resolve_shared_root(project_id)
    target = os.path.join(shared_root, "PROJECT_CONTEXT.md")
    if os.path.exists(target) and not force:
        try:
            size = os.path.getsize(target)
        except OSError:
            size = 0
        return (f"PROJECT_CONTEXT.md already exists at {target} "
                f"({size} bytes). Pass force=true to regenerate.")

    # Make sure shared root exists (project may be brand new).
    try:
        os.makedirs(shared_root, exist_ok=True)
    except OSError as e:
        return f"Error: cannot create shared root {shared_root}: {e}"

    # Depth check (reuse the same invariant the rest of the spawn
    # primitives use).
    parent_depth = int(getattr(parent, "_delegate_depth", 0) or 0)
    max_depth = int(getattr(parent, "_max_delegate_depth", 3) or 3)
    if parent_depth >= max_depth:
        return (f"Error: subagent depth limit reached "
                f"({parent_depth}/{max_depth}). Cannot spawn init subagent.")

    try:
        timeout_s = max(60, min(int(timeout_s), 900))
    except Exception:
        timeout_s = 300

    # Spawn the init subagent (writing permission enabled, scoped to
    # the curated whitelist).
    try:
        from ..agent import Agent
    except Exception as e:
        return f"Error: cannot import Agent class: {e}"

    parent_sw = ""
    try:
        if hasattr(parent, "get_active_shared_workspace"):
            parent_sw = parent.get_active_shared_workspace() or ""
    except Exception:
        parent_sw = ""

    try:
        sub_agent = Agent(
            name=f"{parent.name}_init_{uuid.uuid4().hex[:6]}",
            role=parent.role,
            model=parent.model,
            provider=parent.provider,
            working_dir=parent.working_dir,
            shared_workspace=parent_sw or shared_root,
            system_prompt=parent.system_prompt,
            profile=parent.profile.__class__.from_dict(parent.profile.to_dict()),
            node_id=getattr(parent, "node_id", "") or "",
            parent_id=parent.id,
            authorized_workspaces=list(getattr(parent, "authorized_workspaces", []) or []),
        )
    except Exception as e:
        return f"Error: failed to spawn init subagent: {e}"

    sub_agent._delegate_depth = parent_depth + 1
    sub_agent._max_delegate_depth = max_depth
    try:
        sub_agent._cancellation_event = parent._cancellation_event
    except Exception:
        pass
    # Restrict subagent's tools to the init whitelist.
    try:
        sub_agent.profile.allowed_tools = sorted(_INIT_SUBAGENT_TOOLS)
    except Exception:
        pass

    # Track child for cancellation cascading.
    try:
        with parent._active_children_lock:
            parent._active_children.append((sub_agent.id, sub_agent))
    except Exception:
        pass

    extra_block = ""
    if extra_focus and extra_focus.strip():
        extra_block = (
            f"\n[Extra focus from caller]\n{extra_focus.strip()}\n"
        )

    prompt = (
        f"[init-project-context task | depth={sub_agent._delegate_depth}/"
        f"{sub_agent._max_delegate_depth}]\n"
        f"Generate the PROJECT_CONTEXT.md file for project_id={project_id}.\n"
        f"Target path: {target}\n"
        f"Shared root: {shared_root}\n\n"
        f"Steps to follow:\n"
        f"  1. glob_files / search_files to learn the directory layout under {shared_root}\n"
        f"  2. read_file the README / package.json / requirements.txt / pyproject.toml / Cargo.toml if any are present\n"
        f"  3. Identify the entry point (main.js / index.html / app.py / src/main.* / etc.) and read it\n"
        f"  4. If the project dir is a git repo, run `bash` with `git -C {shared_root} log --oneline -5` to learn recent activity\n"
        f"  5. Call project_state(scope='team') to get milestones / goals snapshot\n"
        f"  6. Write the PROJECT_CONTEXT.md with these sections (use markdown headings):\n"
        f"     # Project Context\n"
        f"     ## Overview      — 1-2 sentence what-is-this\n"
        f"     ## Tech stack    — detected languages / frameworks / build tools\n"
        f"     ## Directory layout — annotated tree (top 2 levels, brief one-liner per dir)\n"
        f"     ## Entry points — how to run / where execution starts\n"
        f"     ## Conventions  — file naming / test layout / commit style if discoverable\n"
        f"     ## Open milestones — bullet list from project_state, with owners\n"
        f"     ## Notes for future agents — gotchas you observed during this scan\n"
        f"  7. Use write_file with absolute path: {target}\n\n"
        f"Be concise — the whole document should be 600-1500 chars. "
        f"Do NOT submit_deliverable or update milestone status; "
        f"this is purely a documentation pass.{extra_block}"
    )

    started_at = time.time()
    result_text = ""
    error_msg = ""
    try:
        import threading
        result_holder: dict = {}

        def _run():
            try:
                result_holder["text"] = sub_agent.chat(prompt)
            except Exception as e:
                result_holder["error"] = repr(e)

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=timeout_s)
        if th.is_alive():
            try:
                sub_agent._cancellation_event.set()
            except Exception:
                pass
            error_msg = (f"init subagent exceeded timeout {timeout_s}s. "
                          "Cancelled.")
        elif "error" in result_holder:
            error_msg = f"init subagent crashed: {result_holder['error']}"
        else:
            result_text = (result_holder.get("text") or "").strip()
    finally:
        try:
            with parent._active_children_lock:
                parent._active_children = [
                    (cid, c) for (cid, c) in parent._active_children
                    if cid != sub_agent.id
                ]
        except Exception:
            pass

    elapsed = time.time() - started_at
    if error_msg:
        return f"Error: {error_msg} (after {elapsed:.0f}s)"

    # Verify the file was actually written.
    if not os.path.exists(target):
        return (f"⚠️ Subagent ran for {elapsed:.0f}s but PROJECT_CONTEXT.md "
                f"was not written. Subagent reply (first 400 chars):\n"
                f"{result_text[:400] if result_text else '(empty)'}")
    try:
        size = os.path.getsize(target)
    except OSError:
        size = 0
    return (
        f"✅ PROJECT_CONTEXT.md generated · {target}\n"
        f"size: {size} bytes · elapsed: {elapsed:.1f}s\n"
        f"Subagent reply summary (first 300 chars):\n"
        f"{result_text[:300] if result_text else '(empty)'}"
    )
