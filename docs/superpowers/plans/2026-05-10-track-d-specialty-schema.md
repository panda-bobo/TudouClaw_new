# Track D: Specialty Schema & Loader Implementation Plan

> **Independent track.** Forks from `phase-0-complete`. No coordination with Tracks A/B/C until vertical V2.
>
> **For agentic workers:** Use superpowers:executing-plans.

**Spec reference:** [§3.1 SpecialtyTemplate, §3.4 Cultivation Lifecycle, §3.9.5 Specialty-Pluggable Components](../specs/2026-05-10-agent-specialty-cultivation-design.md)

**Goal:** Build the specialty template subsystem — YAML schema validation, template loader, version diff, bundle apply engine. After Track D, V2 vertical can wire these into API endpoints + UI.

**Architecture:** Pure Python in `app/domain_expert/`. No HTTP / UI. Outputs:
- `template.py` (rich, full schema)
- `template_loader.py` (YAML parse + validate + diff)
- `bundle_apply.py` (given template + agent → batch grant + bind + install)

**Tech Stack:** PyYAML, jsonschema, no other new deps.

**Verification:** pytest only. Each task = unit-tested module.

---

## File Structure

```
app/domain_expert/
├── template.py                       # (Phase 0 stub) → expand to full SpecialtyTemplate
├── template_loader.py                # NEW: YAML parse + validate
├── template_diff.py                  # NEW: version comparison
├── bundle_apply.py                   # NEW: batch capability bundle apply

app/data/specialty_templates/
├── _schema.json                      # JSONSchema for legal.yaml etc. (full schema)
├── legal.yaml                        # NEW: legal template (the SP-1 first user)

app/domain_expert/tests/
├── test_template_loader.py
├── test_template_diff.py
├── test_bundle_apply.py
└── fixtures/
    ├── valid_legal.yaml
    ├── invalid_missing_id.yaml
    └── valid_minimal_specialty.yaml
```

---

## Task D1: Full SpecialtyTemplate dataclass + JSONSchema

**Goal:** Replace Phase 0 stub with the full schema covering all spec §3.1 fields.

- [ ] **Step 1: Replace `app/domain_expert/template.py`**

```python
"""SpecialtyTemplate — parsed YAML for a specialty (e.g. legal-expert).

Per spec §3.1.2 — the canonical schema. Lives in
app/data/specialty_templates/<id>.yaml.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptPackRef:
    id: str


@dataclass
class SkillRef:
    id: str


@dataclass
class MCPRef:
    name: str
    optional: bool = True


@dataclass
class CorpusSourceRef:
    source: str                          # e.g. "flk_npc" / "hf:disc-law-sft"
    estimated_size: str = ""             # "1.2GB" — for UI display


@dataclass
class TrainingConfig:
    base_model: str = "Qwen2.5-7B-Instruct"
    lora_r: int = 16
    raft_data_target: int = 5000
    refresh_cadence_days: int = 30


@dataclass
class EvalSuiteEntry:
    id: str                              # runner_id, must match Track C registry
    description: str = ""
    source: str = ""                     # dataset source if applicable
    metric: str = ""                     # accuracy / ratio / blind_eval_score
    runner: str = ""                     # fully-qualified runner class path


@dataclass
class ChunkerConfig:
    strategy: str                        # registered strategy_id
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class LevelRequirements:
    """Free-form dict for level-rule predicates.
    Example: {"bundle_complete_pct": ">=80", "lora_active": True}
    Evaluator parses comparison expressions; True/False is exact match.
    """
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_yaml(d: dict | None) -> "LevelRequirements":
        return LevelRequirements(raw=dict(d or {}))


@dataclass
class LevelRule:
    description: str = ""
    requirements: LevelRequirements = field(default_factory=LevelRequirements)


@dataclass
class SafetyConfig:
    pipl_redact: bool = True
    required_disclaimer: str = ""


@dataclass
class SpecialtyTemplate:
    """Full parsed specialty config. Validated against _schema.json."""
    id: str                              # e.g. "legal-expert"
    version: str                         # "1.0"
    name: str                            # 用户可见
    specialty: str                       # category, e.g. "legal"
    icon: str = ""
    description: str = ""

    required_prompt_packs: list[PromptPackRef] = field(default_factory=list)
    required_skills: list[SkillRef] = field(default_factory=list)
    recommended_mcps: list[MCPRef] = field(default_factory=list)
    recommended_corpus: list[CorpusSourceRef] = field(default_factory=list)

    training: TrainingConfig = field(default_factory=TrainingConfig)
    eval_suite: list[EvalSuiteEntry] = field(default_factory=list)
    chunker: ChunkerConfig | None = None
    chunker_secondary: list[dict] = field(default_factory=list)  # judgment etc.

    level_rules: dict[str, LevelRule] = field(default_factory=dict)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    def primary_chunker_strategy(self) -> str:
        return self.chunker.strategy if self.chunker else "paragraph"
```

