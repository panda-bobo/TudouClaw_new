"""Track D Task D4 — bundle apply engine (idempotent, mock-friendly)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.domain_expert.bundle_apply import (
    BundleApplyResult,
    apply_bundle,
)
from app.domain_expert.template import SpecialtyTemplate


# ── Fixture factory ──

@dataclass
class FakeAgent:
    """Mirror of the real Agent's expert_* fields, plus .id."""
    id: str = "ag1"
    expert_specialty: str = ""
    expert_template_version: str = ""
    expert_level: str = "novice"
    expert_lora_version: str = ""
    expert_initialized_at: float = 0.0


def _tpl(**overrides) -> SpecialtyTemplate:
    base = {
        "id": "legal-expert", "version": "1.0",
        "name": "Legal", "specialty": "legal",
        "required_packs": ["agency_legal_lawyer", "agency_legal_legal_counsel"],
        "required_anthropic_packs": ["akwp_legal_brief", "akwp_legal_review-contract"],
        "required_skills": ["cite_check"],
        "required_mcps": ["mcp_lawdb"],
    }
    base.update(overrides)
    return SpecialtyTemplate.from_dict(base)


# ── Happy path ──

def test_apply_stamps_expert_fields():
    a = FakeAgent()
    fixed_now = lambda: 1234567890.0
    r = apply_bundle(_tpl(), a, now=fixed_now)
    assert a.expert_specialty == "legal"
    assert a.expert_template_version == "1.0"
    assert a.expert_initialized_at == 1234567890.0
    assert a.expert_level == "novice"
    assert r.template_id == "legal-expert"
    assert r.template_version == "1.0"
    assert r.agent_id == "ag1"
    assert r.initialized_at == 1234567890.0


def test_apply_binds_all_packs_and_skills_when_no_existence_check():
    a = FakeAgent()
    granted: list[tuple[str, str]] = []
    r = apply_bundle(_tpl(), a,
                     skill_grant_callback=lambda aid, sid: granted.append((aid, sid)))
    assert r.packs_bound == ["agency_legal_lawyer", "agency_legal_legal_counsel"]
    assert r.anthropic_packs_bound == ["akwp_legal_brief", "akwp_legal_review-contract"]
    assert r.skills_granted == ["cite_check"]
    assert r.mcps_required == ["mcp_lawdb"]
    assert granted == [("ag1", "cite_check")]
    assert r.is_complete()


def test_apply_calls_save_callback_when_changes():
    a = FakeAgent()
    saves = []
    r = apply_bundle(_tpl(), a, save_callback=lambda: saves.append(1))
    assert saves == [1]
    assert r.saved is True


# ── Idempotency ──

def test_idempotent_reapply_does_not_double_save():
    a = FakeAgent()
    saves: list[int] = []
    grants: list[tuple[str, str]] = []
    r1 = apply_bundle(_tpl(), a,
                      save_callback=lambda: saves.append(1),
                      skill_grant_callback=lambda aid, sid: grants.append((aid, sid)))
    r2 = apply_bundle(_tpl(), a,
                      save_callback=lambda: saves.append(1),
                      skill_grant_callback=lambda aid, sid: grants.append((aid, sid)))
    # First save fires; second is a no-op since nothing changed
    assert len(saves) == 1
    # Second result still reports the bound state
    assert r2.packs_bound == r1.packs_bound
    assert r2.is_complete()
    assert r2.saved is False


def test_idempotent_initialized_at_preserved():
    """initialized_at is not bumped on subsequent applies of the same template."""
    a = FakeAgent()
    apply_bundle(_tpl(), a, now=lambda: 1000.0)
    first = a.expert_initialized_at
    apply_bundle(_tpl(), a, now=lambda: 9999.0)
    assert a.expert_initialized_at == first


def test_dedupes_within_template():
    """If a template lists the same skill twice, callback fires once."""
    tpl = _tpl(required_skills=["cite_check", "cite_check", "summarize"])
    a = FakeAgent()
    grants: list[tuple[str, str]] = []
    r = apply_bundle(tpl, a,
                     skill_grant_callback=lambda aid, sid: grants.append((aid, sid)))
    assert grants == [("ag1", "cite_check"), ("ag1", "summarize")]
    assert r.skills_granted == ["cite_check", "summarize"]


