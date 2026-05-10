"""SpecialtyTemplate — full schema for a specialty (e.g. legal-expert).

A SpecialtyTemplate is the "recipe" for cultivating an expert agent:
which prompt packs to bind, which skills to grant, eval suite, RAG chunker
config, level rules (novice/journeyman/expert/master), corpus sources,
training params, MCP tools, safety rails.

Templates are shipped as YAML under ``app/data/specialty_templates/`` and
loaded via :mod:`app.domain_expert.template_loader` (Track D Task D2).
The loader validates against the JSONSchema produced by :func:`schema()`.

Design ref: docs spec §3.1 (SpecialtyTemplate), §3.4 (lifecycle), §3.9.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Sub-structs (each is a dataclass with sensible defaults)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CorpusSource:
    """One ingestion source for the agent's RAG corpus."""
    type: str                              # "url" | "file" | "git" | "rss"
    location: str = ""                     # URL / path / repo / feed
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChunkerConfig:
    """How to chunk the corpus before embedding."""
    strategy: str = "semantic"             # "semantic" | "fixed" | "structural"
    max_tokens: int = 512
    overlap_tokens: int = 64
    respect_boundaries: bool = True        # don't split mid-sentence/section

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrainingConfig:
    """RAFT/LoRA training params (consumed by Track C)."""
    base_model: str = ""                   # e.g. "Qwen/Qwen2.5-7B-Instruct"
    raft_recipe: str = "default"           # named recipe under training/
    lora_rank: int = 16
    lora_alpha: int = 32
    learning_rate: float = 2e-4
    max_steps: int = 0                     # 0 = recipe default
    distractor_count: int = 4              # RAFT distractors per Q
    eval_split: float = 0.1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalRunner:
    """One eval that gates level promotion."""
    runner_id: str                         # registered with Track C, e.g. "legalbench_zh"
    weight: float = 1.0                    # weight in promotion score
    threshold: float = 0.0                 # min score to count as "passed"
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LevelRule:
    """Promotion rule from one level to the next."""
    from_level: str                        # "novice" | "journeyman" | "expert"
    to_level: str                          # "journeyman" | "expert" | "master"
    min_eval_score: float = 0.0            # weighted score across runners
    min_corpus_chunks: int = 0             # at least N chunks indexed
    min_traces: int = 0                    # at least N cleaned Q/A traces

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SafetyRails:
    """Specialty-specific guardrails."""
    cite_required: bool = False            # answers must include citations
    confidence_threshold: float = 0.0      # below this → "I don't know"
    refuse_topics: list[str] = field(default_factory=list)
    disclaimer: str = ""                   # appended to answers when set

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Top-level SpecialtyTemplate
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SpecialtyTemplate:
    """Parsed specialty template. Construct via :func:`from_dict` (which
    is what the loader uses after schema validation)."""
    # Required identity
    id: str                                # "legal-expert"
    version: str                           # "1.0" — semver-ish "MAJOR.MINOR"
    name: str                              # "法律专家"
    specialty: str                         # "legal"

    # Display
    icon: str = ""
    description: str = ""

    # Knowledge layer (Track A consumes)
    required_packs: list[str] = field(default_factory=list)        # community catalog ids
    required_anthropic_packs: list[str] = field(default_factory=list)  # akwp_* ids
    required_skills: list[str] = field(default_factory=list)
    required_mcps: list[str] = field(default_factory=list)
    corpus_sources: list[CorpusSource] = field(default_factory=list)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)

    # Training layer (Track C consumes)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    eval_suite: list[EvalRunner] = field(default_factory=list)
    level_rules: list[LevelRule] = field(default_factory=list)

    # Safety
    safety: SafetyRails = field(default_factory=SafetyRails)

    # ── Serialization ──

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already handles nested dataclasses recursively
        return d

    @staticmethod
    def from_dict(d: dict) -> "SpecialtyTemplate":
        """Construct from a parsed-yaml dict. Caller is expected to have
        validated against :func:`schema` first; this method is forgiving
        about missing optional fields but will raise on missing required."""
        if not isinstance(d, dict):
            raise TypeError(f"SpecialtyTemplate.from_dict expected dict, got {type(d)!r}")

        for required in ("id", "version", "name", "specialty"):
            if required not in d or not d[required]:
                raise ValueError(f"SpecialtyTemplate missing required field: {required}")

        corpus = [
            CorpusSource(**_sub(cs, CorpusSource))
            for cs in d.get("corpus_sources", []) or []
        ]
        chunker_d = d.get("chunker") or {}
        chunker = ChunkerConfig(**_sub(chunker_d, ChunkerConfig))

        training_d = d.get("training") or {}
        training = TrainingConfig(**_sub(training_d, TrainingConfig))

        eval_suite = [
            EvalRunner(**_sub(e, EvalRunner))
            for e in d.get("eval_suite", []) or []
        ]
        level_rules = [
            LevelRule(**_sub(lr, LevelRule))
            for lr in d.get("level_rules", []) or []
        ]
        safety_d = d.get("safety") or {}
        safety = SafetyRails(**_sub(safety_d, SafetyRails))

        return SpecialtyTemplate(
            id=str(d["id"]),
            version=str(d["version"]),
            name=str(d["name"]),
            specialty=str(d["specialty"]),
            icon=str(d.get("icon", "")),
            description=str(d.get("description", "")),
            required_packs=list(d.get("required_packs", []) or []),
            required_anthropic_packs=list(d.get("required_anthropic_packs", []) or []),
            required_skills=list(d.get("required_skills", []) or []),
            required_mcps=list(d.get("required_mcps", []) or []),
            corpus_sources=corpus,
            chunker=chunker,
            training=training,
            eval_suite=eval_suite,
            level_rules=level_rules,
            safety=safety,
        )