- [ ] **Step 2: Write `app/data/specialty_templates/_schema.json`**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "SpecialtyTemplate",
  "type": "object",
  "required": ["id", "version", "name", "specialty"],
  "properties": {
    "id":          { "type": "string", "minLength": 1 },
    "version":     { "type": "string", "pattern": "^[0-9]+\\.[0-9]+(\\.[0-9]+)?$" },
    "name":        { "type": "string", "minLength": 1 },
    "specialty":   { "type": "string", "minLength": 1 },
    "icon":        { "type": "string" },
    "description": { "type": "string" },

    "required_prompt_packs": {
      "type": "array",
      "items": { "type": "object", "required": ["id"], "properties": { "id": { "type": "string" } } }
    },
    "required_skills": {
      "type": "array",
      "items": { "type": "object", "required": ["id"], "properties": { "id": { "type": "string" } } }
    },
    "recommended_mcps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name"],
        "properties": {
          "name":     { "type": "string" },
          "optional": { "type": "boolean" }
        }
      }
    },
    "recommended_corpus": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["source"],
        "properties": {
          "source":         { "type": "string" },
          "estimated_size": { "type": "string" }
        }
      }
    },

    "training": {
      "type": "object",
      "properties": {
        "base_model":            { "type": "string" },
        "lora_r":                { "type": "integer", "minimum": 1, "maximum": 256 },
        "raft_data_target":      { "type": "integer", "minimum": 100 },
        "refresh_cadence_days":  { "type": "integer", "minimum": 1 }
      }
    },

    "eval_suite": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id"],
        "properties": {
          "id":          { "type": "string" },
          "description": { "type": "string" },
          "source":      { "type": "string" },
          "metric":      { "type": "string" },
          "runner":      { "type": "string" }
        }
      }
    },

    "chunker": {
      "type": "object",
      "required": ["strategy"],
      "properties": {
        "strategy": { "type": "string" },
        "config":   { "type": "object" }
      }
    },
    "chunker_secondary": {
      "type": "array",
      "items": { "type": "object" }
    },

    "level_rules": {
      "type": "object",
      "patternProperties": {
        "^[a-z_]+$": {
          "type": "object",
          "properties": {
            "description":  { "type": "string" },
            "requirements": { "type": "object" }
          }
        }
      }
    },

    "safety": {
      "type": "object",
      "properties": {
        "pipl_redact":         { "type": "boolean" },
        "required_disclaimer": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add app/domain_expert/template.py app/data/specialty_templates/_schema.json
git commit -m "Track D task 1: full SpecialtyTemplate dataclass + JSONSchema"
```

---

## Task D2: Template loader (parse + validate)

**Goal:** Read YAML file, validate against schema, deserialize to `SpecialtyTemplate`. Cache parsed templates per process.

- [ ] **Step 1: Write `app/domain_expert/template_loader.py`**

```python
"""SpecialtyTemplate loader: YAML → validated dataclass.

Caches parsed templates per process (invalidate on file mtime change).
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any
from .template import (
    SpecialtyTemplate, PromptPackRef, SkillRef, MCPRef, CorpusSourceRef,
    TrainingConfig, EvalSuiteEntry, ChunkerConfig, LevelRule,
    LevelRequirements, SafetyConfig,
)
from . import _config

logger = logging.getLogger("tudouclaw.expert.template_loader")
_cache: dict[str, tuple[float, SpecialtyTemplate]] = {}


def _schema_path() -> str:
    return os.path.join(_config.template_dir(), "_schema.json")


def _load_schema() -> dict:
    with open(_schema_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def list_template_files() -> list[str]:
    """All *.yaml files (not _schema.json) under specialty_templates/."""
    d = _config.template_dir()
    if not os.path.isdir(d):
        return []
    files = []
    for fn in os.listdir(d):
        if fn.endswith(".yaml") and not fn.startswith("_"):
            files.append(os.path.join(d, fn))
    return sorted(files)


def load_yaml_file(path: str) -> dict:
    """Read YAML, return raw dict. Doesn't validate."""
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("PyYAML required: pip install pyyaml") from e
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate(raw: dict, schema: dict | None = None) -> None:
    """Validate against JSONSchema. Raises jsonschema.ValidationError on fail."""
    try:
        import jsonschema
    except ImportError as e:
        raise RuntimeError("jsonschema required: pip install jsonschema") from e
    if schema is None:
        schema = _load_schema()
    jsonschema.validate(raw, schema)


def parse(raw: dict) -> SpecialtyTemplate:
    """Convert raw dict → SpecialtyTemplate dataclass."""
    return SpecialtyTemplate(
        id=raw["id"],
        version=raw["version"],
        name=raw["name"],
        specialty=raw["specialty"],
        icon=raw.get("icon", ""),
        description=raw.get("description", ""),

        required_prompt_packs=[
            PromptPackRef(id=p["id"]) for p in raw.get("required_prompt_packs", [])
        ],
        required_skills=[
            SkillRef(id=s["id"]) for s in raw.get("required_skills", [])
        ],
        recommended_mcps=[
            MCPRef(name=m["name"], optional=m.get("optional", True))
            for m in raw.get("recommended_mcps", [])
        ],
        recommended_corpus=[
            CorpusSourceRef(
                source=c["source"],
                estimated_size=c.get("estimated_size", ""),
            )
            for c in raw.get("recommended_corpus", [])
        ],

        training=TrainingConfig(**(raw.get("training") or {})),
        eval_suite=[
            EvalSuiteEntry(**e) for e in raw.get("eval_suite", [])
        ],
        chunker=(
            ChunkerConfig(**raw["chunker"]) if raw.get("chunker") else None
        ),
        chunker_secondary=raw.get("chunker_secondary", []),

        level_rules={
            level_name: LevelRule(
                description=level.get("description", ""),
                requirements=LevelRequirements.from_yaml(level.get("requirements")),
            )
            for level_name, level in (raw.get("level_rules") or {}).items()
        },
        safety=SafetyConfig(**(raw.get("safety") or {})),
    )


def load(template_id: str) -> SpecialtyTemplate:
    """Find & load <template_id>.yaml. Validates + parses + caches."""
    candidate = os.path.join(_config.template_dir(), f"{template_id}.yaml")
    if not os.path.exists(candidate):
        raise FileNotFoundError(f"specialty template not found: {template_id}")
    mtime = os.path.getmtime(candidate)
    cached = _cache.get(template_id)
    if cached and cached[0] == mtime:
        return cached[1]
    raw = load_yaml_file(candidate)
    validate(raw)
    tpl = parse(raw)
    _cache[template_id] = (mtime, tpl)
    return tpl


def load_all() -> list[SpecialtyTemplate]:
    """Load every template under specialty_templates/. Skip invalid with warning."""
    out = []
    for f in list_template_files():
        try:
            template_id = os.path.splitext(os.path.basename(f))[0]
            out.append(load(template_id))
        except Exception as e:
            logger.warning("template %s failed: %s", f, e)
    return out


def invalidate_cache(template_id: str | None = None):
    if template_id:
        _cache.pop(template_id, None)
    else:
        _cache.clear()
```

- [ ] **Step 2: Write valid + invalid YAML fixtures**

`app/domain_expert/tests/fixtures/valid_legal.yaml`:

```yaml
id: legal-expert-test
version: "1.0"
name: 法律专家测试
specialty: legal
icon: ⚖️
description: 测试用最小法律配方

required_prompt_packs:
  - id: pack_a
  - id: pack_b
required_skills:
  - id: skill_x
recommended_mcps:
  - name: legal_mcp
    optional: true
recommended_corpus:
  - source: flk_npc
    estimated_size: 1.2GB

training:
  base_model: Qwen2.5-7B-Instruct
  lora_r: 16
  raft_data_target: 5000
  refresh_cadence_days: 30

eval_suite:
  - id: legalbench_zh
    metric: accuracy
  - id: citation_accuracy
    metric: ratio

chunker:
  strategy: hierarchical_legal
  config:
    primary_unit: article

level_rules:
  novice:
    description: 见习
    requirements:
      bundle_complete_pct: "<50"
  expert:
    description: 专家
    requirements:
      bundle_complete_pct: ">=80"
      lora_active: true
      benchmarks:
        legalbench_zh: ">=0.80"

safety:
  pipl_redact: true
  required_disclaimer: "AI 建议非法律意见。"
```

`app/domain_expert/tests/fixtures/invalid_missing_id.yaml`:

```yaml
# missing required field "id"
version: "1.0"
name: test
specialty: legal
```

`app/domain_expert/tests/fixtures/valid_minimal_specialty.yaml`:

```yaml
id: minimal-test
version: "1.0"
name: minimal
specialty: test
```

- [ ] **Step 3: Tests**

```python
# app/domain_expert/tests/test_template_loader.py
import os
import pytest

# Skip if pyyaml/jsonschema not installed
yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

from app.domain_expert import template_loader as tl
from app.domain_expert.template import SpecialtyTemplate

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def test_load_yaml_valid():
    raw = tl.load_yaml_file(os.path.join(FIX, "valid_legal.yaml"))
    assert raw["id"] == "legal-expert-test"


def test_validate_valid_passes():
    raw = tl.load_yaml_file(os.path.join(FIX, "valid_legal.yaml"))
    tl.validate(raw)  # no exception


def test_validate_invalid_raises():
    raw = tl.load_yaml_file(os.path.join(FIX, "invalid_missing_id.yaml"))
    with pytest.raises(jsonschema.ValidationError):
        tl.validate(raw)


def test_parse_full():
    raw = tl.load_yaml_file(os.path.join(FIX, "valid_legal.yaml"))
    tpl = tl.parse(raw)
    assert isinstance(tpl, SpecialtyTemplate)
    assert tpl.id == "legal-expert-test"
    assert tpl.version == "1.0"
    assert len(tpl.required_prompt_packs) == 2
    assert tpl.chunker.strategy == "hierarchical_legal"
    assert tpl.training.base_model == "Qwen2.5-7B-Instruct"
    assert tpl.eval_suite[0].id == "legalbench_zh"
    assert tpl.level_rules["expert"].description == "专家"


def test_parse_minimal():
    raw = tl.load_yaml_file(os.path.join(FIX, "valid_minimal_specialty.yaml"))
    tl.validate(raw)
    tpl = tl.parse(raw)
    assert tpl.id == "minimal-test"
    assert tpl.required_prompt_packs == []
    assert tpl.chunker is None
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/pip install pyyaml jsonschema
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_template_loader.py -v
# expect: 5 passed
git add app/domain_expert/template_loader.py \
        app/domain_expert/tests/test_template_loader.py \
        app/domain_expert/tests/fixtures/{valid_legal,invalid_missing_id,valid_minimal_specialty}.yaml
git commit -m "Track D task 2: template loader (YAML parse + JSONSchema validate)"
```

---

## Task D3: Template version diff

**Goal:** Compare two SpecialtyTemplate objects, report what changed (added packs / removed skills / config bumps). Used when user has v1.0 applied and v1.1 ships.

- [ ] **Step 1: Write `app/domain_expert/template_diff.py`**

```python
"""Diff two SpecialtyTemplate versions. Produces a human-readable diff
report users can review before accepting a template upgrade.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .template import SpecialtyTemplate


@dataclass
class TemplateDiff:
    old_version: str
    new_version: str
    same_id: bool

    packs_added: list[str] = field(default_factory=list)
    packs_removed: list[str] = field(default_factory=list)
    skills_added: list[str] = field(default_factory=list)
    skills_removed: list[str] = field(default_factory=list)
    mcps_added: list[str] = field(default_factory=list)
    mcps_removed: list[str] = field(default_factory=list)
    corpus_added: list[str] = field(default_factory=list)
    corpus_removed: list[str] = field(default_factory=list)
    training_changed: list[tuple[str, object, object]] = field(default_factory=list)
    eval_suite_changed: bool = False
    chunker_changed: bool = False
    level_rules_changed: bool = False
    safety_changed: bool = False

    def is_breaking(self) -> bool:
        """Returns True if upgrade requires user attention (removed items
        or breaking config changes)."""
        return bool(
            self.packs_removed or self.skills_removed
            or self.training_changed or self.chunker_changed
        )

    def summary(self) -> str:
        """Compact 1-paragraph human summary."""
        parts = []
        if self.packs_added:    parts.append(f"+{len(self.packs_added)} packs")
        if self.packs_removed:  parts.append(f"-{len(self.packs_removed)} packs")
        if self.skills_added:   parts.append(f"+{len(self.skills_added)} skills")
        if self.skills_removed: parts.append(f"-{len(self.skills_removed)} skills")
        if self.training_changed: parts.append("training params changed")
        if self.chunker_changed:  parts.append("chunker changed")
        if not parts:
            return "no functional changes"
        return ", ".join(parts)


def diff(old: SpecialtyTemplate, new: SpecialtyTemplate) -> TemplateDiff:
    """Compute diff old → new."""
    d = TemplateDiff(
        old_version=old.version,
        new_version=new.version,
        same_id=(old.id == new.id),
    )

    def ids(items, attr):
        return {getattr(x, attr) for x in items}

    old_packs = ids(old.required_prompt_packs, "id")
    new_packs = ids(new.required_prompt_packs, "id")
    d.packs_added = sorted(new_packs - old_packs)
    d.packs_removed = sorted(old_packs - new_packs)

    old_skills = ids(old.required_skills, "id")
    new_skills = ids(new.required_skills, "id")
    d.skills_added = sorted(new_skills - old_skills)
    d.skills_removed = sorted(old_skills - new_skills)

    old_mcps = ids(old.recommended_mcps, "name")
    new_mcps = ids(new.recommended_mcps, "name")
    d.mcps_added = sorted(new_mcps - old_mcps)
    d.mcps_removed = sorted(old_mcps - new_mcps)

    old_corpus = ids(old.recommended_corpus, "source")
    new_corpus = ids(new.recommended_corpus, "source")
    d.corpus_added = sorted(new_corpus - old_corpus)
    d.corpus_removed = sorted(old_corpus - new_corpus)

    # training params
    for f in ("base_model", "lora_r", "raft_data_target", "refresh_cadence_days"):
        ov = getattr(old.training, f)
        nv = getattr(new.training, f)
        if ov != nv:
            d.training_changed.append((f, ov, nv))

    d.eval_suite_changed = (
        [(e.id, e.metric) for e in old.eval_suite]
        != [(e.id, e.metric) for e in new.eval_suite]
    )
    d.chunker_changed = (
        (old.chunker is None) != (new.chunker is None)
        or (old.chunker and new.chunker
            and (old.chunker.strategy != new.chunker.strategy
                 or old.chunker.config != new.chunker.config))
    )
    d.level_rules_changed = (
        list(old.level_rules.keys()) != list(new.level_rules.keys())
        or any(old.level_rules[k].requirements.raw != new.level_rules[k].requirements.raw
               for k in old.level_rules if k in new.level_rules)
    )
    d.safety_changed = (
        old.safety.pipl_redact != new.safety.pipl_redact
        or old.safety.required_disclaimer != new.safety.required_disclaimer
    )
    return d
```

- [ ] **Step 2: Test**

```python
# app/domain_expert/tests/test_template_diff.py
from app.domain_expert.template import (
    SpecialtyTemplate, PromptPackRef, SkillRef, TrainingConfig,
    ChunkerConfig,
)
from app.domain_expert.template_diff import diff


def _mk(version="1.0", packs=None, skills=None, training=None, chunker=None):
    return SpecialtyTemplate(
        id="test", version=version, name="t", specialty="test",
        required_prompt_packs=[PromptPackRef(id=p) for p in (packs or [])],
        required_skills=[SkillRef(id=s) for s in (skills or [])],
        training=training or TrainingConfig(),
        chunker=chunker,
    )


def test_diff_packs_added():
    old = _mk(packs=["a", "b"])
    new = _mk(packs=["a", "b", "c"])
    d = diff(old, new)
    assert d.packs_added == ["c"]
    assert d.packs_removed == []
    assert not d.is_breaking()


def test_diff_packs_removed_is_breaking():
    old = _mk(packs=["a", "b", "c"])
    new = _mk(packs=["a"])
    d = diff(old, new)
    assert sorted(d.packs_removed) == ["b", "c"]
    assert d.is_breaking()


def test_diff_training_changed_is_breaking():
    old = _mk(training=TrainingConfig(lora_r=16))
    new = _mk(training=TrainingConfig(lora_r=32))
    d = diff(old, new)
    assert d.training_changed[0] == ("lora_r", 16, 32)
    assert d.is_breaking()


def test_diff_chunker_change():
    old = _mk(chunker=ChunkerConfig(strategy="paragraph"))
    new = _mk(chunker=ChunkerConfig(strategy="hierarchical_legal"))
    d = diff(old, new)
    assert d.chunker_changed
    assert d.is_breaking()


def test_diff_no_change():
    old = _mk(packs=["a"])
    new = _mk(packs=["a"])
    d = diff(old, new)
    assert d.summary() == "no functional changes"
    assert not d.is_breaking()
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_template_diff.py -v
# expect: 5 passed
git add app/domain_expert/template_diff.py app/domain_expert/tests/test_template_diff.py
git commit -m "Track D task 3: TemplateDiff with breaking-change detection"
```

---

## Task D4: Bundle apply engine

**Goal:** Given (template, agent_id), apply the capability bundle: batch grant skills, batch bind prompt packs, register MCPs. Idempotent (re-running on already-applied = no-op). Returns detailed per-item status.

- [ ] **Step 1: Write `app/domain_expert/bundle_apply.py`**

```python
"""Bundle apply engine — given a SpecialtyTemplate and an agent, apply the
capability bundle (packs + skills + mcp recommendations) using existing
Tudou primitives.

This is the V2 vertical's "Stage 1" core logic. Track D ships it as a
pure-Python module; V2 wraps it with progress UI.

NB: this module CALLS into existing Tudou subsystems
    (PromptPackRegistry, skill registry, agent dataclass methods).
On those failures we log + skip, don't abort. Reason: partial application
is acceptable; user retries.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from .template import SpecialtyTemplate

logger = logging.getLogger("tudouclaw.expert.bundle_apply")


@dataclass
class BundleApplyResult:
    template_id: str
    agent_id: str
    packs_bound: list[str] = field(default_factory=list)
    packs_skipped: list[tuple[str, str]] = field(default_factory=list)  # (pack, reason)
    skills_granted: list[str] = field(default_factory=list)
    skills_skipped: list[tuple[str, str]] = field(default_factory=list)
    mcps_recommended: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def is_complete(self) -> bool:
        return not self.packs_skipped and not self.skills_skipped and not self.errors


def apply_bundle(
    template: SpecialtyTemplate,
    agent,                              # app.agent.Agent — duck-typed
    *,
    save_callback=None,                 # callable to persist agent (typically hub._save_agents)
    skill_grant_callback=None,          # callable(skill_id, agent_id) -> bool
) -> BundleApplyResult:
    """Idempotently apply the capability bundle to `agent`.

    Args:
        template: parsed SpecialtyTemplate
        agent: app.agent.Agent instance with .id, .bound_prompt_packs,
               .granted_skills, .mcp_servers attributes
        save_callback: invoked once after mutations; if None, caller persists
        skill_grant_callback: invoked per skill; if None, mutates agent.granted_skills directly
    """
    res = BundleApplyResult(
        template_id=template.id, agent_id=getattr(agent, "id", "unknown"))

    # ── Prompt packs ──
    bound_set = set(getattr(agent, "bound_prompt_packs", []))
    for ref in template.required_prompt_packs:
        if ref.id in bound_set:
            res.packs_skipped.append((ref.id, "already bound"))
            continue
        try:
            # Verify the pack exists in the registry
            from app.skills.prompt_enhancer import get_prompt_pack_registry
            reg = get_prompt_pack_registry()
            if reg.store.get(ref.id) is None:
                res.packs_skipped.append((ref.id, "not in registry — install from market first"))
                continue
            agent.bound_prompt_packs.append(ref.id)
            bound_set.add(ref.id)
            res.packs_bound.append(ref.id)
        except Exception as e:
            res.errors.append(f"pack {ref.id}: {e}")
            logger.warning("pack bind failed for %s: %s", ref.id, e)

    # ── Skills ──
    granted_set = set(getattr(agent, "granted_skills", []))
    for ref in template.required_skills:
        if ref.id in granted_set:
            res.skills_skipped.append((ref.id, "already granted"))
            continue
        try:
            if skill_grant_callback:
                ok = skill_grant_callback(ref.id, agent.id)
                if not ok:
                    res.skills_skipped.append((ref.id, "grant callback returned false"))
                    continue
            else:
                # Direct mutation fallback
                agent.granted_skills.append(ref.id)
                granted_set.add(ref.id)
            res.skills_granted.append(ref.id)
        except Exception as e:
            res.errors.append(f"skill {ref.id}: {e}")
            logger.warning("skill grant failed for %s: %s", ref.id, e)

    # ── MCPs (recommend only — actual install is user-driven) ──
    for mcp_ref in template.recommended_mcps:
        res.mcps_recommended.append(mcp_ref.name)

    # ── Persist ──
    if save_callback:
        try:
            save_callback()
        except Exception as e:
            res.errors.append(f"save: {e}")

    logger.info(
        "bundle apply: agent=%s template=%s packs+%d skills+%d mcps_rec=%d errors=%d",
        res.agent_id, res.template_id,
        len(res.packs_bound), len(res.skills_granted),
        len(res.mcps_recommended), len(res.errors),
    )
    return res
```

- [ ] **Step 2: Test (with mocked agent + registry)**

```python
# app/domain_expert/tests/test_bundle_apply.py
from unittest.mock import MagicMock, patch
import pytest
from app.domain_expert.template import (
    SpecialtyTemplate, PromptPackRef, SkillRef, MCPRef,
)
from app.domain_expert.bundle_apply import apply_bundle


def _mk_template():
    return SpecialtyTemplate(
        id="test-tpl", version="1.0", name="t", specialty="test",
        required_prompt_packs=[
            PromptPackRef(id="pack_a"),
            PromptPackRef(id="pack_b"),
        ],
        required_skills=[SkillRef(id="skill_x")],
        recommended_mcps=[MCPRef(name="mcp_y", optional=True)],
    )


def _mk_agent():
    a = MagicMock()
    a.id = "ag1"
    a.bound_prompt_packs = []
    a.granted_skills = []
    a.mcp_servers = []
    return a


@patch("app.skills.prompt_enhancer.get_prompt_pack_registry")
def test_bundle_apply_grants_all_when_registry_has_packs(mock_reg):
    mock_store = MagicMock()
    mock_store.get.return_value = MagicMock()  # truthy = pack exists
    mock_reg.return_value.store = mock_store

    tpl = _mk_template()
    agent = _mk_agent()
    saved_calls = []
    res = apply_bundle(tpl, agent, save_callback=lambda: saved_calls.append(1))

    assert res.packs_bound == ["pack_a", "pack_b"]
    assert res.skills_granted == ["skill_x"]
    assert res.mcps_recommended == ["mcp_y"]
    assert agent.bound_prompt_packs == ["pack_a", "pack_b"]
    assert agent.granted_skills == ["skill_x"]
    assert len(saved_calls) == 1
    assert res.is_complete()


@patch("app.skills.prompt_enhancer.get_prompt_pack_registry")
def test_bundle_apply_idempotent(mock_reg):
    mock_store = MagicMock()
    mock_store.get.return_value = MagicMock()
    mock_reg.return_value.store = mock_store

    tpl = _mk_template()
    agent = _mk_agent()
    agent.bound_prompt_packs = ["pack_a"]   # already bound

    res = apply_bundle(tpl, agent)

    assert res.packs_bound == ["pack_b"]    # only the new one
    assert any(p[0] == "pack_a" and "already bound" in p[1] for p in res.packs_skipped)


@patch("app.skills.prompt_enhancer.get_prompt_pack_registry")
def test_bundle_apply_skips_missing_packs(mock_reg):
    mock_store = MagicMock()
    mock_store.get.return_value = None   # registry doesn't have it
    mock_reg.return_value.store = mock_store

    tpl = _mk_template()
    agent = _mk_agent()
    res = apply_bundle(tpl, agent)

    assert res.packs_bound == []
    assert all("not in registry" in reason for _, reason in res.packs_skipped)
    assert not res.is_complete()


def test_skill_grant_callback_used():
    """When skill_grant_callback is provided, it controls whether grant succeeds."""
    with patch("app.skills.prompt_enhancer.get_prompt_pack_registry") as mock_reg:
        mock_store = MagicMock()
        mock_store.get.return_value = MagicMock()
        mock_reg.return_value.store = mock_store

        tpl = _mk_template()
        agent = _mk_agent()
        granted_via_cb = []

        def cb(skill_id, agent_id):
            granted_via_cb.append((skill_id, agent_id))
            return True

        res = apply_bundle(tpl, agent, skill_grant_callback=cb)
        assert res.skills_granted == ["skill_x"]
        assert granted_via_cb == [("skill_x", "ag1")]


def test_skill_grant_callback_can_fail():
    with patch("app.skills.prompt_enhancer.get_prompt_pack_registry") as mock_reg:
        mock_store = MagicMock()
        mock_store.get.return_value = MagicMock()
        mock_reg.return_value.store = mock_store

        tpl = _mk_template()
        agent = _mk_agent()

        def cb(skill_id, agent_id):
            return False

        res = apply_bundle(tpl, agent, skill_grant_callback=cb)
        assert res.skills_granted == []
        assert any("returned false" in reason for _, reason in res.skills_skipped)
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_bundle_apply.py -v
# expect: 5 passed
git add app/domain_expert/bundle_apply.py app/domain_expert/tests/test_bundle_apply.py
git commit -m "Track D task 4: bundle_apply engine + 5 tests (idempotent, mock-friendly)"
```

---

## Task D5: First real legal.yaml + integration smoke

**Goal:** Ship the actual `legal.yaml` (full schema, references real packs/skills/runners that V2 will assume present). Integration test loads it end-to-end.

- [ ] **Step 1: Write `app/data/specialty_templates/legal.yaml`**

(Use the spec §3.1.2 example as the canonical legal config — copy values verbatim from the spec, with package/skill IDs that already exist in your Tudou or note them as `# pending V2: install from market first`.)

```yaml
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
icon: ⚖️
description: 中国法系专家,侧重合同 / 劳动 / 民事

# ── Capability Bundle (复用既有 pack / skill 体系) ──
required_prompt_packs:
  # 来自社区 catalog (agency-agents-zh) — 已通过 prompt pack 市场可装
  - id: agency_legal_lawyer
  - id: agency_legal_legal_counsel
  - id: agency_legal_contract_lawyer
  - id: agency_legal_litigation_specialist
  # 来自 Anthropic catalog
  - id: akwp_legal_brief
  - id: akwp_legal_review-contract
  - id: akwp_legal_compliance-check
  - id: akwp_legal_triage-nda
  - id: akwp_legal_legal-risk-assessment
  - id: akwp_legal_legal-response
  - id: akwp_legal_meeting-briefing
  - id: akwp_legal_vendor-check

required_skills:
  # 这些 skills 在 V2 vertical 阶段需要先装好,否则 bundle apply 会 skip
  # 留作未来扩展项,SP-1 阶段 bundle_apply 会报 not_in_registry 是 OK 的
  []

recommended_mcps:
  - name: legal_database_mcp
    optional: true

# ── Knowledge (RAG 入库源) ──
recommended_corpus:
  - source: flk_npc
    estimated_size: 1.2GB
  - source: hf:disc-law-sft
    estimated_size: 800MB
  - source: hf:cail2018-2019
    estimated_size: 1.5GB

# ── Training params (SP-2 用) ──
training:
  base_model: Qwen2.5-7B-Instruct
  lora_r: 16
  raft_data_target: 5000
  refresh_cadence_days: 30

# ── 法律专属 eval suite (Track C 实装的 runner) ──
eval_suite:
  - id: legalbench_zh
    description: 中国法律理解综合评测
    metric: accuracy
    runner: app.domain_expert.training.eval_runners.legalbench_zh.LegalBenchZhRunner
  - id: citation_accuracy
    description: 引用真实性
    metric: ratio
    runner: app.domain_expert.training.eval_runners.citation_validator.CitationValidator

# ── Chunker (Track A 实装的 strategy) ──
chunker:
  strategy: hierarchical_legal
  config:
    primary_unit: article
    min_chunk_chars: 80
    max_chunk_chars: 800

chunker_secondary:
  - strategy: legal_judgment
    applies_to:
      file_pattern: "*.judgment.txt"
      source_glob: "cail*"
    config:
      min_chunk_chars: 800
      max_chunk_chars: 4000

# ── 段位规则(法律专属硬门槛) ──
level_rules:
  novice:
    description: 配方应用中
    requirements:
      bundle_complete_pct: "<50"

  journeyman:
    description: 知识层就位 + 工具基本齐
    requirements:
      bundle_complete_pct: ">=50"
      corpus_indexed: true

  expert:
    description: LoRA 落地, 有引用能力
    requirements:
      bundle_complete_pct: ">=80"
      lora_active: true
      benchmarks:
        legalbench_zh: ">=0.80"
        citation_accuracy: "==1.00"

  master:
    description: 持续迭代, 本地处理为主
    requirements:
      bundle_complete_pct: "100"
      lora_refresh_count: ">=3"
      local_handle_rate_clean: ">=0.7"
      benchmarks:
        legalbench_zh: ">=0.85"
        citation_accuracy: "==1.00"

safety:
  pipl_redact: true
  required_disclaimer: |
    AI 提供的法律分析仅供参考, 非正式法律意见;
    重大决策请咨询执业律师。
```

- [ ] **Step 2: Integration test — load real legal.yaml end-to-end**

```python
# app/domain_expert/tests/test_legal_yaml_integration.py
import os
import pytest
yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

from app.domain_expert import template_loader as tl


def test_legal_yaml_loads_and_validates():
    """The shipped legal.yaml passes schema validation and parses fully."""
    tpl = tl.load("legal")
    assert tpl.id == "legal-expert"
    assert tpl.specialty == "legal"
    assert tpl.icon == "⚖️"
    assert len(tpl.required_prompt_packs) >= 10
    assert tpl.chunker.strategy == "hierarchical_legal"
    assert tpl.training.base_model == "Qwen2.5-7B-Instruct"
    # Eval suite references runners Track C registers
    eval_ids = [e.id for e in tpl.eval_suite]
    assert "legalbench_zh" in eval_ids
    assert "citation_accuracy" in eval_ids
    # 4 段位都定义
    assert set(tpl.level_rules.keys()) == {"novice", "journeyman", "expert", "master"}
    # Expert level has the legal-specific benchmark gates
    expert_req = tpl.level_rules["expert"].requirements.raw
    assert "benchmarks" in expert_req
    assert expert_req["benchmarks"]["legalbench_zh"] == ">=0.80"


def test_load_all_does_not_crash():
    """Even if other yaml files exist, load_all returns at least legal."""
    all_tpls = tl.load_all()
    ids = [t.id for t in all_tpls]
    assert "legal-expert" in ids
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_legal_yaml_integration.py -v
# expect: 2 passed
git add app/data/specialty_templates/legal.yaml \
        app/domain_expert/tests/test_legal_yaml_integration.py
git commit -m "Track D task 5: ship legal.yaml + end-to-end load integration test"
```

---

## Self-Review

- ☑ All 5 tasks covered. ~22 unit tests across the track.
- ☑ Pure backend, no HTTP / UI / DB schema touched
- ☑ Bundle_apply uses dependency injection (callbacks) — V2 wires real Tudou primitives without Track D depending on them
- ☑ JSONSchema enforces field types — invalid YAML caught at load time
- ☑ Templates cached by mtime — file edit picks up next call
- ☑ Diff identifies breaking vs non-breaking changes — V2 UI uses for upgrade prompts
- ☑ TUDOU_EXPERT_DISABLED still works (this module just doesn't get imported)

## Handoff to V2 Vertical

After Track D merges, V2 vertical (Bundle apply API + UI) wires:

- `app.domain_expert.template_loader.load_all()` → list endpoint /api/portal/specialty-templates
- `app.domain_expert.template_loader.load(template_id)` → detail endpoint
- `app.domain_expert.bundle_apply.apply_bundle(template, agent, ...)` → POST /api/portal/agent/{id}/expert/initialize
- `app.domain_expert.template_diff.diff(old, new)` → upgrade flow when a template version bumps

Track D commits no API endpoint changes — V2 vertical adds those, replacing Phase 0's 501 stubs.
