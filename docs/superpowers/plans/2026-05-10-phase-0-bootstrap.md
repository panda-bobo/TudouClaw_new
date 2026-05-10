# Phase 0: Bootstrap Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec reference:** [§2-3 Architecture](../specs/2026-05-10-agent-specialty-cultivation-design.md)

**Goal:** Lay the foundation that 4 parallel tracks (A/B/C/D) all depend on. Single agent, ~1 day. After this lands and merges to main, the 4 tracks can fork off independently.

**Architecture:** Module skeleton + minimal dataclasses + empty API stubs + feature flag. NO functional logic — just empty containers + 5 optional fields on Agent.

**Tech Stack:** Python 3.11 (existing tudou-env), FastAPI, dataclasses, jsonschema. No new dependencies.

---

## File Structure

### New Files

```
app/domain_expert/
├── __init__.py                       # Module marker + version
├── README.md                         # Module overview (for human readers)
├── _config.py                        # TUDOU_EXPERT_DISABLED flag, paths
├── profile.py                        # ExpertProfile dataclass (minimal)
├── template.py                       # SpecialtyTemplate dataclass (minimal)
├── manager.py                        # ExpertManager singleton (stub)
├── corpus/__init__.py                # Empty (Track A fills)
├── retrieval/__init__.py             # Empty (Track A fills)
├── training/__init__.py              # Empty (Track C fills)
├── inference/__init__.py             # Empty
└── api/
    ├── __init__.py
    └── routers.py                    # Empty router with 501 stubs

app/data/specialty_templates/
├── _schema.json                      # JSONSchema for YAML validation (minimal — fleshed by Track D)
└── (legal.yaml stays for SP-1 V2; Phase 0 leaves dir empty)

requirements-expert.txt               # Optional deps (mlx-lm, sentence-transformers, sqlite-vss, pyyaml, jsonschema)
```

### Files Modified (very small)

```
app/agent.py                          # +5 optional fields on Agent dataclass
app/api/main.py                       # +1 line: register expert router (no-op when DISABLED)
```

### Files Untouched

Per [INDEX.md](2026-05-10-INDEX.md) Rule 2 — list goes there.

---

## Task 1: Create module skeleton + README

**Goal:** Empty `app/domain_expert/` tree with all sub-packages exists. Importable. Doesn't break existing tests.

- [ ] **Step 1: Create directories**

```bash
cd /Users/pangwanchun/AIProjects/TudouClaw_new
mkdir -p app/domain_expert/{corpus,retrieval,training,inference,api}
mkdir -p app/data/specialty_templates
touch app/domain_expert/__init__.py
touch app/domain_expert/{corpus,retrieval,training,inference,api}/__init__.py
```

- [ ] **Step 2: Write `app/domain_expert/__init__.py`**

```python
"""Agent Specialty Cultivation System.

See docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md
for the design doc this module implements.

Sub-packages:
    corpus      — Track A: ingestion + chunking + vector store
    retrieval   — Track A: embedding + reranking + hybrid search
    training    — Track C: trace cleaner + RAFT synth + LoRA + eval
    inference   — Future: routing + safety + pipeline
    api         — REST endpoints under /api/portal/agent/{id}/expert/*
"""
__version__ = "0.0.1-phase-0"
```

- [ ] **Step 3: Write `app/domain_expert/README.md`**

