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
    **ctx: Any,
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

    # ── Self-evolution Phase 1 (2026-05-08): wiki outcome write-back ──
    # If this step closure succeeded AND the calling agent had wiki
    # hits in its lookup_trace, credit each hit with success_count++.
    # Cheap (file write per hit, ≤50 max) and zero LLM cost — purely
    # the data side of "experiences accumulate authority by repeat
    # successful application". Skipped on outright failure (no
    # submitted deliverables AND no step closed AND errors present).
    step_succeeded = bool(submitted or step_closed or milestone_closed) \
        and not errors
    wiki_credited = 0
    if step_succeeded:
        try:
            agent_id = ctx.get("_caller_agent_id", "") or ""
            if agent_id:
                from ..hub import get_hub
                _hub_ref = get_hub()
                _agent_ref = _hub_ref.get_agent(agent_id) if _hub_ref else None
                if _agent_ref is not None and hasattr(
                    _agent_ref, "consume_lookup_trace"
                ):
                    _trace = _agent_ref.consume_lookup_trace()
                    if _trace:
                        from ..knowledge.wiki_store import get_wiki_store
                        _ws = get_wiki_store()
                        for _rec in _trace:
                            try:
                                _result = _ws.update_outcome(
                                    scope=_rec.get("scope", ""),
                                    kind=_rec.get("kind", ""),
                                    slug=_rec.get("slug", ""),
                                    success=True,
                                )
                                if _result is not None:
                                    wiki_credited += 1
                            except Exception as _ue:
                                logger.debug(
                                    "wiki update_outcome skipped for "
                                    "%s: %s", _rec, _ue,
                                )
        except Exception as _wb_err:
            # Never let an outcome-tracking error break finalize_step.
            logger.debug("wiki outcome writeback skipped: %s", _wb_err)

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
    if wiki_credited:
        lines.append(
            f"📚 Credited {wiki_credited} wiki experience(s) "
            f"(success_count++)"
        )
    if errors:
        lines.append("⚠️ Issues encountered:")
        for err in errors:
            lines.append(f"  - {err}")
    if not lines:
        return "No-op (no files submitted, no step / milestone closed)."

    return "\n".join(lines)


# ── submit_review ─────────────────────────────────────────────────────
# Reviewer's standard ritual: write review report + register it as a
# deliverable + batch-report any issues found + transition the
# milestone based on the decision. Replaces the 8-12 atomic call ritual
# (read_file × N + write report + submit_deliverable + report_issue × M
# + update_milestone_status) with a single call.

# Decision → milestone target status mapping. Conservative choices:
# request_changes parks the milestone at "blocked" so the responsible
# agent has to address evidence; reject leaves it cancelled. Approved
# milestones go to done.
_REVIEW_DECISION_STATUS = {
    "approve": "done",
    "approved": "done",
    "request_changes": "blocked",
    "changes_requested": "blocked",
    "needs_changes": "blocked",
    "reject": "cancelled",
    "rejected": "cancelled",
}


