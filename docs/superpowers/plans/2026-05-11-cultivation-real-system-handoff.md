# Specialty Cultivation System — Real System Handoff

**Status**: Foundation laid (R1 done). 4 more rounds queued.
**Last commit**: `13f58a3` (template inheritance)
**Next session goal**: Build a *real* specialty cultivation system per the 4-piece formula, not a dashboard with disconnected wires.

---

## The 4-Piece Formula (terminal abstraction — do not add layers)

Agreed with user 2026-05-10:

```
专家 Agent (具体子领域) = 专家 Prompt + 专家知识库 + 通用 Skill + 专家 Skill
                             ①              ②            ③          ④
```

| # | What | Lives in | Editable via |
|---|---|---|---|
| ① 专家 Prompt | system message text | `expert/<agent>/profile.json` (override) + template (default) | Cultivation hub UI |
| ② 专家知识库 | per-agent corpus chunks (typed) | `expert/<agent>/kb/` | Upload form + RAG |
| ③ 通用 Skill | tools all agents share | marketplace `tag=common` | Skill marketplace |
| ④ 专家 Skill | tools specialty-specific | marketplace `tag=specialty:<key>` | Skill marketplace |

**Critical rule**: 红线 + 流程 are NOT separate subsystems.
- Top-level (5-10 hard guardrails): in ① system prompt
- Detail rules (30-100): in ② KB with `metadata.type=red_line`
- SOP details: in ② KB with `metadata.type=sop`

Engine retrieves from KB by relevance + groups by `type` when injecting.

---

## Sub-specialty granularity (R1 implemented)

```
specialty_templates/
├── legal.yaml                  ← legacy flat (kept for compat)
├── legal/
│   ├── _base.yaml             ← abstract base (extends-only)
│   ├── civil_law.yaml         ← 民法专家 (extends legal/_base)
│   └── (future: criminal_law.yaml, middle_east.yaml, ip_law.yaml)
├── medical/
│   ├── _base.yaml
│   ├── cardiology.yaml
│   └── ...
└── video_editing/
    ├── _base.yaml
    ├── short_video.yaml
    └── ...
```

**Inheritance semantics** (already coded in `template_loader.py`):
- `extends: <key>` resolves recursively, cycle-detected
- `abstract: true` → can't `load()` directly
- Deep merge: dicts merge recursively, lists APPEND with string-dedup
- `__replace__: true` marker = full replace (rare escape hatch)

---

## What's already done (this session)

| Round | Commit | Content |
|---|---|---|
| Recovery | 0315982 | voice mode + tech-style + Phase 0 cultivation skeleton |
| V1-V5 + UI | a449578 → 16ebba9 | Template / Bundle / Corpus / Trace / LoRA / Routing scaffolding |
| 段位 chip + bug fixes | 7bd997a → 66a5622 | accuracy-driven 段位, Provider Edit fix, source_id `?` ghost, etc. |
| In-flow RAG | 337261a | agent.chat injects retrieved chunks (keyword retrieval), writes traces |
| Department fix | a372f09 | `/profile` endpoint persists `department` |
| **R1 Template inheritance** | **13f58a3** | extends/abstract/nested-dirs + legal/_base + legal/civil_law demo |

Total: 23 commits to `origin/main`. 257 tests pass + 3 skipped.

---

## Remaining roadmap (R2-R10)

### Session A — Core Engine (~5h, this is the meaty one)

#### R2: Schema extension
File: `app/domain_expert/template.py`

Add to `SpecialtyTemplate`:
```python
@dataclass
class PromptBlock:
    role: str = ""              # "民法专家,公司法务助手"
    scope: str = ""             # "我专门处理民事问题..."
    core_red_lines: list[CoreRedLine] = field(default_factory=list)
    output_format: str = ""

@dataclass
class CoreRedLine:
    id: str
    pattern: str = ""           # regex (optional)
    message: str = ""           # refuse text shown to user
    severity: str = "HARD_REFUSE"  # or "SOFT_WARN"

@dataclass
class KBSeed:
    file: str                   # path under seeds/
    type: str                   # red_line / sop / law / template / case / internal_doc
    title: str = ""             # display name in KB list
```

Wire into `SpecialtyTemplate`:
```python
prompt: PromptBlock | None = None
kb_seeds: list[KBSeed] = field(default_factory=list)
```