```markdown
# Agent Specialty Cultivation Module

Status: **Phase 0 (skeleton)** — see [docs/superpowers/plans/](../../docs/superpowers/plans/)

## What this is

A self-contained module that lets any Tudou agent be cultivated into a domain
expert (legal / medical / financial / etc.). Agents stay one entity; the module
adds: corpus + RAG + (later) LoRA + routing.

## Hard isolation guarantees

- `TUDOU_EXPERT_DISABLED=1` → module skips init; existing functionality untouched
- New deps optional in `requirements-expert.txt`; main `requirements.txt` unchanged
- All persistent data under `~/.tudou_claw/expert/<agent_id>/`
- Agent dataclass gets 5 OPTIONAL fields (default empty); old agents.json loads cleanly

## Tracks (parallel development)

- Track A: corpus + retrieval (this README sub-packages)
- Track B: SP-0 UI integration (in app/server/static/js/)
- Track C: training (this README sub-package)
- Track D: specialty templates + bundle apply

## Where to look

- API entry: `app/domain_expert/api/routers.py`
- Specialty templates: `app/data/specialty_templates/*.yaml`
- Persistent data: `~/.tudou_claw/expert/<agent_id>/`
- Spec: `docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md`
```

- [ ] **Step 4: Verify import works**

```bash
~/tudou-env/bin/python3 -c "import app.domain_expert; print(app.domain_expert.__version__)"
# expect: 0.0.1-phase-0
```

- [ ] **Step 5: Verify existing tests still pass (smoke)**

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
# wait for ready
curl -s http://localhost:9090/api/portal/state -H "Cookie: ..." | head
# verify still 200
```

- [ ] **Step 6: Commit**

```bash
git add app/domain_expert/
git commit -m "Phase 0 task 1: domain_expert module skeleton + README"
```

---

## Task 2: Add 5 optional fields to Agent dataclass

**Goal:** Agent gains expert_specialty / expert_template_version / expert_level / expert_lora_version / expert_initialized_at. Default values keep existing agents.json fully backward-compatible.

- [ ] **Step 1: Read existing dataclass field block**

```bash
grep -n "bound_prompt_packs:\|unbound_role_packs:\|tts_voice:" app/agent.py | head -5
```

Find the cluster of optional fields near `bound_prompt_packs`.

- [ ] **Step 2: Add the 5 fields**

Edit `app/agent.py` near the bound_prompt_packs / unbound_role_packs block:

```python
    # ── 🆕 Phase 0: Expert specialty (optional, all default empty) ──
    # When all empty, agent behaves identically to current. When
    # expert_specialty != "", reply pipeline routes through
    # app.domain_expert.inference.pipeline (added in V4 vertical).
    # See docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md §3.2
    expert_specialty: str = ""             # "" / "legal" / "medical" / "finance" / ...
    expert_template_version: str = ""      # locked-in template version when initialized
    expert_level: str = "novice"           # novice / journeyman / expert / master
    expert_lora_version: str = ""          # active LoRA version, e.g. "v3"
    expert_initialized_at: float = 0.0     # epoch seconds when专家化 started
```

- [ ] **Step 3: Persist in to_dict / from_dict / to_persist_dict / from_persist_dict**

```bash
grep -n '"bound_prompt_packs":\|d.get("bound_prompt_packs"' app/agent.py
```

For each location, add the 5 expert_* fields with same pattern as bound_prompt_packs / unbound_role_packs.

- [ ] **Step 4: Write a regression test**

Create `app/domain_expert/tests/__init__.py` and `app/domain_expert/tests/test_agent_field_compat.py`:

```python
"""Phase 0 regression: old agents.json must load cleanly with new fields default."""
import json
import pytest
from app.agent import Agent

def test_old_agent_dict_loads_cleanly():
    """Simulate an agents.json record from before Phase 0."""
    old_dict = {
        "id": "test123",
        "name": "test",
        "role": "default",
        "bound_prompt_packs": ["pack1"],
        # NO expert_* fields — pre-Phase-0 record
    }
    agent = Agent.from_persist_dict(old_dict)
    # All 5 expert fields should default cleanly
    assert agent.expert_specialty == ""
    assert agent.expert_template_version == ""
    assert agent.expert_level == "novice"
    assert agent.expert_lora_version == ""
    assert agent.expert_initialized_at == 0.0
    # Existing fields preserved
    assert agent.id == "test123"
    assert "pack1" in agent.bound_prompt_packs

def test_round_trip_with_expert_fields_set():
    """An agent with expert fields set serializes and round-trips."""
    a = Agent(id="ex1", name="expert-test")
    a.expert_specialty = "legal"
    a.expert_level = "journeyman"
    a.expert_initialized_at = 1234567890.0
    d = a.to_persist_dict()
    a2 = Agent.from_persist_dict(d)
    assert a2.expert_specialty == "legal"
    assert a2.expert_level == "journeyman"
    assert a2.expert_initialized_at == 1234567890.0
```

- [ ] **Step 5: Run test**

```bash
cd /Users/pangwanchun/AIProjects/TudouClaw_new
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_agent_field_compat.py -v
# expect: 2 passed
```

- [ ] **Step 6: Restart server, verify agents load**

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
# wait
JWT=$(curl -s -X POST http://localhost:9090/api/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -H "Authorization: Bearer $JWT" http://localhost:9090/api/portal/state | python3 -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('agents', [])[:3]:
    print(f'{a[\"id\"]:15} expert_specialty={a.get(\"expert_specialty\",\"\")!r}')
"
# expect: existing agents listed, expert_specialty = ''
```

- [ ] **Step 7: Commit**

```bash
git add app/agent.py app/domain_expert/tests/
git commit -m "Phase 0 task 2: Agent dataclass +5 optional expert_* fields with backward-compat test"
```

---

## Task 3: ExpertProfile + SpecialtyTemplate minimal dataclasses

**Goal:** Two dataclasses that represent (1) the per-agent expert state on disk and (2) a parsed YAML template. Track D will flesh them out; Phase 0 puts the minimal struct in place so `app/domain_expert/api/routers.py` can type-hint them.

- [ ] **Step 1: Write `app/domain_expert/profile.py`**

```python
"""ExpertProfile — per-agent persistent expert configuration.

Lives at ~/.tudou_claw/expert/<agent_id>/config.json.

Phase 0: minimal fields. Track D adds full schema.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class ExpertProfile:
    agent_id: str
    specialty: str                         # "legal" / "medical" / ...
    template_id: str                       # "legal-expert"
    template_version: str                  # "1.0"
    level: str = "novice"
    active_lora_version: str = ""
    last_eval_score: float = 0.0
    initialized_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Track D will add: corpus_sources, cite_required, confidence_threshold, ...

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ExpertProfile":
        return ExpertProfile(**{
            k: v for k, v in d.items()
            if k in ExpertProfile.__dataclass_fields__
        })

    @staticmethod
    def load(agent_id: str) -> "ExpertProfile | None":
        from ._config import expert_dir_for
        p = os.path.join(expert_dir_for(agent_id), "config.json")
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return ExpertProfile.from_dict(json.load(f))

    def save(self) -> None:
        from ._config import expert_dir_for
        d = expert_dir_for(self.agent_id)
        os.makedirs(d, exist_ok=True)
        self.updated_at = time.time()
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
```

- [ ] **Step 2: Write `app/domain_expert/template.py`**

```python
"""SpecialtyTemplate — parsed YAML config for a specialty (e.g. legal-expert).

Phase 0: minimal schema. Track D adds full validation + diff.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SpecialtyTemplate:
    id: str                                # "legal-expert"
    version: str                           # "1.0"
    name: str                              # "法律专家"
    specialty: str                         # "legal"
    icon: str = ""
    description: str = ""
    # Track D will add: required_prompt_packs, required_skills,
    # recommended_mcps, recommended_corpus, training, eval_suite,
    # chunker, level_rules, safety
```

- [ ] **Step 3: Write `app/domain_expert/_config.py`**

```python
"""Module-wide config + path helpers + feature flag."""
from __future__ import annotations
import os
from pathlib import Path

DISABLED_ENV_VAR = "TUDOU_EXPERT_DISABLED"


def is_disabled() -> bool:
    """Env-var feature flag — when '1', the entire module is a no-op."""
    return os.environ.get(DISABLED_ENV_VAR, "0") == "1"


def expert_root() -> str:
    """Root for all expert persistent data."""
    home = os.path.expanduser("~")
    return os.path.join(home, ".tudou_claw", "expert")


def expert_dir_for(agent_id: str) -> str:
    """Per-agent expert data dir. Caller is responsible for makedirs."""
    return os.path.join(expert_root(), agent_id)


def template_dir() -> str:
    """Where shipped specialty templates live."""
    here = Path(__file__).resolve().parent.parent  # app/
    return str(here / "data" / "specialty_templates")
```

- [ ] **Step 4: Write `app/domain_expert/manager.py`** (stub for now, Track D fleshes)

```python
"""ExpertManager — singleton entry point. Phase 0 stub."""
from __future__ import annotations
from . import _config


class ExpertManager:
    def __init__(self):
        pass

    def is_available(self) -> bool:
        return not _config.is_disabled()


_singleton: ExpertManager | None = None


def get_manager() -> ExpertManager:
    global _singleton
    if _singleton is None:
        _singleton = ExpertManager()
    return _singleton
```

- [ ] **Step 5: Test**

`app/domain_expert/tests/test_phase0_dataclasses.py`:

```python
import os
import tempfile
from app.domain_expert.profile import ExpertProfile
from app.domain_expert.template import SpecialtyTemplate
from app.domain_expert import _config

def test_profile_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = ExpertProfile(
        agent_id="ag1", specialty="legal",
        template_id="legal-expert", template_version="1.0",
    )
    p.save()
    loaded = ExpertProfile.load("ag1")
    assert loaded is not None
    assert loaded.specialty == "legal"
    assert loaded.template_id == "legal-expert"

def test_template_minimal():
    t = SpecialtyTemplate(
        id="legal-expert", version="1.0",
        name="法律专家", specialty="legal",
    )
    assert t.specialty == "legal"

def test_config_disabled_default():
    assert _config.is_disabled() is False  # default unset

def test_config_disabled_when_set(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    assert _config.is_disabled() is True
```

- [ ] **Step 6: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_phase0_dataclasses.py -v
# expect: 4 passed
git add app/domain_expert/{profile,template,manager,_config}.py app/domain_expert/tests/test_phase0_dataclasses.py
git commit -m "Phase 0 task 3: ExpertProfile + SpecialtyTemplate minimal dataclasses + config helpers"
```

---

## Task 4: Empty API router with 501 stubs

**Goal:** All 12 expert endpoints listed in spec §5.1 exist and return `501 Not Implemented`. Tracks A/D and verticals later replace each stub with real impl. This guarantees the API surface is locked from day 1.

- [ ] **Step 1: Write `app/domain_expert/api/routers.py`**

```python
"""Expert API surface. Phase 0: all 501 stubs.

Real implementations land via vertical slices V1-V5.
Endpoints under /api/portal/agent/{agent_id}/expert/*
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Body
from app.api.deps.auth import CurrentUser, get_current_user
from app.api.deps.hub import get_hub
from .. import _config

logger = logging.getLogger("tudouclaw.api.expert")
router = APIRouter(prefix="/api/portal", tags=["expert"])


def _check_enabled():
    if _config.is_disabled():
        raise HTTPException(503, "expert module disabled (TUDOU_EXPERT_DISABLED=1)")


@router.get("/specialty-templates")
async def list_specialty_templates(
    user: CurrentUser = Depends(get_current_user),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (Track D delivers)")


@router.get("/specialty-templates/{template_id}")
async def get_specialty_template(
    template_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (Track D delivers)")


@router.get("/agent/{agent_id}/expert")
async def get_expert_status(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V1 delivers)")


@router.post("/agent/{agent_id}/expert/initialize")
async def initialize_expert(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V2 delivers)")


@router.delete("/agent/{agent_id}/expert")
async def delete_expert(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V1 delivers)")


@router.post("/agent/{agent_id}/expert/corpus/ingest")
async def corpus_ingest(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


@router.get("/agent/{agent_id}/expert/corpus")
async def corpus_list(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


@router.post("/agent/{agent_id}/expert/corpus/reindex")
async def corpus_reindex(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V3 delivers)")


@router.post("/agent/{agent_id}/expert/query")
async def expert_query(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V4 delivers)")


@router.post("/agent/{agent_id}/expert/feedback")
async def expert_feedback(
    agent_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")


@router.get("/agent/{agent_id}/expert/traces")
async def expert_traces(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")


@router.get("/agent/{agent_id}/expert/stats")
async def expert_stats(
    agent_id: str,
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    _check_enabled()
    raise HTTPException(501, "not implemented (V5 delivers)")
```

- [ ] **Step 2: Wire router into `app/api/main.py`**

Find the section where routers get included (search `include_router`):

```bash
grep -n "include_router\|app.include_router" app/api/main.py | head -10
```

Add (next to existing includes):

```python
# Expert specialty cultivation (optional module — gated by TUDOU_EXPERT_DISABLED)
try:
    from ..domain_expert.api.routers import router as expert_router
    app.include_router(expert_router)
    logger.info("expert module router registered")
except Exception as _ee:
    logger.info("expert module not registered: %s", _ee)
```

- [ ] **Step 3: Restart + smoke test all 12 endpoints**

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
# wait
JWT=$(curl -s -X POST http://localhost:9090/api/auth/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# All should return 501 (not 404 or 500)
for path in \
  "GET /api/portal/specialty-templates" \
  "GET /api/portal/agent/test/expert" \
  "POST /api/portal/agent/test/expert/initialize" \
  "GET /api/portal/agent/test/expert/corpus" \
  "POST /api/portal/agent/test/expert/query" \
  "GET /api/portal/agent/test/expert/traces" \
  "GET /api/portal/agent/test/expert/stats"
do
  method=${path%% *}
  url=${path#* }
  code=$(curl -s -o /dev/null -w "%{http_code}" -X $method "http://localhost:9090$url" \
    -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{}')
  echo "$method $url → $code"
done
# Expected: all 501
```

- [ ] **Step 4: Test feature flag**

```bash
TUDOU_EXPERT_DISABLED=1 ~/run_tudou.sh --restart 2>&1 | tail -3
# wait
curl -s -H "Authorization: Bearer $JWT" http://localhost:9090/api/portal/specialty-templates -w "\nHTTP=%{http_code}\n"
# Expected: 503 (module disabled)

# Re-enable
unset TUDOU_EXPERT_DISABLED
~/run_tudou.sh --restart 2>&1 | tail -3
# Re-run from step 3 to verify 501 returned
```

- [ ] **Step 5: Commit**

```bash
git add app/domain_expert/api/routers.py app/api/main.py
git commit -m "Phase 0 task 4: 12 expert API endpoints stubbed (501) + feature flag verified"
```

---

## Task 5: requirements-expert.txt

**Goal:** Optional dependencies isolated. Main `requirements.txt` not touched. CI can install both for full testing.

- [ ] **Step 1: Create `requirements-expert.txt`**

```
# Optional dependencies for app/domain_expert/.
# Install: pip install -r requirements-expert.txt
# When NOT installed: TUDOU_EXPERT_DISABLED=1 (or import errors gracefully degrade).

# RAG layer (Track A)
sqlite-vss>=0.1.2
sentence-transformers>=2.7.0
torch>=2.0  # mlx-lm pulls this on Mac; explicit for non-Mac fallback

# Inference & training (Track C / SP-2)
mlx-lm>=0.13.0; platform_system == "Darwin" and platform_machine == "arm64"
peft>=0.10.0
transformers>=4.40.0
datasets>=2.18.0

# Schema validation (Track D)
jsonschema>=4.21.0
pyyaml>=6.0

# Eval (Track C / SP-2)
scikit-learn>=1.4.0
```

- [ ] **Step 2: Verify install path**

```bash
~/tudou-env/bin/pip install -r requirements-expert.txt --dry-run | tail -10
# Expected: shows what would install; no errors
```

(Note: don't ACTUALLY install yet — only Phase 0 work doesn't need any of these. Tracks install per their needs.)

- [ ] **Step 3: Commit**

```bash
git add requirements-expert.txt
git commit -m "Phase 0 task 5: requirements-expert.txt for optional cultivation deps"
```

---

## Task 6: Reply pipeline hook (no-op for now)

**Goal:** Add the if-branch in agent reply pipeline that V4 will fill. For Phase 0, the branch is `pass` (no-op). This way V4 just needs to write the implementation, not edit core agent code.

- [ ] **Step 1: Find reply pipeline entry**

```bash
grep -n "def agent_reply\|async def agent_reply\|def _default_llm_reply" app/agent.py app/hub/_core.py | head -10
```

Identify the function that orchestrates an agent's response to a query.

- [ ] **Step 2: Add the hook**

(Exact location depends on findings; pseudo-code shown.)

```python
async def agent_reply(self, query, ...):
    # ── 🆕 Phase 0: expert pipeline hook (V4 fills) ──
    # When agent.expert_specialty is set AND module is enabled,
    # delegate to the expert pipeline. On any error, fall through.
    if self.expert_specialty:
        try:
            from app.domain_expert._config import is_disabled
            if not is_disabled():
                from app.domain_expert.inference import pipeline as _expert_pipeline
                if hasattr(_expert_pipeline, "answer"):
                    return await _expert_pipeline.answer(self, query, ...)
        except ImportError:
            pass  # module not fully built yet, fall through
        except Exception as e:
            logger.warning("expert pipeline failed, falling back: %s", e)
    # ── existing default path (unchanged) ──
    return await self._existing_default_reply(query, ...)
```

In Phase 0, `app.domain_expert.inference.pipeline` doesn't exist (empty `__init__.py`), so `ImportError` is caught and fallthrough works. V4 will create that module.

- [ ] **Step 3: Test backward-compat**

A普通 agent (`expert_specialty == ""`) skips the hook entirely → existing tests pass.

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
# wait — open browser, send a chat message to a regular agent
# expected: response works exactly as before
```

- [ ] **Step 4: Test `expert_specialty` set + module empty (graceful fallback)**

```bash
# Manually set expert_specialty='legal' on an agent via DB:
~/tudou-env/bin/python3 -c "
import sqlite3, json
DB = '/Users/pangwanchun/.tudou_claw/tudou_claw.db'
c = sqlite3.connect(DB)
cur = c.execute('SELECT agent_id, data FROM agents LIMIT 1')
row = cur.fetchone()
aid, data = row
d = json.loads(data)
d['expert_specialty'] = 'legal'
c.execute('UPDATE agents SET data = ? WHERE agent_id = ?', (json.dumps(d), aid))
c.commit()
print(f'Set {aid}.expert_specialty = legal')
"
~/run_tudou.sh --restart 2>&1 | tail -3
# Open chat with that agent — message should still work (fallthrough kicks in)
# Reset:
~/tudou-env/bin/python3 -c "
import sqlite3, json
DB = '/Users/pangwanchun/.tudou_claw/tudou_claw.db'
c = sqlite3.connect(DB)
cur = c.execute('SELECT agent_id, data FROM agents WHERE json_extract(data, \"\$.expert_specialty\") = \"legal\"')
for aid, data in cur.fetchall():
    d = json.loads(data); d['expert_specialty'] = ''
    c.execute('UPDATE agents SET data = ? WHERE agent_id = ?', (json.dumps(d), aid))
c.commit()
print('reset')
"
```

- [ ] **Step 5: Commit**

```bash
git commit -am "Phase 0 task 6: reply pipeline hook (no-op fallthrough until V4 fills it)"
```

---

## Task 7: End-to-end Phase 0 verification

**Goal:** Phase 0 complete sanity check. Existing functionality 100% preserved. Module skeleton importable. All 12 stub endpoints respond 501.

- [ ] **Step 1: Run all existing tests**

```bash
cd /Users/pangwanchun/AIProjects/TudouClaw_new
~/tudou-env/bin/python3 -m pytest app/ -x -q 2>&1 | tail -20
# Expected: all pass
```

- [ ] **Step 2: Test feature-flag-disabled path**

```bash
TUDOU_EXPERT_DISABLED=1 ~/tudou-env/bin/python3 -m pytest app/ -x -q 2>&1 | tail -10
# Expected: same tests pass; module import doesn't break anything
```

- [ ] **Step 3: Manual browser smoke**

1. Open portal, log in
2. Open existing agent (e.g. 小土) → send chat message → expect: works as before
3. Voice mode → expect: works as before
4. Capabilities popup → expect: works as before
5. Edit Agent → expect: works as before

- [ ] **Step 4: Final commit + tag**

```bash
git tag phase-0-complete
git log --oneline phase-0-complete~7..phase-0-complete
# Expected: 6-7 commits, all "Phase 0 task N"
```

- [ ] **Step 5: Announce Phase 0 done**

Phase 0 is the gate for Tracks A/B/C/D to fork off in parallel. Once tagged, those 4 tracks can branch from `phase-0-complete` and run independently.

---

## Self-Review

**Spec coverage check:** ☑ All foundational pieces (module skeleton, dataclasses, API stubs, feature flag, agent fields, reply hook, optional deps) covered.

**Placeholder scan:** ☑ No TBD. Each task has actual code/commands.

**Type/name consistency:** ☑ `expert_*` field names align with spec §3.2; `ExpertProfile` and `SpecialtyTemplate` field names match spec §3.

**Files-changed sanity:** ☑ Only ADDS new files + 5 fields on agent.py + 1 hook in reply pipeline + 1 line in api/main.py. Nothing removed/refactored.

**Reversibility:** ☑ `git revert phase-0-complete` cleanly removes all Phase 0 changes. `TUDOU_EXPERT_DISABLED=1` neutralizes runtime impact even before revert.

---

## Handoff to Tracks

After this plan completes and `phase-0-complete` tag is in place, the 4 tracks fork off from this point:

```bash
git tag phase-0-complete
git worktree add ../tudou-track-a -b track-a-corpus-rag phase-0-complete
git worktree add ../tudou-track-b -b track-b-sp0-ui phase-0-complete
git worktree add ../tudou-track-c -b track-c-eval-cleanup phase-0-complete
git worktree add ../tudou-track-d -b track-d-specialty-schema phase-0-complete
```

Each track's plan is self-contained and assumes Phase 0 is in place.
