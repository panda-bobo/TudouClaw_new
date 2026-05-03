# Agent ↔ Knowledge Templates Binding — Design Spec

**Date:** 2026-05-03
**Author:** brainstormed with @pangalano1983-dev
**Status:** Approved (pending review of this spec)
**Skill:** superpowers/brainstorming

---

## Goal

Let admins **explicitly bind** knowledge templates to specific agents from the Agent edit modal. Today the live `app/agent.py:7460-7479` chat path auto-matches templates by role + keyword, but there's no UI affordance for "I want THIS agent to always use these templates regardless of what auto-match decides."

This spec adds:
- A new `knowledge_templates: list[str]` field on `AgentProfile`
- An "📋 专业领域模版" section in the Agent edit modal with a checkbox list
- An injection-priority rule: explicitly-bound templates always render first; auto-match fills remaining token budget on top.

## Background — what we're keeping vs replacing

### Keeping
- `app/template_library.py` — `TemplateLibrary` singleton, `add_template/update_template/remove_template/list_templates/match_templates/render_for_agent`. Already wired through the just-fixed POST `/api/portal/templates` endpoint (commit `33fb6df`).
- Live auto-match path at `app/agent.py:7460-7479` — `match_templates(_user_text, role=self.role, limit=2)` runs every chat turn. Stays.
- Existing tests that exercise template matching (none directly, but the chat path is exercised indirectly).