def _tool_submit_review(
    milestone_id: str = "",
    decision: str = "",
    summary: str = "",
    issues: list = None,
    deliverable_path: str = "",
    deliverable_title: str = "",
    deliverable_content: str = "",
    project_id: str = "",
    **_: Any,
) -> str:
    """Atomic review closure for a milestone.

    milestone_id        — REQUIRED. The milestone being reviewed.
    decision            — REQUIRED. One of: approve | request_changes |
                          reject. Maps to milestone status (done /
                          blocked / cancelled respectively).
    summary             — Short review summary (≤200 chars recommended).
                          Used as evidence on the milestone status
                          update + as the deliverable title fallback.
    issues              — Optional list of issues to file alongside
                          the review. Each item is a dict:
                            {title, severity?, description?, milestone_id?}
                          severity defaults to "medium". milestone_id
                          defaults to the top-level milestone_id so
                          issues are auto-linked.
    deliverable_path    — Optional path to a pre-written review report.
                          Submitted as a deliverable kind='analysis'.
                          submit_deliverable copies it into the shared
                          dir if it lives elsewhere.
    deliverable_title   — Title for the review-report deliverable.
                          Defaults to "Review · <milestone_id>".
    deliverable_content — If provided WITHOUT deliverable_path, the
                          content is written into the shared dir
                          automatically (uses submit_deliverable's
                          content_text path).
    project_id          — Optional; inferred from chat context.
    """
    if not milestone_id:
        return "Error: 'milestone_id' is required for submit_review."
    decision_norm = (decision or "").strip().lower()
    target_status = _REVIEW_DECISION_STATUS.get(decision_norm)
    if not target_status:
        return (
            "Error: 'decision' must be one of approve / request_changes / reject. "
            f"Got: {decision!r}"
        )

    summary_clean = (summary or "").strip()
    if not summary_clean:
        summary_clean = f"Review decision: {decision_norm}"
    submitted_review = False
    issues_filed: list[str] = []
    errors: list[str] = []

    # 1. Submit the review report as a deliverable (if any provided).
    if deliverable_path or deliverable_content:
        title = (deliverable_title or "").strip() or f"Review · {milestone_id}"
        sub_args = {
            "title": title,
            "kind": "analysis",
            "milestone_id": milestone_id,
            "project_id": project_id,
        }
        if deliverable_path:
            sub_args["file_path"] = deliverable_path
        if deliverable_content and not deliverable_path:
            sub_args["content_text"] = deliverable_content
        ok, sub_result = _safe_call_tool("submit_deliverable", sub_args)
        if ok:
            submitted_review = True
        else:
            errors.append(f"submit review report: {sub_result[:200]}")

    # 2. File each issue (if any). Issues default to severity=medium and
    # auto-link to this milestone.
    for idx, issue in enumerate(issues or []):
        if not isinstance(issue, dict):
            errors.append(f"issue[{idx}]: not a dict")
            continue
        ititle = (issue.get("title") or "").strip()
        if not ititle:
            errors.append(f"issue[{idx}]: missing 'title'")
            continue
        severity = (issue.get("severity") or "medium").strip().lower()
        idesc = (issue.get("description") or "").strip()
        i_milestone = (issue.get("milestone_id") or milestone_id).strip()
        ok, ires = _safe_call_tool("report_issue", {
            "title": ititle,
            "description": idesc,
            "severity": severity,
            "milestone_id": i_milestone,
            "project_id": project_id,
        })
        if ok:
            issues_filed.append(ititle)
        else:
            errors.append(f"issue '{ititle}': {ires[:200]}")

    # 3. Transition the milestone to the decided status.
    milestone_updated = False
    ms_evidence = summary_clean
    if issues_filed:
        ms_evidence += f" (filed {len(issues_filed)} issue(s))"
    ok, ms_result = _safe_call_tool("update_milestone_status", {
        "milestone_id": milestone_id,
        "status": target_status,
        "evidence": ms_evidence,
        "project_id": project_id,
    })
    if ok:
        milestone_updated = True
    else:
        errors.append(f"milestone status update: {ms_result[:200]}")

    # Format result
    lines: list[str] = []
    lines.append(f"📋 Review decision: **{decision_norm}** → milestone status `{target_status}`")
    if submitted_review:
        lines.append("✅ Review report registered as deliverable")
    if issues_filed:
        lines.append(f"✅ Filed {len(issues_filed)} issue(s): " + ", ".join(issues_filed[:5])
                      + (f" (+{len(issues_filed) - 5} more)" if len(issues_filed) > 5 else ""))
    if milestone_updated:
        lines.append(f"✅ Milestone {milestone_id} → {target_status}")
    if errors:
        lines.append("⚠️ Issues encountered:")
        for err in errors:
            lines.append(f"  - {err}")
    return "\n".join(lines)


# ── bootstrap_project ─────────────────────────────────────────────────
# PM's "first-day ritual" — define the blueprint, create milestones,
# create goals, dispatch initial tasks. Currently 15+ separate tool
# calls; this folds them into a single intent.

