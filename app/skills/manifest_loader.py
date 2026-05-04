"""Routing-manifest loader for skill chain and disambiguation metadata.

MANIFEST.yaml (at app/skills/MANIFEST.yaml) is a thin single-source-of-truth
file layered on top of the per-skill manifest.yaml / SKILL.md frontmatter.
It carries only the routing-extras the engine cares about:
    not_for, next, sop_step, requires_mcp

Why two files instead of one: individual skill manifests ship with the skill
package (upstream source of truth for name / version / runtime / entry),
while routing extras are a HOST-LEVEL concern (what chains into what in
OUR workflow) and change independently.

This loader is intentionally small and pure — no I/O side effects beyond
the single file read. Consumers: SkillRegistry (merge into inst metadata
at scan time) and the system-prompt builder (render a compact bootstrap
table for LLM routing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - yaml is already a hard dep elsewhere
    _yaml = None


# Default location — override via env for tests.
_DEFAULT_MANIFEST_PATH = Path(__file__).parent / "MANIFEST.yaml"

# Current schema version we understand. Bump in lockstep with MANIFEST.yaml
# when the structure changes in a non-backward-compatible way.
_SUPPORTED_SCHEMA = 1


@dataclass(frozen=True)
class SkillRoutingExtras:
    """Routing-layer metadata for a single skill.

    Every field defaults to empty so a skill that isn't listed in the
    manifest still gets a valid (empty) extras record from lookups.
    """
    not_for: list[str] = field(default_factory=list)
    next: list[str] = field(default_factory=list)
    sop_step: int | None = None
    requires_mcp: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillGroup:
    """A named bundle of skills granted as a unit.

    ``catalog_only=True`` means: visible in the catalog UI, but NOT
    auto-included when a role's defaults reference this group name —
    advanced users opt in explicitly.
    """
    name: str
    label: str = ""
    description: str = ""
    members: list[str] = field(default_factory=list)
    catalog_only: bool = False


@dataclass
class SkillManifest:
    """Parsed MANIFEST.yaml — schema version + per-skill extras map +
    optional group definitions for bundle-style role binding."""
    schema_version: int
    skills: dict[str, SkillRoutingExtras]
    groups: dict[str, SkillGroup] = field(default_factory=dict)

    def extras_for(self, skill_name: str) -> SkillRoutingExtras:
        """Return extras for a skill, or an empty record if not listed."""
        return self.skills.get(skill_name, SkillRoutingExtras())

    def expand_groups(self, group_names: list[str],
                      include_catalog_only: bool = False) -> list[str]:
        """Resolve a list of group names to a flat list of member skill
        names, dedup'd and order-preserving.

        Unknown groups silently skipped. By default ``catalog_only``
        groups are excluded — pass ``include_catalog_only=True`` to
        force inclusion (admin override scenario).
        """
        seen: set[str] = set()
        out: list[str] = []
        for gn in group_names or []:
            g = self.groups.get(gn)
            if g is None:
                continue
            if g.catalog_only and not include_catalog_only:
                continue
            for m in g.members:
                if m and m not in seen:
                    seen.add(m)
                    out.append(m)
        return out


def _coerce_list(v: Any) -> list[str]:
    """Accept list-of-strings, strip empties; anything else → []."""
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _coerce_sop_step(v: Any) -> int | None:
    """Accept int, or None/missing. Reject strings to avoid silent typos."""
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return None


def load_manifest(path: Path | str | None = None) -> SkillManifest:
    """Load MANIFEST.yaml and return a validated SkillManifest.

    Never raises for missing/empty/malformed files — returns a manifest
    with schema_version=_SUPPORTED_SCHEMA and no skills. This keeps the
    system bootable even if the file has a typo; callers that need to
    detect that should check ``len(manifest.skills)``.
    """
    target = Path(path) if path else _DEFAULT_MANIFEST_PATH
    if not target.is_file() or _yaml is None:
        return SkillManifest(schema_version=_SUPPORTED_SCHEMA, skills={})

    try:
        raw = _yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:
        return SkillManifest(schema_version=_SUPPORTED_SCHEMA, skills={})

    if not isinstance(raw, dict):
        return SkillManifest(schema_version=_SUPPORTED_SCHEMA, skills={})

    schema_version = int(raw.get("schemaVersion", _SUPPORTED_SCHEMA))
    skills_raw = raw.get("skills") or {}
    if not isinstance(skills_raw, dict):
        skills_raw = {}

    skills: dict[str, SkillRoutingExtras] = {}
    for name, meta in skills_raw.items():
        if not isinstance(meta, dict):
            # Skip malformed entries but keep loading the rest.
            continue
        skills[str(name)] = SkillRoutingExtras(
            not_for=_coerce_list(meta.get("not_for")),
            next=_coerce_list(meta.get("next")),
            sop_step=_coerce_sop_step(meta.get("sop_step")),
            requires_mcp=_coerce_list(meta.get("requires_mcp")),
        )

    # ── Groups (optional) ──
    # Bundle definitions for role-default binding. Missing or malformed
    # `groups:` block falls back to empty dict — existing manifests stay
    # forward-compatible.
    groups_raw = raw.get("groups") or {}
    groups: dict[str, SkillGroup] = {}
    if isinstance(groups_raw, dict):
        for gname, gmeta in groups_raw.items():
            if not isinstance(gmeta, dict):
                continue
            groups[str(gname)] = SkillGroup(
                name=str(gname),
                label=str(gmeta.get("label") or ""),
                description=str(gmeta.get("description") or ""),
                members=_coerce_list(gmeta.get("members")),
                catalog_only=bool(gmeta.get("catalog_only")),
            )

    return SkillManifest(schema_version=schema_version, skills=skills, groups=groups)


def render_bootstrap_table(
    manifest: SkillManifest,
    skill_names: list[str],
    descriptions: dict[str, str] | None = None,
    max_desc_chars: int = 120,
) -> str:
    """Render a compact markdown bootstrap table for system-prompt injection.

    Columns: skill name + first line of description + "not for" + "next".
    LLMs scan this to decide which skill to load WITHOUT having to pay
    the full SKILL.md token cost upfront.

    Args:
        manifest: parsed routing manifest
        skill_names: names of granted skills to include (preserves order)
        descriptions: optional map name→short description; truncated to
                      ``max_desc_chars``. Skills without a description
                      show "(see SKILL.md)" as a nudge.
    """
    descriptions = descriptions or {}
    if not skill_names:
        return ""

    lines = [
        "| Skill | Use when | Not for | Next |",
        "|-------|----------|---------|------|",
    ]
    for name in skill_names:
        extras = manifest.extras_for(name)
        desc_raw = descriptions.get(name, "").strip() or "(see SKILL.md)"
        # First non-empty line of desc, truncated.
        first_line = next(
            (ln.strip() for ln in desc_raw.splitlines() if ln.strip()),
            desc_raw,
        )
        if len(first_line) > max_desc_chars:
            first_line = first_line[: max_desc_chars - 1] + "…"
        not_for_cell = "; ".join(extras.not_for) if extras.not_for else "—"
        next_cell = ", ".join(extras.next) if extras.next else "—"
        # Markdown-escape pipes in cell content.
        first_line = first_line.replace("|", "\\|")
        not_for_cell = not_for_cell.replace("|", "\\|")
        lines.append(f"| `{name}` | {first_line} | {not_for_cell} | {next_cell} |")
    return "\n".join(lines)
