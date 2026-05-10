"""Bundle apply engine — idempotent template-to-agent binding.

When a user picks a SpecialtyTemplate (e.g. legal-expert) for an agent,
this module:

    1. Stamps the agent's expert_* fields (specialty, template_version,
       initialized_at) so the reply pipeline routes through the expert.
    2. For each required prompt pack, calls a save_callback (the host
       app's persistence — typically ``hub._save_agents``) with a binding
       record. We deliberately do NOT touch the prompt-pack registry here;
       V2 vertical handles "pack not in registry" gracefully.
    3. For each required skill, calls a skill_grant_callback (the
       host app's skill registry, typically ``SkillRegistry.grant``).
    4. Tracks what was bound vs what was missing in the registry, returns
       a BundleApplyResult.

Idempotency: applying the same bundle twice yields no double-binds. The
callbacks are expected to be idempotent themselves; this module also
de-duplicates within a single call.

Mock-friendly: both callbacks are pure-Python ``Callable``s. Tests pass
in lambdas/MagicMocks; real V2 vertical wires in the production
implementations.

Public API::

    from app.domain_expert.bundle_apply import apply_bundle, BundleApplyResult

    result = apply_bundle(
        template,
        agent,
        save_callback=lambda: hub._save_agents(),
        skill_grant_callback=lambda agent_id, skill_id: registry.grant(agent_id, skill_id),
        pack_exists_callback=lambda pack_id: registry.has(pack_id),  # optional
        skill_exists_callback=lambda skill_id: registry.has(skill_id),  # optional
        now=time.time,
    )

    result.is_complete()
    result.packs_bound
    result.skills_granted
    result.missing_packs
    result.missing_skills
    result.summary()

Track D scope: bundle_apply does NOT raise when packs/skills are missing
from the host registry — V2 vertical handles the missing-resources flow
(prompts user to install). It just reports them in the result.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .template import SpecialtyTemplate


# ── Result ──

@dataclass
class BundleApplyResult:
    """Outcome of applying a SpecialtyTemplate to an Agent.

    is_complete() returns True when every required pack/skill/mcp from
    the template was confirmed present in the host registry (or no
    existence check was wired). Anything in the missing_* lists indicates
    a resource the V2 flow needs to install before the agent is usable.
    """
    template_id: str
    template_version: str
    specialty: str
    agent_id: str

    packs_bound: list[str] = field(default_factory=list)
    anthropic_packs_bound: list[str] = field(default_factory=list)
    skills_granted: list[str] = field(default_factory=list)
    mcps_required: list[str] = field(default_factory=list)

    missing_packs: list[str] = field(default_factory=list)
    missing_anthropic_packs: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    missing_mcps: list[str] = field(default_factory=list)

    saved: bool = False
    initialized_at: float = 0.0

    def is_complete(self) -> bool:
        """True if no required item was reported missing by the existence
        callbacks. When no existence callbacks are passed, everything is
        considered "present" by definition."""
        return not (self.missing_packs
                    or self.missing_anthropic_packs
                    or self.missing_skills
                    or self.missing_mcps)

    def summary(self) -> str:
        lines = [
            f"Applied {self.template_id}@{self.template_version} "
            f"to agent {self.agent_id}:",
            f"  packs bound: {len(self.packs_bound)} community + "
            f"{len(self.anthropic_packs_bound)} anthropic",
            f"  skills granted: {len(self.skills_granted)}",
            f"  mcps required: {len(self.mcps_required)}",
        ]
        if not self.is_complete():
            lines.append("  MISSING:")
            if self.missing_packs:
                lines.append(f"    packs: {self.missing_packs}")
            if self.missing_anthropic_packs:
                lines.append(f"    anthropic_packs: {self.missing_anthropic_packs}")
            if self.missing_skills:
                lines.append(f"    skills: {self.missing_skills}")
            if self.missing_mcps:
                lines.append(f"    mcps: {self.missing_mcps}")
        return "\n".join(lines)


# ── Public API ──

def apply_bundle(
    template: SpecialtyTemplate,
    agent: Any,
    *,
    save_callback: Optional[Callable[[], None]] = None,
    skill_grant_callback: Optional[Callable[[str, str], None]] = None,
    pack_exists_callback: Optional[Callable[[str], bool]] = None,
    anthropic_pack_exists_callback: Optional[Callable[[str], bool]] = None,
    skill_exists_callback: Optional[Callable[[str], bool]] = None,
    mcp_exists_callback: Optional[Callable[[str], bool]] = None,
    now: Callable[[], float] = _time.time,
) -> BundleApplyResult:
    """Apply ``template`` to ``agent``. Returns a BundleApplyResult.

    Required parameters:
        template: a fully-loaded SpecialtyTemplate.
        agent:    the Agent dataclass instance. Must have an ``id``
                  attribute and the 5 ``expert_*`` fields. The function
                  mutates ``agent.expert_specialty``,
                  ``agent.expert_template_version``,
                  ``agent.expert_initialized_at`` (only when first time)
                  and ``agent.expert_level`` (defaults to "novice" when
                  the agent has no prior expert state).

    Optional callbacks:
        save_callback: called once at the end iff something changed,
            so the host can persist agents.json (or equivalent).
        skill_grant_callback(agent_id, skill_id): called once per
            required skill. Should be idempotent.
        pack_exists_callback / anthropic_pack_exists_callback /
        skill_exists_callback / mcp_exists_callback:
            return True/False for "is this id installed in the host
            registry?" — used to populate the missing_* fields. When
            absent, no existence check happens (everything is treated
            as present by default — V2 vertical wires the real check).

    Idempotency: calling apply_bundle twice with the same template
    produces the same result. The agent's expert_initialized_at is
    only set the first time (or when a different template_version is
    being applied — i.e. re-cultivation).
    """
    if not isinstance(template, SpecialtyTemplate):
        raise TypeError("apply_bundle: template must be SpecialtyTemplate")
    if not hasattr(agent, "id"):
        raise TypeError("apply_bundle: agent must expose .id")

    agent_id = getattr(agent, "id", "") or ""
    if not agent_id:
        raise ValueError("apply_bundle: agent.id is required")

    result = BundleApplyResult(
        template_id=template.id,
        template_version=template.version,
        specialty=template.specialty,
        agent_id=agent_id,
    )

    something_changed = False

    # ── 1. Stamp expert_* fields on the agent ──
    prev_specialty = _safe_get(agent, "expert_specialty", "")
    prev_version = _safe_get(agent, "expert_template_version", "")
    prev_init = _safe_get(agent, "expert_initialized_at", 0.0)

    if prev_specialty != template.specialty:
        # New specialty (or first cultivation) — reset level
        if hasattr(agent, "expert_level"):
            agent.expert_level = "novice"
        if hasattr(agent, "expert_lora_version"):
            agent.expert_lora_version = ""
        something_changed = True

    if hasattr(agent, "expert_specialty"):
        if agent.expert_specialty != template.specialty:
            agent.expert_specialty = template.specialty
            something_changed = True
    if hasattr(agent, "expert_template_version"):
        if agent.expert_template_version != template.version:
            agent.expert_template_version = template.version
            something_changed = True

    # initialized_at: set on first apply OR when (re-)applying after
    # specialty/template_version change. Otherwise preserve.
    if (prev_specialty != template.specialty
            or prev_version != template.version
            or not prev_init):
        if hasattr(agent, "expert_initialized_at"):
            agent.expert_initialized_at = float(now())
            something_changed = True

    result.initialized_at = _safe_get(agent, "expert_initialized_at", 0.0)

    # ── 2. Bind packs (de-dup, optionally check existence) ──
    seen_packs: set[str] = set()
    for pack_id in template.required_packs:
        if pack_id in seen_packs:
            continue
        seen_packs.add(pack_id)
        if pack_exists_callback is not None and not pack_exists_callback(pack_id):
            result.missing_packs.append(pack_id)
            continue
        result.packs_bound.append(pack_id)

    seen_anthropic: set[str] = set()
    for ap in template.required_anthropic_packs:
        if ap in seen_anthropic:
            continue
        seen_anthropic.add(ap)
        if (anthropic_pack_exists_callback is not None
                and not anthropic_pack_exists_callback(ap)):
            result.missing_anthropic_packs.append(ap)
            continue
        result.anthropic_packs_bound.append(ap)

    # ── 3. Grant skills (idempotent via callback + de-dup here) ──
    seen_skills: set[str] = set()
    for skill_id in template.required_skills:
        if skill_id in seen_skills:
            continue
        seen_skills.add(skill_id)
        if (skill_exists_callback is not None
                and not skill_exists_callback(skill_id)):
            result.missing_skills.append(skill_id)
            continue
        if skill_grant_callback is not None:
            try:
                skill_grant_callback(agent_id, skill_id)
            except Exception:
                # Track D contract: do not raise out of bundle_apply on
                # callback errors — record as "missing" so V2 vertical
                # handles the install flow.
                result.missing_skills.append(skill_id)
                continue
        result.skills_granted.append(skill_id)
        # Skill grants don't mutate agent state directly — they're
        # tracked by the host's skill registry — so they don't trigger
        # save_callback. Idempotent re-applies of an already-cultivated
        # template are a no-op for save.

    # ── 4. MCPs are require-only (no granting in Track D) ──
    seen_mcps: set[str] = set()
    for mcp_id in template.required_mcps:
        if mcp_id in seen_mcps:
            continue
        seen_mcps.add(mcp_id)
        if (mcp_exists_callback is not None
                and not mcp_exists_callback(mcp_id)):
            result.missing_mcps.append(mcp_id)
            continue
        result.mcps_required.append(mcp_id)

    # ── 5. Save once at the end iff anything changed ──
    if something_changed and save_callback is not None:
        save_callback()
        result.saved = True
    elif not something_changed:
        # Idempotent re-apply of a template already on the agent — still
        # populate packs_bound (so callers can confirm what's bound)
        # but don't trigger save.
        result.saved = False

    return result


# ── Helpers ──

def _safe_get(obj: Any, attr: str, default: Any) -> Any:
    return getattr(obj, attr, default) if hasattr(obj, attr) else default
