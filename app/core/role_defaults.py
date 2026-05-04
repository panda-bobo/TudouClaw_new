"""
Role Defaults — role → default skill packages + prompt packs mapping.

When a new agent is created with a given `role`, `hub.create_agent()` consults
this mapping to auto-populate:
    - agent.granted_skills       (executable skill package IDs, from SkillRegistry)
    - agent.bound_prompt_packs    (prompt pack IDs, from PromptPackRegistry)

The mapping is intentionally name-based (not ID-based). At bind time we look
up the actual installed skill / prompt pack by name and resolve to the real ID.
This lets the mapping survive re-installs and stay portable across machines.

Users can override the defaults in the Create Agent modal (the form already
supports per-agent granted_skills and bound_prompt_packs fields).

To extend: add a new entry to ROLE_DEFAULTS and describe which skill / pack
names that role should start with. Missing items are silently skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleDefaults:
    """Default capabilities bundled with a role at agent-creation time."""

    # Executable skill package names (match SkillManifest.name)
    skill_names: list[str] = field(default_factory=list)
    # PromptPack names (match PromptPack.name from SKILL.md frontmatter)
    prompt_pack_names: list[str] = field(default_factory=list)
    # PromptPack GROUP names — expanded at bind time via the routing
    # MANIFEST.yaml `groups:` section. Lets a role say "I want the whole
    # planning pack" instead of listing 3-4 individual skills.
    # Group expansion is unioned with `prompt_pack_names`; duplicates
    # are dropped (order-preserving).
    prompt_pack_groups: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Role → defaults map
# ---------------------------------------------------------------------------
#
# Keep this list short and high-signal. "general" covers everyone; role-specific
# entries only add role-relevant extras on top. Resolution at bind time is a
# union (general ∪ role_specific).
#
# Skill names listed here must match the `name` field in an installed
# manifest.yaml. Prompt pack names must match the `name` field in a SKILL.md
# YAML frontmatter.
# ---------------------------------------------------------------------------

ROLE_DEFAULTS: dict[str, RoleDefaults] = {
    # Baseline: every agent gets these
    "general": RoleDefaults(
        skill_names=[
            "take_screenshot",
            "send_email",
        ],
        # safe-artifact-paths: enforces that every file reported as a
        # deliverable lives under $AGENT_WORKSPACE (avoids the
        # 403 "path outside deliverable_dir" sandbox rejection).
        # action-first: enforces "act, don't announce" — the single biggest
        # source of wasted turns is the agent saying "Let me fix it:" and
        # then stopping without calling a tool. Hard rules + decision ladder.
        # Both bound at the 'general' baseline so ALL roles inherit.
        prompt_pack_names=["safe-artifact-paths", "action-first"],
    ),

    # Product / leadership
    "ceo": RoleDefaults(
        skill_names=["send_email"],
        prompt_pack_names=["code-review-guide"],
    ),
    "cto": RoleDefaults(
        skill_names=["send_email", "take_screenshot"],
        prompt_pack_names=["code-review-guide"],
    ),
    "pm": RoleDefaults(
        skill_names=["send_email", "take_screenshot"],
        # Single merged pack covers brainstorming + writing-plans +
        # dispatching-parallel-agents (the planning subset of superpowers).
        prompt_pack_names=["superpowers-engineering"],
    ),

    # Engineering
    # ── Superpowers binding (vendored from obra/superpowers) ──
    # 2026-05-04: 12 sub-skills physically merged into a single
    # `superpowers-engineering` pack — see app/skills/builtin/
    # superpowers-engineering/SKILL.md. Originals archived under
    # builtin/superpowers/.legacy_split/. All engineering roles now
    # bind the same one merged pack.
    # Catalog-only meta skills (using-superpowers / writing-skills) are
    # NOT auto-bound — advanced users can grant them manually.
    "coder": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=["code-review-guide", "superpowers-engineering"],
    ),
    "reviewer": RoleDefaults(
        skill_names=[],
        prompt_pack_names=["code-review-guide", "superpowers-engineering"],
    ),
    "architect": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=["code-review-guide", "superpowers-engineering"],
    ),
    "tester": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=[
            "superpowers-engineering",
            # web-automator: end-to-end browser testing via `npx agent-browser`
            "web-automator",
        ],
    ),
    "devops": RoleDefaults(
        skill_names=["take_screenshot", "send_email"],
        prompt_pack_names=["superpowers-engineering"],
    ),

    # Creative / research
    "designer": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=[],
    ),
    "researcher": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=[
            # web-automator: scrape / snapshot / PDF-capture pages during research
            "web-automator",
        ],
    ),
    "data": RoleDefaults(
        skill_names=["take_screenshot"],
        prompt_pack_names=[],
    ),
}


def _expand_groups_to_packs(group_names: list[str]) -> list[str]:
    """Expand `prompt_pack_groups` into concrete pack names via
    MANIFEST.yaml. Returns an empty list if the manifest is missing /
    malformed / lists no groups — that's a soft failure (the role just
    doesn't get the group's packs, rather than crashing agent creation).
    """
    if not group_names:
        return []
    try:
        from ..skills.manifest_loader import load_manifest
        m = load_manifest()
        return m.expand_groups(group_names, include_catalog_only=False)
    except Exception:
        return []


def get_role_defaults(role: str) -> RoleDefaults:
    """Return defaults for a role (merged with 'general' baseline).

    Unknown roles fall back to the 'general' baseline.
    Groups (``prompt_pack_groups``) get expanded into pack names via the
    MANIFEST.yaml `groups:` section, then unioned with any explicitly
    listed ``prompt_pack_names``.
    """
    base = ROLE_DEFAULTS.get("general", RoleDefaults())
    role_spec = ROLE_DEFAULTS.get(role) if role and role != "general" else None

    # Skills (still by name, no group concept here)
    skills = list(base.skill_names)
    if role_spec is not None:
        for s in role_spec.skill_names:
            if s not in skills:
                skills.append(s)

    # Prompt packs: explicit names + group expansions, unioned, base first
    packs: list[str] = []
    seen: set[str] = set()
    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            packs.append(name)
    for n in base.prompt_pack_names:
        _add(n)
    for n in _expand_groups_to_packs(list(base.prompt_pack_groups)):
        _add(n)
    if role_spec is not None:
        for n in role_spec.prompt_pack_names:
            _add(n)
        for n in _expand_groups_to_packs(list(role_spec.prompt_pack_groups)):
            _add(n)

    # Carry the (already-merged) group names back so callers that want
    # to inspect "what groups does this role have" can see them without
    # re-deriving from the pack list.
    groups = list(base.prompt_pack_groups)
    if role_spec is not None:
        for g in role_spec.prompt_pack_groups:
            if g not in groups:
                groups.append(g)

    return RoleDefaults(
        skill_names=skills,
        prompt_pack_names=packs,
        prompt_pack_groups=groups,
    )


def resolve_role_default_ids(
    role: str,
    skill_registry,
    prompt_pack_registry,
) -> tuple[list[str], list[str]]:
    """Translate role defaults (by name) into concrete IDs.

    Args:
        role: agent role string
        skill_registry: app.skills.SkillRegistry instance (or None)
        prompt_pack_registry: app.core.prompt_enhancer.PromptPackRegistry (or None)

    Returns:
        (granted_skill_ids, bound_prompt_pack_ids)
    """
    defaults = get_role_defaults(role)
    skill_ids: list[str] = []
    pack_ids: list[str] = []

    # Resolve skill package names → installed IDs
    if skill_registry is not None and defaults.skill_names:
        try:
            installs = skill_registry.list_all()
            name_to_id = {}
            for inst in installs:
                try:
                    name_to_id[inst.manifest.name] = inst.id
                except Exception:
                    continue
            for name in defaults.skill_names:
                sid = name_to_id.get(name)
                if sid:
                    skill_ids.append(sid)
        except Exception:
            pass

    # Resolve prompt pack names → pack IDs
    if prompt_pack_registry is not None and defaults.prompt_pack_names:
        try:
            store = getattr(prompt_pack_registry, "store", None)
            if store is not None:
                packs = store.get_active() if hasattr(store, "get_active") else []
                name_to_id = {p.name: p.skill_id for p in packs if getattr(p, "name", "")}
                for name in defaults.prompt_pack_names:
                    pid = name_to_id.get(name)
                    if pid:
                        pack_ids.append(pid)
        except Exception:
            pass

    return skill_ids, pack_ids
