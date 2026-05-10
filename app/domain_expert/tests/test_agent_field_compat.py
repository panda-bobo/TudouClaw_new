"""Phase 0 regression test: agent dataclass with new expert_* fields.

Hard requirement: old agents.json records (no expert_* keys) must load
cleanly with all 5 new fields defaulting empty/zero.

Spec ref: docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md §3.2
"""
from __future__ import annotations

import pytest

from app.agent import Agent


def test_old_agent_dict_loads_cleanly():
    """Simulate an agents.json record from BEFORE Phase 0 — no expert_* keys.

    All 5 new expert fields must default to safe empty values, and existing
    fields must be preserved exactly.
    """
    old_dict = {
        "id": "test123",
        "name": "test-agent",
        "role": "default",
        "model": "gpt-4o-mini",
        "bound_prompt_packs": ["pack_a", "pack_b"],
        "granted_skills": [],
        # NO expert_* fields — pre-Phase-0 record
    }
    agent = Agent.from_persist_dict(old_dict)
    # All 5 expert fields default cleanly
    assert agent.expert_specialty == ""
    assert agent.expert_template_version == ""
    assert agent.expert_level == "novice"
    assert agent.expert_lora_version == ""
    assert agent.expert_initialized_at == 0.0
    # Existing fields preserved exactly
    assert agent.id == "test123"
    assert agent.name == "test-agent"
    assert "pack_a" in agent.bound_prompt_packs
    assert "pack_b" in agent.bound_prompt_packs


def test_round_trip_with_expert_fields_set():
    """An agent with expert_* fields set: serialize → deserialize round-trip
    preserves all field values exactly."""
    a = Agent(id="ex1", name="expert-test")
    a.expert_specialty = "legal"
    a.expert_template_version = "1.0"
    a.expert_level = "journeyman"
    a.expert_lora_version = "v2"
    a.expert_initialized_at = 1234567890.0

    d = a.to_persist_dict()

    # to_persist_dict must include all 5 fields explicitly
    assert d["expert_specialty"] == "legal"
    assert d["expert_template_version"] == "1.0"
    assert d["expert_level"] == "journeyman"
    assert d["expert_lora_version"] == "v2"
    assert d["expert_initialized_at"] == 1234567890.0

    # Round-trip via from_persist_dict
    a2 = Agent.from_persist_dict(d)
    assert a2.expert_specialty == "legal"
    assert a2.expert_template_version == "1.0"
    assert a2.expert_level == "journeyman"
    assert a2.expert_lora_version == "v2"
    assert a2.expert_initialized_at == 1234567890.0


def test_to_dict_surfaces_expert_fields():
    """Regular to_dict (UI-facing) must include expert_* fields so the
    workspace UI can read agent state without an extra fetch."""
    a = Agent(id="ui1", name="ui-test")
    a.expert_specialty = "medical"
    a.expert_level = "expert"

    d = a.to_dict()

    assert d["expert_specialty"] == "medical"
    assert d["expert_level"] == "expert"
    # Defaults for unset fields
    assert d["expert_template_version"] == ""
    assert d["expert_lora_version"] == ""
    assert d["expert_initialized_at"] == 0.0


def test_default_empty_agent_is_pre_phase_0_compatible():
    """An agent created with no expert config behaves identically to a
    pre-Phase-0 agent — empty defaults across the board."""
    a = Agent(id="plain", name="plain-agent")
    assert a.expert_specialty == ""
    assert a.expert_template_version == ""
    assert a.expert_level == "novice"  # design default
    assert a.expert_lora_version == ""
    assert a.expert_initialized_at == 0.0
    # Round trip preserves
    d = a.to_persist_dict()
    a2 = Agent.from_persist_dict(d)
    assert a2.expert_specialty == ""
    assert a2.expert_level == "novice"


def test_corrupted_field_types_default_safely():
    """Defensive: if persisted JSON has corrupt types (e.g. None where
    str expected), from_persist_dict should default rather than crash."""
    bad_dict = {
        "id": "corrupt",
        "name": "x",
        "expert_specialty": None,         # None instead of empty str
        "expert_template_version": None,
        "expert_level": None,             # None instead of "novice"
        "expert_lora_version": None,
        "expert_initialized_at": None,    # None instead of 0.0
    }
    agent = Agent.from_persist_dict(bad_dict)
    assert agent.expert_specialty == ""
    assert agent.expert_template_version == ""
    assert agent.expert_level == "novice"
    assert agent.expert_lora_version == ""
    assert agent.expert_initialized_at == 0.0
