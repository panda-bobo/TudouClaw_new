"""Track D Task D3 — template version diff (breaking vs non-breaking)."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain_expert.template import (
    EvalRunner,
    LevelRule,
    SafetyRails,
    SpecialtyTemplate,
)
from app.domain_expert.template_diff import Change, TemplateDiff, diff


def _base() -> SpecialtyTemplate:
    return SpecialtyTemplate.from_dict({
        "id": "legal-expert", "version": "1.0",
        "name": "Legal Expert", "specialty": "legal",
        "icon": "balance",
        "required_packs": ["agency_legal_lawyer"],
        "required_anthropic_packs": ["akwp_legal_brief"],
        "required_skills": ["cite_check"],
        "required_mcps": ["mcp_lawdb"],
        "eval_suite": [
            {"runner_id": "legalbench_zh", "weight": 0.7, "threshold": 0.6},
            {"runner_id": "citation_accuracy", "weight": 0.3, "threshold": 0.8},
        ],
        "level_rules": [
            {"from_level": "novice", "to_level": "journeyman",
             "min_eval_score": 0.5, "min_corpus_chunks": 100,
             "min_traces": 0},
        ],
        "safety": {"cite_required": False,
                   "confidence_threshold": 0.5,
                   "refuse_topics": [],
                   "disclaimer": "v1 disclaimer"},
        "corpus_sources": [
            {"type": "url", "location": "https://example.com/laws"},
        ],
        "training": {"base_model": "Qwen/Qwen2.5-7B-Instruct",
                     "lora_rank": 16, "lora_alpha": 32,
                     "learning_rate": 2e-4, "raft_recipe": "default",
                     "max_steps": 0, "distractor_count": 4,
                     "eval_split": 0.1},
        "chunker": {"strategy": "semantic", "max_tokens": 512,
                    "overlap_tokens": 64, "respect_boundaries": True},
    })


# ── No-op ──

def test_diff_identical_returns_empty():
    a = _base()
    b = _base()
    d = diff(a, b)
    assert d.is_empty()
    assert not d.is_breaking()
    assert "No changes" in d.summary()


def test_diff_type_check():
    with pytest.raises(TypeError):
        diff("not a template", _base())


# ── Identity changes ──

def test_id_change_is_breaking():
    a = _base()
    b = _base()
    b.id = "different-template"
    d = diff(a, b)
    assert d.is_breaking()
    assert any(c.path == "id" and c.breaking for c in d.changes)


def test_specialty_change_is_breaking():
    a = _base()
    b = _base()
    b.specialty = "medical"
    d = diff(a, b)
    assert d.is_breaking()
    assert any(c.path == "specialty" and c.breaking for c in d.changes)


def test_major_version_bump_is_breaking():
    a = _base()
    b = _base()
    b.version = "2.0"
    d = diff(a, b)
    assert d.is_breaking()
    bc = [c for c in d.changes if c.path == "version"][0]
    assert bc.breaking
    assert "major" in bc.note


def test_minor_version_bump_is_non_breaking():
    a = _base()
    b = _base()
    b.version = "1.1"
    d = diff(a, b)
    # Only a minor version diff → no breaking changes
    assert not d.is_breaking()
    assert any(c.path == "version" and not c.breaking for c in d.changes)


# ── Cosmetic ──

def test_cosmetic_changes_non_breaking():
    a = _base()
    b = _base()
    b.name = "New Name"
    b.icon = "scales"
    b.description = "updated"
    d = diff(a, b)
    assert not d.is_breaking()
    paths = {c.path for c in d.changes}
    assert paths == {"name", "icon", "description"}


# ── Required lists ──

def test_required_pack_removed_is_breaking():
    a = _base()
    b = _base()
    b.required_packs = []
    d = diff(a, b)
    assert d.is_breaking()
    bc = d.breaking_changes()
    assert any("required_packs" in c.path for c in bc)


def test_required_pack_added_is_non_breaking():
    a = _base()
    b = _base()
    b.required_packs = a.required_packs + ["agency_legal_litigation_specialist"]
    d = diff(a, b)
    assert not d.is_breaking()
    assert any(c.kind == "added" for c in d.changes)


def test_required_anthropic_pack_removed_is_breaking():
    a = _base()
    b = _base()
    b.required_anthropic_packs = []
    d = diff(a, b)
    assert d.is_breaking()


def test_required_skill_removed_is_breaking():
    a = _base()
    b = _base()
    b.required_skills = []
    d = diff(a, b)
    assert d.is_breaking()


def test_required_mcp_removed_is_breaking():
    a = _base()
    b = _base()
    b.required_mcps = []
    d = diff(a, b)
    assert d.is_breaking()


# ── Eval suite ──

def test_eval_runner_removed_is_breaking():
    a = _base()
    b = _base()
    b.eval_suite = [b.eval_suite[0]]  # drop citation_accuracy
    d = diff(a, b)
    assert d.is_breaking()
    bc = d.breaking_changes()
    assert any("eval_suite" in c.path for c in bc)


def test_eval_runner_added_is_non_breaking():
    a = _base()
    b = _base()
    b.eval_suite = a.eval_suite + [
        EvalRunner(runner_id="extra_runner", weight=0.1, threshold=0.5),
    ]
    d = diff(a, b)
    assert not d.is_breaking()


def test_eval_threshold_tweak_is_non_breaking():
    a = _base()
    b = _base()
    # bump threshold up
    b.eval_suite[0].threshold = 0.9
    d = diff(a, b)
    assert not d.is_breaking()
    assert any("threshold" in c.path for c in d.changes)


# ── Level rules ──

def test_level_rule_tightened_is_breaking():
    a = _base()
    b = _base()
    b.level_rules[0].min_eval_score = 0.9  # was 0.5
    d = diff(a, b)
    assert d.is_breaking()
    bc = d.breaking_changes()
    assert any("min_eval_score" in c.path for c in bc)


def test_level_rule_loosened_is_non_breaking():
    a = _base()
    b = _base()
    b.level_rules[0].min_eval_score = 0.1  # was 0.5
    d = diff(a, b)
    assert not d.is_breaking()


def test_level_rule_added_is_non_breaking():
    a = _base()
    b = _base()
    b.level_rules.append(LevelRule(from_level="journeyman",
                                   to_level="expert",
                                   min_eval_score=0.7))
    d = diff(a, b)
    assert not d.is_breaking()


def test_level_rule_removed_is_breaking():
    a = _base()
    b = _base()
    b.level_rules = []
    d = diff(a, b)
    assert d.is_breaking()


# ── Safety ──

def test_cite_required_toggled_on_is_breaking():
    a = _base()
    b = _base()
    b.safety.cite_required = True
    d = diff(a, b)
    assert d.is_breaking()
    bc = d.breaking_changes()
    assert any(c.path == "safety.cite_required" for c in bc)


def test_cite_required_toggled_off_is_non_breaking():
    a = _base()
    a.safety.cite_required = True
    b = _base()
    b.safety.cite_required = False
    d = diff(a, b)
    assert not d.is_breaking()


def test_confidence_threshold_tightened_is_breaking():
    a = _base()
    b = _base()
    b.safety.confidence_threshold = 0.9  # was 0.5
    d = diff(a, b)
    assert d.is_breaking()


def test_confidence_threshold_loosened_is_non_breaking():
    a = _base()
    b = _base()
    b.safety.confidence_threshold = 0.1
    d = diff(a, b)
    assert not d.is_breaking()


def test_refuse_topic_added_is_breaking():
    a = _base()
    b = _base()
    b.safety.refuse_topics = ["criminal_advice"]
    d = diff(a, b)
    assert d.is_breaking()


def test_refuse_topic_removed_is_non_breaking():
    a = _base()
    a.safety.refuse_topics = ["criminal_advice"]
    b = _base()
    d = diff(a, b)
    assert not d.is_breaking()


def test_disclaimer_change_is_non_breaking():
    a = _base()
    b = _base()
    b.safety.disclaimer = "Updated disclaimer."
    d = diff(a, b)
    assert not d.is_breaking()


# ── Diff metadata + summary ──

def test_summary_lists_all_changes():
    a = _base()
    b = _base()
    b.name = "Renamed"
    b.required_packs = []  # breaking
    d = diff(a, b)
    s = d.summary()
    assert "BREAKING" in s
    assert "non-breaking" in s
    # Both changes mentioned
    assert "name" in s
    assert "required_packs" in s


def test_breaking_and_non_breaking_partition():
    a = _base()
    b = _base()
    b.required_packs = []  # breaking
    b.name = "Renamed"      # non-breaking
    d = diff(a, b)
    assert any(c.breaking for c in d.breaking_changes())
    assert all(not c.breaking for c in d.non_breaking_changes())
    # Partition is total: every change is in exactly one half
    assert len(d.breaking_changes()) + len(d.non_breaking_changes()) == len(d.changes)
    for c in d.changes:
        assert c in d.breaking_changes() or c in d.non_breaking_changes()


def test_diff_records_old_new_versions():
    a = _base()
    b = _base()
    b.version = "1.1"
    d = diff(a, b)
    assert d.old_version == "1.0"
    assert d.new_version == "1.1"


def test_change_summary_format():
    c_mod = Change("foo.bar", "modified", True, old=1, new=2)
    assert "BREAKING" in c_mod.summary()
    assert "1" in c_mod.summary() and "2" in c_mod.summary()
    c_add = Change("foo.x", "added", False, new="z")
    assert "added" in c_add.summary()
    c_rem = Change("foo.y", "removed", False, old="w")
    assert "removed" in c_rem.summary()