# ── Re-cultivation: changing template / specialty ──

def test_changing_template_version_resets_initialized_at():
    a = FakeAgent()
    apply_bundle(_tpl(version="1.0"), a, now=lambda: 1000.0)
    apply_bundle(_tpl(version="1.1"), a, now=lambda: 2000.0)
    assert a.expert_template_version == "1.1"
    assert a.expert_initialized_at == 2000.0


def test_changing_specialty_resets_level():
    a = FakeAgent(expert_specialty="legal", expert_level="expert",
                  expert_template_version="1.0",
                  expert_lora_version="v3")
    new_tpl = _tpl(id="medical-expert", specialty="medical")
    r = apply_bundle(new_tpl, a)
    assert a.expert_specialty == "medical"
    assert a.expert_level == "novice"  # reset
    assert a.expert_lora_version == ""  # reset


# ── Existence checks → missing_* fields ──

def test_missing_packs_reported_when_existence_check_fails():
    a = FakeAgent()
    r = apply_bundle(
        _tpl(), a,
        pack_exists_callback=lambda p: p == "agency_legal_lawyer",
    )
    assert r.packs_bound == ["agency_legal_lawyer"]
    assert r.missing_packs == ["agency_legal_legal_counsel"]
    assert not r.is_complete()


def test_missing_anthropic_packs_reported():
    a = FakeAgent()
    r = apply_bundle(
        _tpl(), a,
        anthropic_pack_exists_callback=lambda p: False,
    )
    assert r.anthropic_packs_bound == []
    assert set(r.missing_anthropic_packs) == {
        "akwp_legal_brief", "akwp_legal_review-contract"
    }
    assert not r.is_complete()


def test_missing_skills_reported_when_existence_check_fails():
    a = FakeAgent()
    grants: list = []
    r = apply_bundle(
        _tpl(), a,
        skill_grant_callback=lambda aid, sid: grants.append((aid, sid)),
        skill_exists_callback=lambda s: False,
    )
    assert r.skills_granted == []
    assert r.missing_skills == ["cite_check"]
    assert grants == []  # never called when missing
    assert not r.is_complete()


def test_missing_mcps_reported():
    a = FakeAgent()
    r = apply_bundle(
        _tpl(), a,
        mcp_exists_callback=lambda m: False,
    )
    assert r.mcps_required == []
    assert r.missing_mcps == ["mcp_lawdb"]
    assert not r.is_complete()


def test_skill_grant_callback_exception_marks_missing():
    """If skill_grant_callback raises, we record the skill as missing
    rather than crashing the whole apply."""
    def boom(_aid, _sid):
        raise RuntimeError("registry not ready")

    a = FakeAgent()
    r = apply_bundle(_tpl(), a, skill_grant_callback=boom)
    assert "cite_check" in r.missing_skills
    assert r.skills_granted == []
    assert not r.is_complete()


# ── is_complete + summary ──

def test_is_complete_when_no_existence_callbacks():
    """No existence checks wired → everything assumed present → complete."""
    a = FakeAgent()
    r = apply_bundle(_tpl(), a)
    assert r.is_complete()


def test_summary_lists_missing_when_incomplete():
    a = FakeAgent()
    r = apply_bundle(
        _tpl(), a,
        pack_exists_callback=lambda p: False,
    )
    s = r.summary()
    assert "MISSING" in s
    assert "agency_legal_lawyer" in s


def test_summary_clean_when_complete():
    a = FakeAgent()
    r = apply_bundle(_tpl(), a)
    s = r.summary()
    assert "MISSING" not in s
    assert "legal-expert" in s


# ── Argument validation ──

def test_apply_requires_specialty_template():
    with pytest.raises(TypeError):
        apply_bundle({"not": "a template"}, FakeAgent())