Update `schema()` accordingly. Backward-compatible: `prompt` and `kb_seeds`
are optional; existing templates (legal.yaml) stay valid.

#### R3: Prompt renderer + KB seed ingest

File: `app/domain_expert/prompt_renderer.py` (new)
```python
def render_specialty_system_prompt(template: SpecialtyTemplate,
                                    overrides: dict | None = None) -> str:
    """Compose system prompt from PromptBlock + sub-specialty scope."""
    parts = []
    if template.prompt.role:
        parts.append(f"# 角色\n{template.prompt.role}")
    if template.prompt.scope:
        parts.append(f"# 范围\n{template.prompt.scope}")
    if template.prompt.core_red_lines:
        parts.append("# ⚠️ 不能做的事 (硬护栏)")
        for rl in template.prompt.core_red_lines:
            parts.append(f"  - {rl.id}: {rl.message}")
    if template.prompt.output_format:
        parts.append(f"# 输出格式\n{template.prompt.output_format}")
    return "\n\n".join(parts)
```

File: `app/domain_expert/kb_seed_loader.py` (new)
```python
def ingest_seeds_into_agent_kb(agent_id: str, template: SpecialtyTemplate):
    """Read each kb_seeds entry, chunk it, write to expert/<agent>/kb/
    with metadata.type set."""
    for seed in template.kb_seeds:
        seed_path = resolve_seed_path(seed.file)
        content = read_file(seed_path)
        chunks = chunker.get('paragraph').chunk(content, {...})
        write_chunks_with_type(agent_id, seed.title, chunks, type=seed.type)
```

Hook into `bundle_apply`: when initializing an agent, call
`ingest_seeds_into_agent_kb(agent.id, template)` so the per-agent KB
gets seeded with the template's reference materials.

#### R4: Red-line engine

File: `app/agent.py` — modify `chat()` method.

Add red-line check helper:
```python
def _check_red_lines(self, text: str, template) -> CoreRedLine | None:
    if not template or not template.prompt:
        return None
    for rl in template.prompt.core_red_lines:
        if rl.severity == "HARD_REFUSE" and rl.pattern:
            if re.search(rl.pattern, text, re.IGNORECASE):
                return rl
    return None
```

Pre-check: right after the user_text is normalized (around line 9382),
before the LLM call:
```python
template = _load_specialty_template_for_agent(self)
hit = self._check_red_lines(_user_text, template)
if hit:
    self._log("safety_blocked", {"rule_id": hit.id, "stage": "pre"})
    refuse = hit.message
    self.messages.append({"role": "assistant", "content": refuse, "_source": "safety"})
    # Save + return
    return refuse
```

Post-check: right after `final_content` is set (around line 11260),
before the save block:
```python
hit = self._check_red_lines(final_content, template)
if hit:
    self._log("safety_blocked", {"rule_id": hit.id, "stage": "post"})
    final_content = hit.message  # replace LLM output
```

#### R5: Typed RAG injection

File: `app/agent.py` — find the in-flow RAG block (around line 9395+
where `chunks = _pl._retrieve(...)` is called).

Currently:
```python
ctx_block = "\n\n".join(f"[来源: {c['source_id']}]\n{c['text']}" for c in chunks)
```

Change to group by `metadata.type`:
```python
groups = {}
for c in chunks:
    t = c.get('metadata', {}).get('type', 'reference')
    groups.setdefault(t, []).append(c)

sections = []
type_titles = {
    'red_line': '⚠️ 适用红线规则',
    'sop': '📋 参考工作流程',
    'law': '📖 法条参考',
    'template': '📑 模板参考',
    'case': '⚖️ 案例参考',
    'internal_doc': '📁 内部文档',
    'reference': '📚 参考资料',
}
for t, items in groups.items():
    section = f"=== {type_titles.get(t, t.upper())} ({len(items)}) ===\n"
    section += "\n\n".join(f"[{t} · {c['source_id']}]\n{c['text']}" for c in items)
    sections.append(section)

sys_msg = "\n\n".join(sections) + "\n\n" + 引用要求...
```

Update `pipeline.answer()` similarly so REST `/expert/query` is consistent.

---

### Session B — Real RAG + Skill (~5h)

#### R6: bge-m3 + sqlite-vss

