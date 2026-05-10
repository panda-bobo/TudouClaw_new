"""Template version diff — classify changes as breaking or non-breaking.

When a SpecialtyTemplate is updated (new YAML version shipped or hot-edited),
existing experts may need to be re-cultivated. This module compares two
templates and tells callers:

    - what changed (added/removed/altered fields)
    - whether any change is "breaking" (forces level reset / re-eval)

Non-breaking changes (cosmetic, additive, knob-tweaking):
    - name / icon / description
    - safety.disclaimer text
    - eval threshold / weight tweaks (still same runner_ids)
    - chunker.* tweaks (re-index possibly needed but not a level reset)
    - training.* tweaks
    - level_rules thresholds *loosened* — bumping someone up never breaks
    - additive: new optional packs/skills/mcps/eval-runners

Breaking changes (force level reset to "novice" + re-init flow):
    - id changed (effectively a different template)
    - specialty changed
    - MAJOR version bumped
    - any required_pack/required_skill/required_mcp REMOVED
    - any required_anthropic_pack REMOVED
    - any eval runner REMOVED
    - level_rules thresholds *tightened* (current expert no longer qualifies)
    - safety.cite_required toggled on (existing answers don't comply)

Usage::

    from app.domain_expert.template_diff import diff
    d = diff(old_tpl, new_tpl)
    if d.is_breaking():
        # show user "this is a breaking change, your expert level will reset"
        print(d.summary())
    for change in d.changes:
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .template import SpecialtyTemplate


# ── Change record ──

@dataclass
class Change:
    """One field-level difference between two templates."""
    path: str          # dotted path, e.g. "safety.cite_required"
    kind: str          # "added" | "removed" | "modified"
    breaking: bool     # whether this single change is breaking
    old: Any = None
    new: Any = None
    note: str = ""     # human reason ("required pack removed", etc.)

    def summary(self) -> str:
        marker = "BREAKING" if self.breaking else "non-breaking"
        if self.kind == "added":
            return f"[{marker}] {self.path}: added {self.new!r}"
        if self.kind == "removed":
            return f"[{marker}] {self.path}: removed {self.old!r}"
        return f"[{marker}] {self.path}: {self.old!r} → {self.new!r}"


# ── Diff result ──

@dataclass
class TemplateDiff:
    """Result of comparing two templates."""
    old_version: str
    new_version: str
    changes: list[Change] = field(default_factory=list)

    def is_breaking(self) -> bool:
        return any(c.breaking for c in self.changes)

    def is_empty(self) -> bool:
        return not self.changes

    def breaking_changes(self) -> list[Change]:
        return [c for c in self.changes if c.breaking]

    def non_breaking_changes(self) -> list[Change]:
        return [c for c in self.changes if not c.breaking]

    def summary(self) -> str:
        if self.is_empty():
            return f"No changes between {self.old_version} and {self.new_version}."
        head = (f"Diff {self.old_version} → {self.new_version}: "
                f"{len(self.breaking_changes())} breaking, "
                f"{len(self.non_breaking_changes())} non-breaking.")
        lines = [head] + [f"  - {c.summary()}" for c in self.changes]
        return "\n".join(lines)


# ── Public entry point ──

def diff(old: SpecialtyTemplate, new: SpecialtyTemplate) -> TemplateDiff:
    """Compute the diff between two templates."""
    if not isinstance(old, SpecialtyTemplate) or not isinstance(new, SpecialtyTemplate):
        raise TypeError("diff() requires two SpecialtyTemplate instances")

    out: list[Change] = []

    # ── Identity (id, specialty, version) ──
    if old.id != new.id:
        out.append(Change("id", "modified", True, old.id, new.id,
                          "id changed (different template)"))
    if old.specialty != new.specialty:
        out.append(Change("specialty", "modified", True,
                          old.specialty, new.specialty,
                          "specialty changed (different family)"))
    if _major(old.version) != _major(new.version):
        out.append(Change("version", "modified", True,
                          old.version, new.version,
                          "major version bumped"))
    elif old.version != new.version:
        out.append(Change("version", "modified", False,
                          old.version, new.version,
                          "minor/patch version bumped"))

    # ── Cosmetic (always non-breaking) ──
    for fname in ("name", "icon", "description"):
        ov = getattr(old, fname)
        nv = getattr(new, fname)
        if ov != nv:
            out.append(Change(fname, "modified", False, ov, nv))

    # ── Required lists: removals are breaking, additions are not ──
    out.extend(_diff_required_list(
        "required_packs", old.required_packs, new.required_packs))
    out.extend(_diff_required_list(
        "required_anthropic_packs",
        old.required_anthropic_packs, new.required_anthropic_packs))
    out.extend(_diff_required_list(
        "required_skills", old.required_skills, new.required_skills))
    out.extend(_diff_required_list(
        "required_mcps", old.required_mcps, new.required_mcps))

    # ── Eval suite: removed runner = breaking, added = non-breaking,
    #     threshold/weight tweaks = non-breaking ──
    out.extend(_diff_eval_suite(old.eval_suite, new.eval_suite))

    # ── Level rules: tightened thresholds = breaking, loosened = non ──
    out.extend(_diff_level_rules(old.level_rules, new.level_rules))

    # ── Safety ──
    out.extend(_diff_safety(old.safety, new.safety))

    # ── Chunker / training: always non-breaking knobs ──
    out.extend(_diff_simple_struct(
        "chunker", old.chunker, new.chunker, breaking=False))
    out.extend(_diff_simple_struct(
        "training", old.training, new.training, breaking=False))

    # ── Corpus sources: additive — non-breaking (re-ingest may run) ──
    out.extend(_diff_corpus_sources(old.corpus_sources, new.corpus_sources))

    return TemplateDiff(old_version=old.version, new_version=new.version,
                        changes=out)


# ── Helpers ──

def _major(v: str) -> str:
    return v.split(".", 1)[0] if "." in v else v


def _diff_required_list(path: str, old: list[str],
                        new: list[str]) -> list[Change]:
    olds = set(old)
    news = set(new)
    out: list[Change] = []
    for removed in sorted(olds - news):
        out.append(Change(f"{path}[{removed}]", "removed", True,
                          old=removed, note="required item removed"))
    for added in sorted(news - olds):
        out.append(Change(f"{path}[{added}]", "added", False, new=added))
    return out


def _diff_eval_suite(old: list, new: list) -> list[Change]:
    om = {e.runner_id: e for e in old}
    nm = {e.runner_id: e for e in new}
    out: list[Change] = []
    for rid in sorted(set(om) - set(nm)):
        out.append(Change(f"eval_suite[{rid}]", "removed", True,
                          old=om[rid].runner_id,
                          note="eval runner removed"))
    for rid in sorted(set(nm) - set(om)):
        out.append(Change(f"eval_suite[{rid}]", "added", False,
                          new=nm[rid].runner_id))
    for rid in sorted(set(om) & set(nm)):
        oe = om[rid]
        ne = nm[rid]
        for fname in ("weight", "threshold", "description"):
            if getattr(oe, fname) != getattr(ne, fname):
                out.append(Change(
                    f"eval_suite[{rid}].{fname}", "modified", False,
                    getattr(oe, fname), getattr(ne, fname)))
    return out


def _diff_level_rules(old: list, new: list) -> list[Change]:
    """Compare level rules, treating tightened thresholds as breaking."""
    om = {(r.from_level, r.to_level): r for r in old}
    nm = {(r.from_level, r.to_level): r for r in new}
    out: list[Change] = []
    for k in sorted(set(om) - set(nm)):
        out.append(Change(f"level_rules[{k[0]}->{k[1]}]", "removed", True,
                          old=str(k), note="level rule removed"))
    for k in sorted(set(nm) - set(om)):
        out.append(Change(f"level_rules[{k[0]}->{k[1]}]", "added", False,
                          new=str(k)))
    for k in sorted(set(om) & set(nm)):
        old_r = om[k]
        new_r = nm[k]
        for fname in ("min_eval_score", "min_corpus_chunks", "min_traces"):
            ov = getattr(old_r, fname)
            nv = getattr(new_r, fname)
            if ov == nv:
                continue
            tightened = nv > ov
            out.append(Change(
                f"level_rules[{k[0]}->{k[1]}].{fname}", "modified",
                breaking=tightened,
                old=ov, new=nv,
                note="threshold tightened" if tightened else
                     "threshold loosened",
            ))
    return out


def _diff_safety(old, new) -> list[Change]:
    out: list[Change] = []
    if old.cite_required != new.cite_required:
        # Breaking only when toggled ON (existing answers no longer comply)
        breaking = bool(new.cite_required) and not bool(old.cite_required)
        out.append(Change("safety.cite_required", "modified", breaking,
                          old.cite_required, new.cite_required,
                          note="cite_required toggled on" if breaking
                               else "cite_required toggled off"))
    if old.confidence_threshold != new.confidence_threshold:
        # Tighter threshold = breaking, lower = non
        breaking = new.confidence_threshold > old.confidence_threshold
        out.append(Change("safety.confidence_threshold", "modified", breaking,
                          old.confidence_threshold, new.confidence_threshold,
                          note="threshold tightened" if breaking
                               else "threshold loosened"))
    # Refuse topics: additions = breaking (now refuses things it didn't),
    # removals = non
    olds = set(old.refuse_topics or [])
    news = set(new.refuse_topics or [])
    for added in sorted(news - olds):
        out.append(Change(f"safety.refuse_topics[{added}]", "added", True,
                          new=added, note="now refuses topic"))
    for removed in sorted(olds - news):
        out.append(Change(f"safety.refuse_topics[{removed}]", "removed",
                          False, old=removed))
    if old.disclaimer != new.disclaimer:
        out.append(Change("safety.disclaimer", "modified", False,
                          old.disclaimer, new.disclaimer))
    return out


def _diff_simple_struct(path: str, old, new,
                        breaking: bool) -> list[Change]:
    out: list[Change] = []
    fields = old.__dataclass_fields__
    for fname in fields:
        ov = getattr(old, fname)
        nv = getattr(new, fname)
        if ov != nv:
            out.append(Change(f"{path}.{fname}", "modified", breaking,
                              ov, nv))
    return out


def _diff_corpus_sources(old: list, new: list) -> list[Change]:
    """Corpus changes are all non-breaking — re-ingest fixes them."""
    om = {(c.type, c.location): c for c in old}
    nm = {(c.type, c.location): c for c in new}
    out: list[Change] = []
    for k in sorted(set(om) - set(nm)):
        out.append(Change(f"corpus_sources[{k[0]}:{k[1]}]", "removed",
                          False, old=str(k)))
    for k in sorted(set(nm) - set(om)):
        out.append(Change(f"corpus_sources[{k[0]}:{k[1]}]", "added",
                          False, new=str(k)))
    return out
