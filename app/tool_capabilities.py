"""Tool-tier classification: core vs capability-skill-gated.

Problem this module solves
--------------------------
Before this refactor every TOOL_DEFINITIONS entry was sent to every
agent's LLM regardless of what the agent was supposed to do. A fresh
meeting turn burned ~22k input tokens, most of it tool schemas the
agent would never call. User observation: "项目管理 那 5 个工具没授
权给 agent 啊" — confirmed: no gating layer existed.

Design
------
Three tiers, enforced by ``filter_tools_by_capability``:

  CORE                     always shipped to every agent's LLM
                           (agent identity / basic filesystem / web /
                            memory / scheduler / MCP bridge). Hardcoded.

  GLOBAL DEFAULT CAPS      admin-editable list of capability skills that
                           every agent gets implicitly. Admins set this
                           in ~/.tudou_claw/capability_defaults.json or
                           via Portal UI.

  PER-AGENT GRANTS         ``agent.granted_skills``. Extra capabilities
                           on top of the global defaults for this
                           specific agent.

Effective capabilities for an agent = GLOBAL_DEFAULTS ∪ granted_skills.
A tool ships iff it is CORE or its gating capability is in that set.

Why the two-layer (global + per-agent) split
--------------------------------------------
Without a global layer admins would have to toggle the same capability
on every single agent one-by-one. Without a per-agent layer the
one-off "only my PM agent should have project-management" cases
become impossible. Both knobs make sense; this module exposes both.

Why not map to existing workflow skill IDs
------------------------------------------
Workflow skills ("test-driven-development", "brainstorming") are
methodology docs, not capability unlocks — agents follow them but
don't gain new tools from granting them. Capability skills are a
different concept: granting them unlocks a bundle of TOOL_DEFINITIONS
entries. We use dedicated names to keep the two kinds separate.

Exception: `pptx-author` already exists AS a workflow skill AND would
be the natural capability-skill name for the two pptx tools. Reusing
is fine since the semantics line up (granting pptx-author = "this
agent should be able to create pptx"). We accept the overload.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tudou.capabilities")


# ── CORE tier ───────────────────────────────────────────────────────
# 2026-05-07 (user feedback "发送的工具只有这里已经绑定的"): the
# Tool Permissions UI is the single source of truth for what tools an
# agent gets. The ONLY exceptions are reflex primitives that aren't
# worth making the user click for every agent:
#
#   plan_update       — agent must be able to report its own progress.
#   get_skill_guide   — bootstrap; agent needs to discover what skills
#                       it was granted.
#   memory_recall     — read-only memory; doing your job needs context.
#   knowledge_lookup  — read-only KB; doing your job needs context.
#   wiki_ingest       — write-side counterpart of knowledge_lookup
#                       (added 2026-05-08, user feedback "wiki_ingest
#                       不是默认的么"). Without it, the ExecutionDiscipline
#                       rule "retros/playbooks must wiki_ingest" can't
#                       fire on agents that haven't been ticked. The
#                       wiki layer's built-in leak guardrail (defends
#                       against API-key / .env / IP leakage at ingest)
#                       provides a similar security floor to
#                       knowledge_lookup, so it's safe as default.
#
# Everything else — dispatch_task, send_message, sc_*, milestone /
# goal / issue updates, mcp_call, submit_skill, task_update, etc. —
# now requires the user to tick the box in Tool Permissions. Previously
# those were silently injected as "CORE", so a solo agent shipped ~33
# tool schemas (~9k tokens) the user never asked for. Token hit on a
# typical agent: 25K tools → ~10–13K tools depending on what's ticked.
CORE_UNIVERSAL_TOOLS: frozenset[str] = frozenset({
    "plan_update",
    "get_skill_guide",
    # 2026-05-15: knowledge_lookup REMOVED from CORE per user request.
    # Reason: even though it's read-only, weak planners (mimo / qwen)
    # reflexively call it on every multi-step task and hit ONE_SHOT
    # violations. With explicit-opt-in (commit 2579e41) the framework
    # now restores it to the tool set only when the user message
    # contains explicit retrieval phrasing (查/搜/find/lookup/...). For
    # agents that genuinely need always-on KB access, tick the box in
    # Tool Permissions UI for that specific agent.
    #
    # memory_recall + wiki_ingest left for now (less abused than
    # knowledge_lookup in observed traces). If those also start
    # showing reflexive over-call patterns, remove them the same way.
    "memory_recall",
    "wiki_ingest",
})

# Kept as an empty set for back-compat with any caller that still
# imports it; project-coordination tools now go through the normal
# allowed_tools gate. Will be removed once no caller references it.
CORE_PROJECT_TOOLS: frozenset[str] = frozenset()

# Back-compat alias — existing imports of CORE_TOOLS still resolve.
# Now equal to CORE_UNIVERSAL_TOOLS since CORE_PROJECT is empty.
CORE_TOOLS: frozenset[str] = CORE_UNIVERSAL_TOOLS


def core_tools_for_context(*, in_project: bool, in_meeting: bool) -> frozenset[str]:
    """Return the CORE bypass set — tools that ship to the LLM
    regardless of the agent's allowed_tools selection. Now context-
    invariant (always the 4 UI-declared 核心 tools): the in_project /
    in_meeting kwargs are kept for caller compatibility but ignored.
    """
    return CORE_UNIVERSAL_TOOLS


# ── SKILL-GATED tier ───────────────────────────────────────────────
# { capability_skill_name: [tool_name, ...] }
# An agent sees these tools only if the skill is in its granted_skills
# OR the skill is in the global capability defaults list (admin-wide).
# Dict ordering preserved for reviewer readability.
#
# Design principle: bundle by FUNCTIONAL DOMAIN (what the tool does),
# not by role (who uses it). "file-ops" is a bundle, "coder-tools" is
# not — a researcher also reads files, a pm also writes reports.
CAPABILITY_SKILLS: dict[str, list[str]] = {
    # ── Core functional bundles (most agents want most of these) ──
    "file-ops": [
        "read_file", "write_file", "edit_file",
        "search_files", "glob_files",
    ],
    "shell-ops": [
        "bash", "run_tests",
        # 2026-05-08: Background mode helpers — let agents kick off
        # long-running compiles/tests with run_in_background=true,
        # then poll bash_logs(pid) and finally bash_kill(pid).
        "bash_logs", "bash_kill",
    ],
    "web-ops": [
        "web_search", "web_fetch",
    ],
    "memory-ops": [
        # 2026-05-15: knowledge_lookup REMOVED from memory-ops
        # capability per user request. Was the second auto-grant
        # source after CORE_UNIVERSAL_TOOLS (commit d0b5bb4): any
        # agent with the memory-ops skill capability got the tool
        # automatically, bypassing the UI tick. With this removal,
        # knowledge_lookup is now a normal opt-in tool — admin must
        # explicitly tick it in Tool Permissions for each agent that
        # needs always-on KB search. Even then, the chat-time
        # explicit-opt-in filter (commit 2579e41) further gates it
        # on user message phrasing.
        "save_experience",
        "share_knowledge", "learn_from_peers",
        "memory_recall",
    ],
    "data-process": [
        "datetime_calc", "json_process", "text_process",
    ],
    "ui-visibility": [
        "emit_ui_block",
    ],
    "scheduling": [
        "task_update",
        # 2026-05-08: Per-agent scratch todo list — TodoWrite-style
        # in-memory checklist, max 1 in_progress at a time.
        "agent_todo",
    ],
    "messaging": [
        "send_message", "ack_message", "reply_message",
        "check_inbox",
    ],
    "handoff": [
        "emit_handoff", "handoff_request", "team_create",
    ],
    # ── Specialty bundles (opt-in per agent) ──
    #
    # 2026-05-08: project-management WAS just goal/milestone/
    # deliverable creation. Bug surfaced from pm-小明's complaint
    # ("wiki_ingest 仍不可用"): the strict capability filter was
    # silently dropping 28 unclassified tools (project_state, sc_*,
    # finalize_step, etc.) even when the admin had ticked them in
    # Tool Permissions. Fix: classify ALL of them so the filter
    # ships them whenever the relevant skill is granted. Two new
    # bundles below (task-coordination, shadow-control) cover the
    # rest.
    "project-management": [
        # Goals / milestones / deliverables (existing)
        "submit_deliverable",
        "create_goal",
        "update_goal_progress",
        "create_milestone",
        "update_milestone_status",
        # Project state / blueprint / issue tracking
        "project_state",
        "define_project_blueprint",
        "update_milestone_responsibility",
        "list_issues",
        "report_issue",
        "update_issue",
        # Composite project workflows (collapse N atomic calls → 1)
        "bootstrap_project",
        "submit_review",
        "finalize_step",
        "init_project_context",
    ],
    "task-coordination": [
        # Agent-to-agent task lifecycle: dispatch / accept / inbox /
        # report-back. PMs need dispatch; every worker needs accept
        # + inbox + report.
        "dispatch_task",
        "accept_task",
        "inbox_assignments",
        "report_back",
        "propose_decomposition",
        "query_agent_status",
        "query_team_status",
        # Read-only fork — explore-subagent for off-loading research
        "spawn_explore_subagent",
    ],
    "shadow-control": [
        # Story Control / Shadow Constraint — artifact ledger and
        # decision log. Used by PMs and any agent that produces
        # tracked artifacts.
        "sc_register_artifact",
        "sc_get_artifact",
        "sc_query",
        "sc_record_decision",
        "sc_handoff",
    ],
    "pptx-author": [
        "create_pptx",
        "create_pptx_advanced",
    ],
    "video-forge": [
        "create_video",
    ],
    "screenshot": [
        "web_screenshot",
        "desktop_screenshot",
    ],
    "http-client": [
        "http_request",
    ],
    "admin-ops": [
        "pip_install",
        "request_web_login",
        "propose_skill",
        # 2026-05-08: submit_skill / mcp_call moved here. Earlier
        # comment claimed submit_skill was "moved to CORE_TOOLS on
        # 2026-04-30" but it never actually landed in CORE — it was
        # silently unclassified and stripped. mcp_call is the same
        # story; bridging to external MCP servers is admin-level.
        "submit_skill",
        "mcp_call",
    ],
}


# ── Computed reverse lookups ────────────────────────────────────────
# tool_name → capability_skill_name  (or None if core / unregistered)
_TOOL_TO_CAPABILITY: dict[str, str] = {}
for _skill, _tool_list in CAPABILITY_SKILLS.items():
    for _tool_name in _tool_list:
        _TOOL_TO_CAPABILITY[_tool_name] = _skill


# All capability-skill identifiers (for UI / migration).
CAPABILITY_SKILL_IDS: frozenset[str] = frozenset(CAPABILITY_SKILLS.keys())


def get_tool_capability(tool_name: str) -> Optional[str]:
    """Return the capability skill that gates this tool, or None if
    the tool is in the CORE tier (or not registered at all)."""
    return _TOOL_TO_CAPABILITY.get(tool_name)


def is_core_tool(tool_name: str) -> bool:
    """True if the tool is in the always-on CORE tier."""
    return tool_name in CORE_TOOLS


# ── Global default capability layer ────────────────────────────────
# Admin-editable file mapping name → list of capability skill ids that
# apply to EVERY agent implicitly. Lives alongside tool_denylist.json
# so users who configured one already know where to look for the other.
_DEFAULTS_FILENAME = "capability_defaults.json"

# Factory default: minimum functional bundles every agent needs to do
# anything useful. CORE is only 3 tools (plan_update / get_skill_guide /
# mcp_call) which is not enough — without these defaults, a fresh agent
# can't even read a file. Admins override by writing capability_defaults.json.
#
# Rationale for each entry:
#   file-ops     — read/write/edit are table-stakes; removing them leaves
#                   the agent unable to inspect/modify anything.
#   shell-ops    — many tasks end with 'run this' / 'test it'.
#   web-ops      — search + fetch is basic research capability.
#   memory-ops   — knowledge_lookup + save_experience are L3 learning loop.
#   data-process — datetime_calc / json_process / text_process utilities.
#   ui-visibility — emit_ui_block lets agent render rich UI in chat.
#   scheduling   — task_update for reminders / deferred work.
#   messaging    — send_message for inter-agent communication.
#   handoff      — emit_handoff for workflow baton-pass.
#
# Total schema weight of these 9 bundles: ~16KB vs the old ~62KB full
# dump — ~75% reduction on an agent with no extra skills granted.
_FACTORY_DEFAULT_CAPABILITIES: list[str] = [
    "file-ops",
    "shell-ops",
    "web-ops",
    "memory-ops",
    "data-process",
    "ui-visibility",
    "scheduling",
    "messaging",
    "handoff",
    # 2026-05-08: task-coordination added to defaults — every worker
    # agent needs accept_task / inbox_assignments / report_back to
    # function in a multi-agent setting. PM-style dispatch_task is
    # also here; non-PM agents simply won't call it. Token cost is
    # ~2KB schema for the bundle, negligible compared to fixing the
    # silent-drop bug.
    "task-coordination",
]


def _default_home() -> Path:
    """Resolve the data directory (~/.tudou_claw or $TUDOU_CLAW_HOME)."""
    home = os.environ.get("TUDOU_CLAW_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    return Path.home() / ".tudou_claw"


def load_global_default_capabilities(path: Path | None = None) -> list[str]:
    """Load the admin-configured global default capability skill list.

    Never raises — missing file / malformed file both yield the
    factory default. Unknown capability names (not in CAPABILITY_SKILLS)
    are silently dropped with a warning, so a typo in the config file
    won't silently unlock the wrong thing.
    """
    target = path or (_default_home() / _DEFAULTS_FILENAME)
    if not target.is_file():
        return list(_FACTORY_DEFAULT_CAPABILITIES)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            "capability_defaults.json unreadable (%s); falling back to none.", e)
        return list(_FACTORY_DEFAULT_CAPABILITIES)

    raw = data.get("defaults") or []
    if not isinstance(raw, list):
        return list(_FACTORY_DEFAULT_CAPABILITIES)

    cleaned: list[str] = []
    unknown: list[str] = []
    for entry in raw:
        name = str(entry).strip()
        if not name:
            continue
        if name in CAPABILITY_SKILLS:
            cleaned.append(name)
        else:
            unknown.append(name)
    if unknown:
        logger.warning(
            "capability_defaults.json has unknown names (ignored): %s",
            unknown,
        )
    return cleaned


def save_global_default_capabilities(
    caps: list[str], path: Path | None = None,
) -> None:
    """Persist the admin-configured global default capability list.

    Writes atomically (tmp + rename) so a crash mid-write can't corrupt
    the file. Unknown names rejected at write time.
    """
    target = path or (_default_home() / _DEFAULTS_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)

    cleaned = []
    for entry in caps:
        name = str(entry).strip()
        if not name:
            continue
        if name not in CAPABILITY_SKILLS:
            raise ValueError(
                f"Unknown capability skill: {name!r}. "
                f"Valid: {sorted(CAPABILITY_SKILL_IDS)}"
            )
        cleaned.append(name)
    cleaned = sorted(set(cleaned))

    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"defaults": cleaned}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def filter_tools_by_capability(
    tools_list: list[dict],
    granted_skills: list[str] | None,
    global_defaults: list[str] | None = None,
    explicit_allow: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Apply the capability-tier filter.

    Keeps a tool iff ANY of:
      1. It is in CORE_TOOLS (tiny irreducible set: plan_update /
         get_skill_guide / memory_recall / knowledge_lookup /
         wiki_ingest).
      2. It is in ``explicit_allow`` — admin clicked the tick box for
         this tool in the per-agent Tool Permissions UI. Per the
         2026-05-07 design note ("the Tool Permissions UI is the
         single source of truth for what tools an agent gets"), an
         explicit tick wins over capability gating. Without this
         bypass, ticking a tool the agent doesn't have the right
         capability skill for silently drops it — the user complained
         "wiki_ingest 仍不可用" after binding because of exactly this.
      3. Its gating capability is in ``global_defaults`` (admin-wide).
      4. Its gating capability is in ``granted_skills`` (per-agent).

    For tools NOT explicitly ticked, the strict classification rule
    still applies: unclassified tools (not in CORE, not in any
    CAPABILITY_SKILLS bundle) don't ship. The sanity-check helper
    flags such drift so admins can classify at review time.

    If ``global_defaults`` is None it's loaded from disk via
    ``load_global_default_capabilities()``. Pass an explicit empty list
    to disable the global layer in tests.
    """
    if global_defaults is None:
        global_defaults = load_global_default_capabilities()
    explicit_set: set[str] = set(explicit_allow or ())
    # Normalize granted_skills — a registry-installed skill has id like
    # "file-ops@1.0.0" while CAPABILITY_SKILLS keys are "file-ops".
    # Accept either form by stripping @version.
    raw_caps = set(global_defaults) | set(granted_skills or ())
    effective_caps: set[str] = set()
    for cap in raw_caps:
        if not cap:
            continue
        # "name@1.0.0" → "name"; leave "name" alone
        bare = cap.split("@", 1)[0] if "@" in cap else cap
        effective_caps.add(bare)
        effective_caps.add(cap)  # also keep original in case id form is used
    kept: list[dict] = []
    for t in tools_list:
        name = t.get("function", {}).get("name", "")
        if name in CORE_TOOLS:
            kept.append(t)
            continue
        if name in explicit_set:
            # Admin's explicit tick in profile.allowed_tools — ship it
            # regardless of capability gating. The tick IS the grant.
            kept.append(t)
            continue
        cap = _TOOL_TO_CAPABILITY.get(name)
        if cap is None:
            # Strict: unclassified tool not explicitly ticked → do NOT
            # ship. Prevents silent payload growth from new tools that
            # haven't been classified into a bundle yet.
            continue
        if cap in effective_caps:
            kept.append(t)
    return kept


def sanity_check() -> tuple[set[str], set[str]]:
    """Dev helper: returns (tools_in_core_AND_capability, tools_missing_classification).

    Run from a script when adding new tools to catch drift between
    TOOL_DEFINITIONS and this module.
    """
    from . import tools as _tools_mod
    all_defined = {t["function"]["name"] for t in _tools_mod.TOOL_DEFINITIONS}
    both = CORE_TOOLS & set(_TOOL_TO_CAPABILITY)
    missing = all_defined - CORE_TOOLS - set(_TOOL_TO_CAPABILITY)
    return both, missing