Files involved:
- `app/domain_expert/retrieval/embedder.py` (already has stub; replace with real bge-m3)
- `app/domain_expert/corpus/store.py` (sqlite-vss)
- `app/domain_expert/inference/pipeline.py::_retrieve` (replace keyword overlap)

Steps:
1. `pip install sentence-transformers sqlite-vss` (Mac wheel exists for sqlite-vss)
2. Lazy-load bge-m3 model in `embedder.py` (cache in `~/.tudou_claw/models/bge-m3/`)
3. On `corpus/ingest` with content: chunk → embed → store in sqlite-vss table
4. `_retrieve(query, k)` → embed query → vector similarity search → top-k

Migration for existing per-agent KBs:
- Detect chunks.jsonl files without embeddings
- Backfill on first query (or via explicit `/corpus/reindex` endpoint)

#### R7: contract_reviewer skill (demo)

File: `app/skills/contract_review/`
- `manifest.yaml` — skill metadata (name, version, params)
- `handler.py` — implements review logic:
  1. Split contract by `第X条` regex
  2. For each section: query KB for `metadata.type=red_line` + `metadata.scope=contract`
  3. LLM judgment: "this clause violates which rule, severity, fix?"
  4. Return structured report `{sections: [{location, severity, rule_id, cite, fix}]}`

---

### Session C — UI + Examples (~5h)

#### R8: Cultivation Hub UI redesign

File: `app/server/static/js/portal_bundle.js` — find `renderCultivationHub()`,
`_awsCultivationCultivatedCard()`, `_renderCultivationWorkflowPage()`.

Replace 6-module pipeline with 4-section profile editor:
```
小土 · 民法专家 (legal/civil_law v1.0)
┌────────────────────────────────────────────────┐
│ ① 专家 Prompt                          [编辑]   │
│   role: 民法专家                              │
│   scope: 我专门处理民事问题...               │
│   core_red_lines (3):                         │
│     ⛔ lawsuit_guarantee                      │
│     ⛔ criminal_question                      │
│     ⛔ replace_lawyer                         │
├────────────────────────────────────────────────┤
│ ② 专家知识库                          [上传/管理]│
│   private KB · 8 sources · 234 chunks          │
│   ─ 类型分布:                                  │
│     red_line: 30  sop: 12  law: 156           │
│     template: 5  case: 23  internal_doc: 8    │
├────────────────────────────────────────────────┤
│ ③ 通用 Skill (marketplace tag=common)          │
│   ☑ file_ops  ☑ web_search  ☑ send_email       │
├────────────────────────────────────────────────┤
│ ④ 专家 Skill (tag=specialty:legal/civil)        │
│   ☑ contract_reviewer                          │
│   ☑ civil_case_analyzer                        │
└────────────────────────────────────────────────┘
            [💬 跟小土对话]   [⚙️ 高级]
```

The "高级" button hides Trace/LoRA/Routing (current 6-module Pipeline)
behind a secondary view. Power users only.

#### R9: Sub-specialty examples

Create: `legal/criminal_law.yaml`, `legal/middle_east.yaml`,
`video_editing/_base.yaml`, `video_editing/short_video.yaml`,
`medical/_base.yaml`, `medical/cardiology.yaml`.

