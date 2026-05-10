"""Track D Task D1 — full SpecialtyTemplate dataclass + JSONSchema."""
from __future__ import annotations

import jsonschema
import pytest

from app.domain_expert.template import (
    ChunkerConfig,
    CorpusSource,
    EvalRunner,
    LevelRule,
    SafetyRails,
    SpecialtyTemplate,
    TrainingConfig,
    schema,
)


# ── from_dict happy path ──

def test_from_dict_minimal_required_only():
    t = SpecialtyTemplate.from_dict({
        "id": "legal-expert", "version": "1.0",
        "name": "法律专家", "specialty": "legal",
    })
    assert t.id == "legal-expert"
    assert t.specialty == "legal"
    # Defaults are populated for everything optional
    assert t.required_packs == []
    assert t.eval_suite == []
    assert isinstance(t.chunker, ChunkerConfig)
    assert t.chunker.strategy == "semantic"
    assert isinstance(t.training, TrainingConfig)
    assert isinstance(t.safety, SafetyRails)


def test_from_dict_full_round_trip():
    src = {
        "id": "legal-expert", "version": "1.2",
        "name": "法律专家", "specialty": "legal",
        "icon": "balance", "description": "Chinese legal expert",
        "required_packs": ["agency_legal_lawyer"],
        "required_anthropic_packs": ["akwp_legal_brief"],
        "required_skills": ["cite_check"],
        "required_mcps": ["mcp_lawdb"],
        "corpus_sources": [
            {"type": "url", "location": "https://example.com/laws",
             "description": "PRC laws"},
        ],
        "chunker": {"strategy": "structural", "max_tokens": 1024,
                    "overlap_tokens": 128, "respect_boundaries": True},
        "training": {"base_model": "Qwen/Qwen2.5-7B-Instruct",
                     "raft_recipe": "legal-v1",
                     "lora_rank": 32, "lora_alpha": 64,
                     "learning_rate": 1e-4, "max_steps": 1000,
                     "distractor_count": 5, "eval_split": 0.1},
        "eval_suite": [
            {"runner_id": "legalbench_zh", "weight": 0.7,
             "threshold": 0.6, "description": "translated legalbench"},
            {"runner_id": "citation_accuracy", "weight": 0.3,
             "threshold": 0.8, "description": "citation correctness"},
        ],
        "level_rules": [
            {"from_level": "novice", "to_level": "journeyman",
             "min_eval_score": 0.5, "min_corpus_chunks": 100,
             "min_traces": 0},
            {"from_level": "journeyman", "to_level": "expert",
             "min_eval_score": 0.7, "min_corpus_chunks": 1000,
             "min_traces": 200},
        ],
        "safety": {"cite_required": True, "confidence_threshold": 0.5,
                   "refuse_topics": ["criminal_advice"],
                   "disclaimer": "Not legal advice."},
    }
    t = SpecialtyTemplate.from_dict(src)
    # Check every layer
    assert t.icon == "balance"
    assert t.required_packs == ["agency_legal_lawyer"]
    assert t.required_anthropic_packs == ["akwp_legal_brief"]
    assert len(t.corpus_sources) == 1
    assert isinstance(t.corpus_sources[0], CorpusSource)
    assert t.corpus_sources[0].type == "url"
    assert t.chunker.strategy == "structural"
    assert t.chunker.max_tokens == 1024
    assert t.training.base_model == "Qwen/Qwen2.5-7B-Instruct"
    assert t.training.lora_rank == 32
    assert len(t.eval_suite) == 2
    assert isinstance(t.eval_suite[0], EvalRunner)
    assert t.eval_suite[0].runner_id == "legalbench_zh"
    assert len(t.level_rules) == 2
    assert isinstance(t.level_rules[0], LevelRule)
    assert t.level_rules[1].to_level == "expert"
    assert t.safety.cite_required is True
    assert t.safety.refuse_topics == ["criminal_advice"]