def _sub(d: dict, cls: type) -> dict:
    """Filter dict to keys the dataclass accepts (defensive — extra
    fields would otherwise raise)."""
    if not isinstance(d, dict):
        return {}
    fields = cls.__dataclass_fields__
    return {k: v for k, v in d.items() if k in fields}


# ─────────────────────────────────────────────────────────────────────────────
# JSONSchema for YAML validation
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SpecialtyTemplate",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "version", "name", "specialty"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "version": {
            "type": "string",
            # MAJOR.MINOR or MAJOR.MINOR.PATCH (semver-ish, all numeric)
            "pattern": r"^\d+\.\d+(\.\d+)?$",
        },
        "name": {"type": "string", "minLength": 1},
        "specialty": {"type": "string", "minLength": 1},
        "icon": {"type": "string"},
        "description": {"type": "string"},

        "required_packs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "required_anthropic_packs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "required_skills": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "required_mcps": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },

        "corpus_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"type": "string",
                             "enum": ["url", "file", "git", "rss"]},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },

        "chunker": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "strategy": {"type": "string",
                             "enum": ["semantic", "fixed", "structural"]},
                "max_tokens": {"type": "integer", "minimum": 1},
                "overlap_tokens": {"type": "integer", "minimum": 0},
                "respect_boundaries": {"type": "boolean"},
            },
        },

        "training": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "base_model": {"type": "string"},
                "raft_recipe": {"type": "string"},
                "lora_rank": {"type": "integer", "minimum": 1},
                "lora_alpha": {"type": "integer", "minimum": 1},
                "learning_rate": {"type": "number", "exclusiveMinimum": 0},
                "max_steps": {"type": "integer", "minimum": 0},
                "distractor_count": {"type": "integer", "minimum": 0},
                "eval_split": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },

        "eval_suite": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["runner_id"],
                "properties": {
                    "runner_id": {"type": "string", "minLength": 1},
                    "weight": {"type": "number", "minimum": 0},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "description": {"type": "string"},
                },
            },
        },

        "level_rules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from_level", "to_level"],
                "properties": {
                    "from_level": {"type": "string",
                                   "enum": ["novice", "journeyman", "expert"]},
                    "to_level": {"type": "string",
                                 "enum": ["journeyman", "expert", "master"]},
                    "min_eval_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "min_corpus_chunks": {"type": "integer", "minimum": 0},
                    "min_traces": {"type": "integer", "minimum": 0},
                },
            },
        },

        "safety": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cite_required": {"type": "boolean"},
                "confidence_threshold": {"type": "number",
                                         "minimum": 0, "maximum": 1},
                "refuse_topics": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "disclaimer": {"type": "string"},
            },
        },
    },
}


def schema() -> dict[str, Any]:
    """Return a deep-copy of the JSONSchema describing a valid template
    YAML. Used by :mod:`app.domain_expert.template_loader` to validate
    parsed YAML before constructing the dataclass."""
    import copy
    return copy.deepcopy(_SCHEMA)
