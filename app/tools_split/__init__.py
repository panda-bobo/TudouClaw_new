"""Per-tool module split (IN PROGRESS — not yet wired in).

Target layout
-------------
Each public tool lives in its own file:

    app/tools_split/read_file.py      → SCHEMA + handler for "read_file"
    app/tools_split/write_file.py     → SCHEMA + handler for "write_file"
    app/tools_split/bash.py           → SCHEMA + handler for "bash"
    ...
    app/tools_split/_core.py          → ToolRegistry, decorators, helpers
    app/tools_split/__init__.py       → aggregates SCHEMA & handlers,
                                          exports execute_tool / get_tool_definitions

Migration plan (pick up after M0-M4 is verified)
-----------------------------------------------
1. Freeze app/tools.py as app/tools_split/_legacy_impl.py (one big file).
2. Extract each _tool_* function to its own file, carrying its
   SCHEMA dict from the TOOLS list.
3. At the end of each new file, register via a decorator:

       @register("read_file", schema=SCHEMA)
       def handler(path: str, ...) -> str:
           ...

4. Drop the old TOOLS list + _TOOL_IMPL dict — they're re-built by
   walking the decorator registrations.
5. Switch app/tools.py to a thin shim:

       from .tools_split import *   # backward-compat re-export

6. Delete app/tools_split/_legacy_impl.py when nothing imports it.

Why not done in one shot
------------------------
34 tools × ~100 lines each × shared helpers (_get_hub,
_resolve_project, _push_audio_event, tool_result, tool_error, ...)
means a careful incremental migration preserves the 411-test
regression signal at every step. Doing all 34 in a single commit
risks quietly breaking one edge case and having to unwind.

Work each chat session:
    pick 3-5 related tools (e.g. file_ops batch) → extract →
    ``pytest tests/`` → commit. Resume next session.
"""

# Migration status (by category, not one-file-per-tool) — DONE.
# -------------------------------------------------------------
#   _common.py      — _get_hub (lazy hub import, shared by 4 modules)
#   fs.py           — read_file, write_file, edit_file, search_files,
#                     glob_files (5)
#   web.py          — web_search, web_fetch, web_screenshot, http_request (4)
#   system.py       — bash, pip_install, desktop_screenshot (3)
#   coordination.py — team_create, send_message, task_update (3)
#   project.py      — submit_deliverable, create_goal, update_goal_progress,
#                     create_milestone, update_milestone_status (5)
#                     + _get_current_scope, _resolve_project,
#                       _save_projects_silently helpers
#   data.py         — datetime_calc, json_process, text_process (3)
#   knowledge.py    — save_experience, knowledge_lookup, share_knowledge,
#                     learn_from_peers (4)
#   media.py        — create_pptx, create_pptx_advanced, create_video (3)
#   skills.py       — get_skill_guide, propose_skill, submit_skill (3)
#   mcp.py          — mcp_call + builtin TTS/STT handler + audio event
#                     queue (1 tool + 3 helpers)
#
# Total: 34 tools + 7 shared helpers.
#
# tools.py retains TOOL_DEFINITIONS (schemas) and _TOOL_FUNCS
# (dispatcher). Handlers and the helpers used by external modules
# (agent.py, agent_execution.py, portal REST) are re-exported from
# tools.py so ``app.tools._tool_read_file``, ``app.tools._get_hub``,
# ``app.tools.get_audio_events`` etc. keep working unchanged.