### Replacing / new
- Auto-match-only behavior → **bound + auto-match** with bound taking precedence.
- Agent edit modal grows one new section between RAG mode and Self-Improvement.
- New `AgentProfile.knowledge_templates` field (currently doesn't exist; the dead-code reference at `agent_execution.py:865` was aspirational).

## Architecture

### Data model

```python
# app/agent.py — AgentProfile
@dataclass
class AgentProfile:
    ...
    knowledge_templates: list[str] = field(default_factory=list)
    # Template IDs explicitly bound to this agent. Always injected
    # at the top of the template-context block. Auto-match fills
    # the remaining token budget below.
```

Persisted via existing `AgentProfile.to_dict() / from_dict()` — same machinery that handles `expertise`, `skills`, `allowed_tools`, etc. Schema migration: missing field on legacy agent.json files defaults to `[]`.

### Injection logic — `app/agent.py:7460-7479`

Replace the current auto-match-only block:

```python
# OLD
matched_templates = tpl_lib.match_templates(_user_text, role=self.role, limit=2)
if matched_templates:
    tpl_context = tpl_lib.render_for_agent(matched_templates, max_chars=4000)
    ...

# NEW — explicit bound + auto-match dedup'd
bound_ids = list(getattr(self.profile, "knowledge_templates", []) or [])
bound_templates = []
for tid in bound_ids:
    t = tpl_lib.get_template(tid)
    if t is not None and t.enabled:
        bound_templates.append(t)

# Auto-match adds 2 more on top, dedup'd against bound
auto_matched = tpl_lib.match_templates(_user_text, role=self.role, limit=2)

seen_ids = {t.id for t in bound_templates}
final_templates = list(bound_templates)
for t in auto_matched:
    if t.id in seen_ids:
        continue
    seen_ids.add(t.id)
    final_templates.append(t)

if final_templates:
    tpl_context = tpl_lib.render_for_agent(final_templates, max_chars=4000)
    ...
```

**Order matters**: bound templates render first inside `render_for_agent` so if the budget is exhausted, auto-matched ones get truncated, not the user's explicit picks.

### UI section — Agent edit modal

Insert between the existing 「RAG 模式」段 and 「Self-Improvement」段. Standard "card" styling matching the surrounding sections (`background:var(--surface2);border-radius:8px;margin:12px 0;padding:12px`):

```
┌──────────────────────────────────────────────┐
│ 📋 专业领域模版 Knowledge Templates           │
│ ─────────────────────────────────────────    │
│ Agent 收到任务时优先注入这些模版作为领域指引。 │
│ 留空 → 仅走自动匹配（按 role + 关键词）。      │
│                                               │
│ ┌─────────────────────────────────────┐      │
│ │ ☑ 产品设计模版    [research]         │      │
│ │ ☐ 市场调研模版    [research]         │      │
│ │ ☑ 代码审查模版    [development]      │      │
│ │ ...                                 │      │
│ └─────────────────────────────────────┘      │
│                                               │
│ 已选 2 个                                     │
└──────────────────────────────────────────────┘
```

- **Checkbox list** (not multi-select dropdown) — visual parity with existing Tools section, ≤30 templates fits on screen.
- **Category badge** next to each name — muted gray pill.
- **Live counter** below — re-renders on each toggle.
- **No role filter** in v1 — admin sees all templates and decides.
- **Empty state**: when 0 templates exist in library, show inline link: "尚未创建模版 → 去 Knowledge & Memory → 专业领域 创建"
- **Loading state**: "Loading templates..." while `GET /api/portal/templates` resolves.

### Field plumbing

| Surface | Change |
|---|---|
| `app/agent.py:AgentProfile` | Add `knowledge_templates: list[str] = field(default_factory=list)` |
| `app/agent.py:AgentProfile.to_dict` | Persist the field |
| `app/agent.py:AgentProfile.from_dict` | Read with `[]` fallback |
| `app/api/routers/agents.py:update_agent_profile` | Accept `knowledge_templates` from body, pass to `AgentProfile(...)` |
| `app/server/static/js/portal_bundle.js:editAgentProfile` | Populate checkboxes from `agent.profile.knowledge_templates` |
| `app/server/static/js/portal_bundle.js:saveAgentProfile` | Collect checked IDs, include in payload |
| `app/server/static/js/portal_bundle.js` (new helper) | `_eaCollectKnowledgeTemplates()` returns checked IDs as list |

### Edge cases

| Case | Handling |
|---|---|
| Agent has bound IDs that no longer exist in library (template deleted) | `get_template(tid)` returns None → silently dropped. UI shows only currently-existing ones; saving rebuilds list to actual checked items. |
| Library is empty | UI shows empty-state hint with link. Save sends `[]` (no-op). |
| User unchecks all | `[]` saved → falls back to pure auto-match. Same as before this feature. |
| Disabled template (enabled=False) | Existing `get_template` returns the entry but `t.enabled` check filters it. Same treatment as auto-match. |
| Agent created before this feature | Field defaults to `[]` via `from_dict` fallback — same as user unchecking all. No migration needed. |
| Concurrent template add/remove while modal is open | UI is point-in-time snapshot from open. Save sends whatever was checked; if a template was deleted server-side, that ID will silently drop on next render. Acceptable. |

### What this is NOT

- **Not a replacement for auto-match.** Both run; bound takes priority slot.
- **Not per-task binding.** Bindings are per-AGENT, not per-conversation or per-prompt.
- **Not about template content.** Editing template content stays in Knowledge & Memory page.
- **Not about template authoring permissions.** Anyone who can edit an agent can bind any existing template; template authorship/visibility ACLs are out of scope.

## Backward compatibility

| Concern | Status |
|---|---|
| Agents saved before this change | `from_dict` defaults to `[]`. No migration. ✓ |
| Auto-match behavior preserved when no binding | Yes — empty list → pure auto-match path runs as today. ✓ |
| Existing API payload that doesn't include `knowledge_templates` | `update_agent_profile` falls back to current value (or `[]` for new agents). Treats absence as "don't change". ✓ |
| `tpl_lib.get_template` for deleted IDs | Returns None → already silently dropped. ✓ |

## Implementation Status

Nothing started.

- [ ] AgentProfile field + to_dict/from_dict + unit test
- [ ] API handler accepts the field + endpoint test
- [ ] Live `agent.py` injection logic refactor + behavior test (bound first, auto-match after, dedup)
- [ ] Frontend modal section + populate + collect + save wire
- [ ] Documentation (one paragraph in `docs/canvas-workflows.md`? actually wrong file — agents don't live there. Skip docs for v1, the UI is self-explanatory)
- [ ] End-to-end verification

## Self-Review

**Spec coverage of brainstormed Q&A:**

| Q | Decision | Where in spec |
|---|---|---|
| 是否按 role 过滤 | 不过滤 (v1) | "UI section" |
| 字段名 | `knowledge_templates` | "Data model" |
| UI 形态 | Checkbox + category badge | "UI section" |
| 注入语义 | 绑定先 + 自动匹配补 (dedup) | "Injection logic" |
| 位置 | RAG 之后 / Self-Improvement 之前 | "UI section" |
| 工期 | 5 task ~70 min | "Implementation Status" |

**Placeholder scan:** every section has concrete code blocks or layout sketches. No "TBD" / "implement later". Code blocks have actual function signatures, exact line ranges from current file, and example field structure. ✓

**Type consistency:** `knowledge_templates: list[str]` everywhere. `bound_templates`, `auto_matched`, `final_templates` are all `list[Template]`. `seen_ids` is `set[str]`. Names consistent. ✓

**Scope check:** single subsystem (template binding), 5 small tasks, ~70 min. No further decomposition needed.

**Risks captured:**
- Templates deleted while bound → silent drop on render (acceptable, documented)
- Empty library → UI guides user to creation page (good UX)
- No auto-migration needed (default `[]` covers existing agents)

---

## Handoff

Once user approves this spec, transition to **superpowers/writing-plans** to produce a checklist-style implementation plan in `docs/superpowers/plans/2026-05-03-agent-knowledge-templates-binding-implementation.md` covering the 5 tasks listed in Implementation Status.
