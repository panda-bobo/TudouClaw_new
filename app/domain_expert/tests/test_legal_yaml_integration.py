"""Track D Task D5 — integration smoke test against the shipped legal.yaml.

Verifies the real ``app/data/specialty_templates/legal.yaml`` round-trips
through the loader → diff → bundle_apply pipeline against a fake Agent
exactly the way V1/V2 verticals will wire it.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.domain_expert.bundle_apply import apply_bundle
from app.domain_expert.template import SpecialtyTemplate
from app.domain_expert.template_diff import diff
from app.domain_expert.template_loader import load, list_available


# ── Fake Agent matching the real Agent's expert_* surface ──

@dataclass
class FakeAgent:
    id: str = "agent_legal_001"
    expert_specialty: str = ""
    expert_template_version: str = ""
    expert_level: str = "novice"
    expert_lora_version: str = ""
    expert_initialized_at: float = 0.0


# ── End-to-end integration ──

def test_legal_yaml_listed_as_available():
    avail = list_available()
    assert "legal" in avail


def test_legal_yaml_loads_cleanly():
    """load("legal") returns a fully-populated SpecialtyTemplate."""
    t = load("legal")
    assert isinstance(t, SpecialtyTemplate)
    assert t.id == "legal-expert"
    assert t.specialty == "legal"
    assert t.version == "1.0"
    assert t.name == "法律专家"


def test_legal_yaml_required_packs_present():
    """All four community packs from spec §3.1.2 are referenced."""
    t = load("legal")
    expected_packs = {
        "agency_legal_lawyer",
        "agency_legal_legal_counsel",
        "agency_legal_contract_lawyer",
        "agency_legal_litigation_specialist",
    }
    assert set(t.required_packs) == expected_packs


def test_legal_yaml_anthropic_packs_present():
    """All eight akwp_legal_* packs from prompt are referenced."""
    t = load("legal")
    expected = {
        "akwp_legal_brief",
        "akwp_legal_review-contract",
        "akwp_legal_compliance-check",
        "akwp_legal_triage-nda",
        "akwp_legal_legal-risk-assessment",
        "akwp_legal_legal-response",
        "akwp_legal_meeting-briefing",
        "akwp_legal_vendor-check",
    }
    assert set(t.required_anthropic_packs) == expected


def test_legal_yaml_eval_runners_match_track_c_contract():
    """Track C registers exactly these two runner IDs."""
    t = load("legal")
    runners = {e.runner_id for e in t.eval_suite}
    assert runners == {"legalbench_zh", "citation_accuracy"}


def test_legal_yaml_safety_rails():
    t = load("legal")
    assert t.safety.cite_required is True
    assert 0.0 < t.safety.confidence_threshold <= 1.0
    assert t.safety.disclaimer  # non-empty
    assert len(t.safety.refuse_topics) >= 1


def test_legal_yaml_full_level_progression():
    """All three transitions (novice→journeyman→expert→master) exist."""
    t = load("legal")
    transitions = {(r.from_level, r.to_level) for r in t.level_rules}
    assert transitions == {
        ("novice", "journeyman"),
        ("journeyman", "expert"),
        ("expert", "master"),
    }


def test_legal_yaml_chunker_is_structural():
    """Chinese statutes need section-aware chunking."""
    t = load("legal")
    assert t.chunker.strategy == "structural"


# ── Diff smoke ──

def test_diff_legal_against_itself_is_empty():
    t1 = load("legal")
    t2 = load("legal")
    d = diff(t1, t2)
    assert d.is_empty()


def test_diff_detects_breaking_pack_removal_against_legal():
    """Synthetic v1.1 that drops a required pack must be flagged breaking."""
    t1 = load("legal")
    src = t1.to_dict()
    src["version"] = "1.1"
    src["required_packs"] = src["required_packs"][:-1]  # drop one
    t2 = SpecialtyTemplate.from_dict(src)
    d = diff(t1, t2)
    assert d.is_breaking()


# ── Bundle apply smoke (V1/V2-style wiring) ──

def test_apply_legal_to_fresh_agent():
    """Full flow: load → apply_bundle → agent has expert_* + result populated."""
    t = load("legal")
    a = FakeAgent()
    saves: list[int] = []
    grants: list[tuple[str, str]] = []
    r = apply_bundle(
        t, a,
        save_callback=lambda: saves.append(1),
        skill_grant_callback=lambda aid, sid: grants.append((aid, sid)),
        now=lambda: 1700000000.0,
    )
    # Agent state stamped
    assert a.expert_specialty == "legal"
    assert a.expert_template_version == "1.0"
    assert a.expert_initialized_at == 1700000000.0
    assert a.expert_level == "novice"

    # Result populated
    assert len(r.packs_bound) == 4
    assert len(r.anthropic_packs_bound) == 8
    assert set(r.skills_granted) == {"cite_check", "legal_quote_extract"}
    assert r.is_complete()
    assert r.saved is True

    # Callbacks fired
    assert saves == [1]
    assert len(grants) == 2
    for aid, _sid in grants:
        assert aid == "agent_legal_001"


def test_apply_legal_idempotent_second_call_is_noop():
    t = load("legal")
    a = FakeAgent()
    apply_bundle(t, a, now=lambda: 1000.0)
    saves: list = []
    r2 = apply_bundle(t, a, save_callback=lambda: saves.append(1),
                      now=lambda: 9999.0)
    assert saves == []
    assert r2.saved is False
    assert a.expert_initialized_at == 1000.0  # not bumped


def test_apply_legal_with_missing_resources_reports_them():
    """V2 vertical scenario: registries say "not installed" → result lists missing."""
    t = load("legal")
    a = FakeAgent()
    r = apply_bundle(
        t, a,
        pack_exists_callback=lambda p: False,
        anthropic_pack_exists_callback=lambda p: False,
        skill_exists_callback=lambda s: False,
    )
    assert r.packs_bound == []
    assert r.anthropic_packs_bound == []
    assert r.skills_granted == []
    assert len(r.missing_packs) == 4
    assert len(r.missing_anthropic_packs) == 8
    assert len(r.missing_skills) == 2
    assert not r.is_complete()
    # Agent expert_* was still stamped (re-cultivation can proceed once
    # registries are populated)
    assert a.expert_specialty == "legal"


def test_apply_legal_partial_resources():
    """Mixed install state — only agency_legal_lawyer exists, rest don't."""
    t = load("legal")
    a = FakeAgent()
    r = apply_bundle(
        t, a,
        pack_exists_callback=lambda p: p == "agency_legal_lawyer",
    )
    assert r.packs_bound == ["agency_legal_lawyer"]
    assert len(r.missing_packs) == 3
    assert not r.is_complete()