def test_from_dict_round_trip_to_dict():
    """to_dict ∘ from_dict is idempotent."""
    src = {
        "id": "legal-expert", "version": "1.0",
        "name": "Legal", "specialty": "legal",
        "required_packs": ["x"],
        "eval_suite": [{"runner_id": "r1"}],
    }
    t1 = SpecialtyTemplate.from_dict(src)
    d1 = t1.to_dict()
    t2 = SpecialtyTemplate.from_dict(d1)
    assert t1 == t2


# ── from_dict error cases ──

@pytest.mark.parametrize("missing", ["id", "version", "name", "specialty"])
def test_from_dict_rejects_missing_required(missing):
    full = {
        "id": "x", "version": "1.0",
        "name": "x", "specialty": "x",
    }
    full.pop(missing)
    with pytest.raises(ValueError, match=missing):
        SpecialtyTemplate.from_dict(full)


def test_from_dict_rejects_empty_required():
    with pytest.raises(ValueError):
        SpecialtyTemplate.from_dict({
            "id": "", "version": "1.0",
            "name": "x", "specialty": "x",
        })


def test_from_dict_rejects_non_dict():
    with pytest.raises(TypeError):
        SpecialtyTemplate.from_dict("not a dict")


def test_from_dict_ignores_unknown_subfields():
    """Defensive: extra keys in nested config dicts shouldn't blow up."""
    t = SpecialtyTemplate.from_dict({
        "id": "x", "version": "1.0", "name": "x", "specialty": "x",
        "chunker": {"strategy": "fixed", "garbage_field": 42},
    })
    assert t.chunker.strategy == "fixed"


# ── schema() ──

def test_schema_validates_minimal():
    s = schema()
    jsonschema.validate(
        {"id": "x", "version": "1.0", "name": "x", "specialty": "x"}, s
    )


def test_schema_rejects_missing_id():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"version": "1.0", "name": "x", "specialty": "x"},
            schema(),
        )


def test_schema_rejects_bad_version_format():
    """Version must look like MAJOR.MINOR."""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"id": "x", "version": "1", "name": "x", "specialty": "x"},
            schema(),
        )


def test_schema_rejects_unknown_top_level_field():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"id": "x", "version": "1.0", "name": "x", "specialty": "x",
             "ohno_typo_field": True},
            schema(),
        )


def test_schema_rejects_bad_corpus_type():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"id": "x", "version": "1.0", "name": "x", "specialty": "x",
             "corpus_sources": [{"type": "bogus", "location": "a"}]},
            schema(),
        )


def test_schema_rejects_bad_chunker_strategy():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"id": "x", "version": "1.0", "name": "x", "specialty": "x",
             "chunker": {"strategy": "bogus"}},
            schema(),
        )


def test_schema_rejects_bad_level_transition():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"id": "x", "version": "1.0", "name": "x", "specialty": "x",
             "level_rules": [{"from_level": "bogus", "to_level": "expert"}]},
            schema(),
        )


def test_schema_returns_independent_copies():
    """Mutating one returned schema should not affect later calls."""
    s1 = schema()
    s1["properties"]["id"] = {"type": "boolean"}  # mutate
    s2 = schema()
    assert s2["properties"]["id"]["type"] == "string"


def test_schema_accepts_full_legal_yaml_shape():
    """The shape we'll ship in legal.yaml must validate cleanly."""
    valid = {
        "id": "legal-expert", "version": "1.0",
        "name": "法律专家", "specialty": "legal",
        "required_packs": ["agency_legal_lawyer"],
        "required_anthropic_packs": ["akwp_legal_brief"],
        "eval_suite": [{"runner_id": "legalbench_zh", "weight": 0.5,
                        "threshold": 0.5}],
        "level_rules": [{"from_level": "novice", "to_level": "journeyman",
                         "min_eval_score": 0.5}],
        "safety": {"cite_required": True, "disclaimer": "Not legal advice."},
    }
    jsonschema.validate(valid, schema())