def _tool_bootstrap_project(
    project_id: str = "",
    blueprint: dict = None,
    milestones: list = None,
    goals: list = None,
    tasks: list = None,
    revision_note: str = "",
    **_: Any,
) -> str:
    """One-shot project skeleton creation.

    project_id     — REQUIRED. Project to bootstrap.
    blueprint      — Optional dict passed straight through to
                     define_project_blueprint. Keys: folders, acceptance,
                     no_glob_in_chat, tool_budget_per_turn.
    milestones     — Optional list of milestone dicts:
                       {name, responsible_agent_id?, description?, due_date?}
    goals          — Optional list of goal dicts:
                       {name, description?, metric?, target_value?,
                        target_text?, owner_agent_id?}
    tasks          — Optional list of task-dispatch dicts:
                       {title, assigned_to, milestone_id?, description?,
                        priority?, due_date?, llm_label?}
    revision_note  — Audit-trail note (forwarded to define_project_blueprint).

    Each list section is independent: passing only goals (or only tasks)
    works fine. All sections are best-effort — partial failure is
    reported, not aborted.
    """
    if not project_id:
        return "Error: 'project_id' is required for bootstrap_project."

    blueprint_done = False
    milestones_created: list[str] = []
    goals_created: list[str] = []
    tasks_dispatched: list[str] = []
    errors: list[str] = []

    # 1. Blueprint (if provided) — generates rules + sets folder/acceptance contracts.
    if isinstance(blueprint, dict) and blueprint:
        bp_args = {"project_id": project_id, "revision_note": revision_note}
        for k in ("folders", "acceptance", "no_glob_in_chat", "tool_budget_per_turn"):
            if k in blueprint:
                bp_args[k] = blueprint[k]
        ok, bp_result = _safe_call_tool("define_project_blueprint", bp_args)
        if ok:
            blueprint_done = True
        else:
            errors.append(f"blueprint: {bp_result[:200]}")

    # 2. Milestones — pass through with best-effort responsible_agent_id.
    for idx, ms in enumerate(milestones or []):
        if not isinstance(ms, dict):
            errors.append(f"milestone[{idx}]: not a dict")
            continue
        name = (ms.get("name") or "").strip()
        if not name:
            errors.append(f"milestone[{idx}]: missing 'name'")
            continue
        ms_args = {"name": name, "project_id": project_id}
        for k in ("responsible_agent_id", "description", "due_date"):
            v = ms.get(k)
            if v:
                ms_args[k] = v
        ok, ms_result = _safe_call_tool("create_milestone", ms_args)
        if ok:
            milestones_created.append(name)
        else:
            errors.append(f"milestone '{name}': {ms_result[:200]}")

    # 3. Goals — count/percent/text metrics handled by create_goal itself.
    for idx, g in enumerate(goals or []):
        if not isinstance(g, dict):
            errors.append(f"goal[{idx}]: not a dict")
            continue
        gname = (g.get("name") or "").strip()
        if not gname:
            errors.append(f"goal[{idx}]: missing 'name'")
            continue
        g_args = {"name": gname, "project_id": project_id}
        for k in ("description", "metric", "target_value",
                   "target_text", "owner_agent_id"):
            v = g.get(k)
            if v is not None and v != "":
                g_args[k] = v
        ok, g_result = _safe_call_tool("create_goal", g_args)
        if ok:
            goals_created.append(gname)
        else:
            errors.append(f"goal '{gname}': {g_result[:200]}")

    # 4. Initial task dispatch — assigns each task to a teammate. The
    # dispatch_task tool also fires the @-mention notification path so
    # the assigned agent picks up the work.
    for idx, t in enumerate(tasks or []):
        if not isinstance(t, dict):
            errors.append(f"task[{idx}]: not a dict")
            continue
        tt = (t.get("title") or "").strip()
        assigned_to = (t.get("assigned_to") or "").strip()
        if not tt:
            errors.append(f"task[{idx}]: missing 'title'")
            continue
        if not assigned_to:
            errors.append(f"task '{tt}': missing 'assigned_to' agent_id")
            continue
        t_args = {
            "title": tt,
            "assigned_to": assigned_to,
            "project_id": project_id,
        }
        for k in ("milestone_id", "description", "priority",
                   "due_date", "llm_label"):
            v = t.get(k)
            if v is not None and v != "":
                t_args[k] = v
        ok, t_result = _safe_call_tool("dispatch_task", t_args)
        if ok:
            tasks_dispatched.append(tt)
        else:
            errors.append(f"task '{tt}': {t_result[:200]}")

    # Format result
    lines: list[str] = [f"🚀 Project {project_id} bootstrap:"]
    if blueprint_done:
        lines.append("✅ Blueprint registered")
    if milestones_created:
        lines.append(f"✅ Created {len(milestones_created)} milestone(s): "
                      + ", ".join(milestones_created[:5])
                      + (f" (+{len(milestones_created) - 5})"
                         if len(milestones_created) > 5 else ""))
    if goals_created:
        lines.append(f"✅ Created {len(goals_created)} goal(s): "
                      + ", ".join(goals_created[:5])
                      + (f" (+{len(goals_created) - 5})"
                         if len(goals_created) > 5 else ""))
    if tasks_dispatched:
        lines.append(f"✅ Dispatched {len(tasks_dispatched)} task(s): "
                      + ", ".join(tasks_dispatched[:5])
                      + (f" (+{len(tasks_dispatched) - 5})"
                         if len(tasks_dispatched) > 5 else ""))
    if errors:
        lines.append("⚠️ Issues encountered:")
        for err in errors:
            lines.append(f"  - {err}")
    if len(lines) == 1:
        return "Error: nothing to bootstrap — provide blueprint / milestones / goals / tasks."
    return "\n".join(lines)
