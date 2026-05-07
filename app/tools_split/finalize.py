"""Composite step-closure tool — atomic 'finalize_step'.

Borrowed from Claude Code's pattern of folding "find file → diff →
write → show patch" into a single ``Edit`` / ``Write`` tool: collapses
the typical 6-9 atomic-tool ritual at the end of a coder/researcher
step (write_file × N → submit_deliverable × N → plan_update →
update_milestone_status) into ONE LLM round-trip.

Why this matters
----------------
Without it the LLM has to:
  1. spend 4-9 tool calls in sequence, eating per-response budget
  2. re-derive paths / titles each time (LLM is bad at clerical work)
  3. emit text in between, which loses prefix-cache friendliness
  4. occasionally drift mid-sequence ("I wrote 3 files... let me read
     them again to verify..." → wandering loop)

With ``finalize_step`` the LLM emits a single intent ("close out step
S with deliverables A,B,C, mark milestone M done"), the BACKEND
executes the sequence deterministically, and the LLM gets one
structured result back. Same outcome, ~85% fewer tool calls.

Reuses existing primitives
--------------------------
This is intentionally a thin orchestrator over the already-tested
underlying tools — submit_deliverable, plan_update,
update_milestone_status. We don't reimplement file copying,
deliverable de-dup, milestone state-machine, etc. — those stay in
their own handlers. The composite is just a fixed sequencer.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _safe_call_tool(tool_name: str, args: dict) -> tuple[bool, str]:
    """Invoke an underlying tool through the central dispatcher and
    classify the result as ok/fail. ``Error`` prefix in the first 30
    chars (the convention every existing tool uses) means failure.
    """
    try:
        from .. import tools as _tools_mod
        result = _tools_mod.execute_tool(tool_name, args)
    except Exception as e:
        return False, f"[exception] {tool_name}: {e}"
    if not isinstance(result, str):
        # Some tools return dicts; stringify defensively.
        result = str(result)
    head = result[:60]
    failed = (
        head.lower().startswith("error")
        or head.startswith("DENIED")
        or "[exception]" in head
    )
    return (not failed), result


def _tool_finalize_step(
    files: list = None,
    step_id: str = "",
    milestone_id: str = "",
    step_summary: str = "",
    project_id: str = "",
    **_: Any,
) -> str:
    """Atomic step closure.

    files          — list of file specs to register as deliverables.
                     Each item is a dict with keys:
                       local_path  (REQUIRED) absolute path to the file
                                  (typically your write_file output).
                                  submit_deliverable copies it into the
                                  project shared dir automatically — no
                                  need to bash cp first.
                       title       (optional) short label; defaults to
                                  basename without extension.
                       kind        (optional) document|code|design|...
                                  Default: 'code'.
                       milestone_id (optional) per-file milestone link.
                                  Falls back to top-level milestone_id.
    step_id        — plan step to close on success. Empty = skip the
                     plan_update; finalize JUST the deliverables.
    milestone_id   — optional milestone to mark done after deliverables
                     register. Empty = skip update_milestone_status.
    step_summary   — short one-liner stamped on the closed step /
                     milestone evidence. If empty, auto-built from the
                     submitted titles.
    project_id     — optional; inferred from chat context if omitted.
    """
    # Argument validation
    if not isinstance(files, list) or not files:
        return (
            "Error: 'files' must be a non-empty list of "
            "{local_path, title?, kind?, milestone_id?}."
        )

    submitted: list[str] = []
    errors: list[str] = []

    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            errors.append(f"file[{idx}]: not a dict, got {type(f).__name__}")
            continue
        local_path = (f.get("local_path") or "").strip()
        if not local_path:
            errors.append(f"file[{idx}]: missing 'local_path'")
            continue
        title = (f.get("title") or "").strip()
        if not title:
            # Default title from basename without extension
            title = os.path.splitext(os.path.basename(local_path))[0] or f"deliverable_{idx + 1}"
        kind = (f.get("kind") or "code").strip()
        f_milestone = (f.get("milestone_id") or milestone_id or "").strip()

        ok, sub_result = _safe_call_tool("submit_deliverable", {
            "title": title,
            "file_path": local_path,
            "kind": kind,
            "milestone_id": f_milestone,
            "project_id": project_id,
        })
        if ok:
            submitted.append(title)
        else:
            errors.append(f"submit '{title}': {sub_result[:200]}")

    # Build a default summary if caller didn't provide one
    if not step_summary:
        if submitted:
            step_summary = f"Submitted {len(submitted)} deliverable(s): " + ", ".join(submitted[:5])
            if len(submitted) > 5:
                step_summary += f" (+{len(submitted) - 5} more)"
        else:
            step_summary = "Step closed (no deliverables submitted)"

    step_closed = False
    if step_id:
        ok, _step_result = _safe_call_tool("plan_update", {
            "action": "complete_step",
            "step_id": step_id,
            "result_summary": step_summary,
        })
        if ok:
            step_closed = True
        else:
            errors.append(f"plan_update step={step_id}: {_step_result[:200]}")

    milestone_closed = False
    if milestone_id:
        ok, _ms_result = _safe_call_tool("update_milestone_status", {
            "milestone_id": milestone_id,
            "status": "done",
            "evidence": step_summary,
            "project_id": project_id,
        })
        if ok:
            milestone_closed = True
        else:
            errors.append(f"milestone_update {milestone_id}: {_ms_result[:200]}")

    # Format the human-readable result
    lines: list[str] = []
    if submitted:
        lines.append(
            f"✅ Registered {len(submitted)} deliverable(s): "
            + ", ".join(submitted)
        )
    if step_closed:
        lines.append(f"✅ Step {step_id} closed")
    if milestone_closed:
        lines.append(f"✅ Milestone {milestone_id} marked done")
    if errors:
        lines.append("⚠️ Issues encountered:")
        for err in errors:
            lines.append(f"  - {err}")
    if not lines:
        return "No-op (no files submitted, no step / milestone closed)."

    return "\n".join(lines)
