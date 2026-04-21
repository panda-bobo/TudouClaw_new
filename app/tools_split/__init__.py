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

# Migration status
# ----------------
# Batch 1 (filesystem-read tools) — DONE:
#     read_file     → tools_split/read_file.py
#     search_files  → tools_split/search_files.py
#     glob_files    → tools_split/glob_files.py
#
# tools.py still owns the TOOL_DEFINITIONS schema list and the
# _TOOL_FUNCS dispatcher. Handlers are now re-exported from this
# package so downstream imports (app.tools._tool_read_file, etc.)
# keep working unchanged.
#
# Next batches (not yet started):
#     write_file / edit_file / bash — filesystem-write + exec (batch 2)
#     web_search / web_fetch / web_screenshot / http_request (batch 3)
#     team_create / send_message / task_update + project tools (batch 4)