def test_apply_requires_agent_with_id():
    @dataclass
    class NoId:
        name: str = "x"

    with pytest.raises(TypeError):
        apply_bundle(_tpl(), NoId())


def test_apply_requires_nonempty_agent_id():
    a = FakeAgent(id="")
    with pytest.raises(ValueError):
        apply_bundle(_tpl(), a)


# ── Save callback skipped when no changes ──

def test_save_callback_not_called_when_nothing_changes():
    a = FakeAgent()
    apply_bundle(_tpl(), a)  # first apply
    saves: list = []
    r = apply_bundle(_tpl(), a, save_callback=lambda: saves.append(1))
    assert saves == []
    assert r.saved is False


# ── Empty template ──

def test_apply_empty_template():
    """A template with no packs/skills/mcps still stamps expert fields."""
    tpl = SpecialtyTemplate.from_dict({
        "id": "empty", "version": "1.0", "name": "x", "specialty": "empty",
    })
    a = FakeAgent()
    r = apply_bundle(tpl, a)
    assert a.expert_specialty == "empty"
    assert r.packs_bound == []
    assert r.skills_granted == []
    assert r.is_complete()


# ── BundleApplyResult dataclass ──

def test_result_default_dataclass_state():
    r = BundleApplyResult(
        template_id="x", template_version="1.0",
        specialty="x", agent_id="ag1",
    )
    assert r.is_complete()
    assert r.packs_bound == []
    assert r.saved is False
    assert r.seeds_ingested == []


# ── R3: kb_seed loader hook ──

def _tpl_with_seeds(**overrides):
    """Template that carries kb_seeds for the seed-hook tests."""
    return _tpl(kb_seeds=[
        {"file": "civil_code.md", "type": "law", "title": "民法典"},
        {"file": "sop.md", "type": "sop"},
    ], **overrides)


def test_seed_loader_callback_not_invoked_when_template_has_no_seeds():
    a = FakeAgent()
    calls = []
    apply_bundle(
        _tpl(),  # no kb_seeds
        a,
        seed_loader_callback=lambda aid, t: calls.append((aid, t)) or [],
    )
    assert calls == []


def test_seed_loader_callback_not_invoked_when_callback_omitted():
    """Backward compat: when callback is None, no seeding attempt is made."""
    a = FakeAgent()
    r = apply_bundle(_tpl_with_seeds(), a)
    assert r.seeds_ingested == []


def test_seed_loader_callback_runs_and_results_recorded():
    """When the callback returns CorpusSourceEntry-shaped objects, their
    source_ids are surfaced on result.seeds_ingested."""
    from dataclasses import dataclass

    @dataclass
    class FakeEntry:
        source_id: str

    a = FakeAgent()
    captured: list = []

    def loader(agent_id, template):
        captured.append((agent_id, template.id))
        return [FakeEntry("seed_civil_code"), FakeEntry("seed_sop")]

    r = apply_bundle(
        _tpl_with_seeds(), a,
        seed_loader_callback=loader,
    )
    assert captured == [("ag1", "legal-expert")]
    assert r.seeds_ingested == ["seed_civil_code", "seed_sop"]


def test_seed_loader_callback_exception_does_not_raise():
    """Seed-loader errors are best-effort — bundle_apply must still
    return a usable result (Track D contract: don't raise out)."""
    a = FakeAgent()

    def boom(aid, t):
        raise RuntimeError("disk full or similar")

    r = apply_bundle(_tpl_with_seeds(), a, seed_loader_callback=boom)
    assert r.seeds_ingested == []
    # Other state still populated
    assert a.expert_specialty == "legal"


def test_seed_loader_callback_runs_on_idempotent_reapply():
    """Re-applying a template with seeds should re-run the loader so
    updated seed files reach the agent's KB even when nothing else
    changed (the loader itself is idempotent)."""
    a = FakeAgent()
    apply_bundle(_tpl_with_seeds(), a)  # first apply

    calls: list = []

    def loader(aid, t):
        calls.append(aid)
        return []

    apply_bundle(_tpl_with_seeds(), a, seed_loader_callback=loader)
    assert calls == ["ag1"]