Each demonstrates:
- `extends:` parent
- own scope + red_lines (don't answer outside-domain questions)
- own kb_seeds + skills

#### R10: Tech-ify all pages

User mandate: 所有页面都要 tech 化.

Audit:
- ✅ Worker Nodes (already tech)
- ✅ KB list (commit b18ce32)
- ✅ Cultivation hub > 待养成 (commit e351883)
- ❌ Cultivation hub > 我的专家 (cards are basic)
- ❌ Cultivation hub > 模板库 (mixed style)
- ❌ Cultivation hub > Wiki (depends on imported _renderKmWiki)
- ❌ Settings sub-tabs (mostly legacy style)
- ❌ Agents list, Project list, etc.

Pattern to apply: tc-card / tc-mono-label / tc-btn / tc-text-dim,
cyber-magenta / cyber-blue / cyber-lime accents, dashed-border add cards,
mono-font labels.

---

## Critical context for whoever picks up

### Architecture mistakes to avoid

❌ **Don't** add a "Reviewer engine" subsystem. Make `contract_reviewer`
a regular skill in marketplace.

❌ **Don't** add early-return hooks in `agent.chat`. They bypass
transcript / on_event / streaming. We learned this the hard way
(commits 7171542 / 337261a). All cultivation logic must integrate
INSIDE the normal chat flow.

❌ **Don't** make corpus shared per-specialty. It's per-agent — 小土
vs 张总都是法务但 corpus 不同。 Templates' `kb_seeds` are SEEDS
(initial copy), not live shared storage.

❌ **Don't** make 红线 / 流程 their own subsystem. They're either
in system prompt (top-level hard guardrails) or in KB with `metadata.type`.

### File map

```
app/domain_expert/
├── _config.py             unchanged
├── template.py            R2: extend with PromptBlock / CoreRedLine / KBSeed
├── template_loader.py     R1 done — has inheritance
├── prompt_renderer.py     R3: NEW
├── kb_seed_loader.py      R3: NEW
├── bundle_apply.py        R3: hook seed ingestion
├── corpus/
│   ├── manifest.py        R6: extend metadata.type column
│   └── store.py           R6: implement sqlite-vss
├── retrieval/
│   ├── embedder.py        R6: implement bge-m3
│   └── pipeline.py        R5: typed grouping
├── inference/
│   └── pipeline.py        R5: typed grouping in _retrieve
└── api/
    └── routers.py         R2: maybe extend response schemas

app/agent.py               R4: red-line pre/post check
                           R5: typed RAG injection (existing block)

app/skills/
└── contract_review/       R7: NEW skill module
    ├── manifest.yaml
    ├── handler.py
    └── red_lines/

app/server/static/js/portal_bundle.js
                           R8: profile editor UI
                           R10: tech-ify remaining pages

app/data/specialty_templates/
├── legal.yaml             unchanged (legacy)
├── legal/                 R1 done
│   ├── _base.yaml
│   └── civil_law.yaml
├── legal/criminal_law.yaml    R9
├── legal/middle_east.yaml     R9
├── medical/                   R9
└── video_editing/             R9
```

### How to verify each round end-to-end

After R2-R5: write a test sub-specialty `legal/test_red_lines.yaml`
with one HARD_REFUSE rule (e.g., pattern: `保证胜诉`). Cultivate an
agent with it. Send "保证我能胜诉" → agent must reply with the rule's
message, not LLM output. Check tudou.log for `[safety_blocked]`.

After R6: ask 小土 "老板让我替他签字怎么办" — semantic question that
keyword retrieval misses. Real RAG should retrieve the relevant
"代理权"  / 表见代理 chunks and produce a grounded answer with
[来源:] citations.

After R7: paste a contract via the cultivation hub UI. Get back a
risk report with red/yellow/green markers + cited rule for each
finding.

After R10: every page in the portal renders with tech-style chrome
(no naked `<button class="btn">` mixed with tc-btn tc-card etc.).

---

## Quick start for next session

```bash
# Pick up where we left off
git pull origin main
git log --oneline -1   # should be 13f58a3

# Read this handoff
cat docs/superpowers/plans/2026-05-11-cultivation-real-system-handoff.md

# Start R2
# (extend SpecialtyTemplate dataclass with PromptBlock / CoreRedLine / KBSeed)

# Run tests after each round
python3 -m pytest app/domain_expert/tests/ -q

# Commit + push each round (do NOT batch multiple rounds)
git add ...
git commit -m "feat(cultivation R2): ..."
git push origin main
```

### Estimated remaining work

| Session | Rounds | Hours |
|---|---|---|
| A: Core engine | R2 + R3 + R4 + R5 | 5h |
| B: Real RAG + skill | R6 + R7 | 5h |
| C: UI + examples | R8 + R9 + R10 | 5h |
| **Total** | | **~15h** |

Each round = independent commit. Can stop and resume any time.

---

## Specific user statements that drove this design

Quote these back if the next agent gets confused:

- "所有的专家共性是: 哪些可以做,流程是什么, 哪些不可以做。红线是什么"
- "每个 agent 要有自己的专属知识库 (知识,流程等),专属 skill"
- "专家 Agent = 专家 Prompt + 专家知识库 + 通用 skill + 专家 skill"
- "比如: 民法专家, 刑法专家, 中东法律专家"
- "红线 / 流程 不是单独子系统 — 是 ① 专家 Prompt 的一部分 (写进 system message 里) — 这个也可能是 RAG 的一部分"
- "OK 要一个真实的专家养成系统"
- "另外页面都要 tech 化"

---

End of handoff.
