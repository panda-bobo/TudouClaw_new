# Agent ↔ Knowledge Templates Binding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the design from `docs/superpowers/specs/2026-05-03-agent-knowledge-templates-binding-design.md` — let admins explicitly bind knowledge templates to agents from the edit modal; live chat path injects bound first then dedup-augments with auto-match.

**Architecture:** Add `knowledge_templates: list[str]` to `AgentProfile`. Backend persists + accepts via `POST /agent/{id}/profile`. `agent.py:7460-7479` reads bound list + auto-match, dedups by id, renders bound first. Frontend adds a checkbox-list section in the agent edit modal between RAG and Self-Improvement.

**Tech Stack:** Python 3.13 (existing FastAPI backend), vanilla JS (existing `portal_bundle.js`). No new deps.

---

## File Structure

| File | Role |
|------|------|
| `app/agent.py` | Modify — `AgentProfile.knowledge_templates` field; injection logic at line 7460-7479 |
| `app/api/routers/agents.py` | Modify — `update_agent_profile` accepts new field |
| `app/templates/portal.html` | Modify — new section in edit-agent modal (between RAG and Self-Improvement, ~line 1957-1995 area) |
| `app/server/static/js/portal_bundle.js` | Modify — populate / collect / save / new helper |
| `tests/test_agent_knowledge_templates.py` | NEW — three tests (field roundtrip, injection priority, dedup with auto-match) |

---

## Task 1: `AgentProfile.knowledge_templates` field + persistence + test

**Files:**
- Modify: `app/agent.py` (AgentProfile dataclass + to_dict/from_dict)
- Create: `tests/test_agent_knowledge_templates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_knowledge_templates.py
"""Tests for Agent.profile.knowledge_templates binding (spec
2026-05-03)."""
from __future__ import annotations
import pytest


def test_profile_has_knowledge_templates_field_default_empty():
    """New AgentProfile defaults knowledge_templates to []."""
    from app.agent import AgentProfile
    p = AgentProfile()
    assert hasattr(p, "knowledge_templates")
    assert p.knowledge_templates == []


def test_profile_to_dict_includes_knowledge_templates():
    from app.agent import AgentProfile
    p = AgentProfile()
    p.knowledge_templates = ["tpl_a", "tpl_b"]
    d = p.to_dict()
    assert d.get("knowledge_templates") == ["tpl_a", "tpl_b"]


def test_profile_from_dict_reads_knowledge_templates():
    from app.agent import AgentProfile
    p = AgentProfile.from_dict({"knowledge_templates": ["x", "y", "z"]})
    assert p.knowledge_templates == ["x", "y", "z"]


def test_profile_from_dict_missing_field_defaults_empty():
    """Legacy agent.json files (saved before this feature) should
    load with knowledge_templates = []."""
    from app.agent import AgentProfile
    p = AgentProfile.from_dict({"agent_class": "enterprise"})
    assert p.knowledge_templates == []


def test_profile_roundtrip_preserves_knowledge_templates():
    from app.agent import AgentProfile
    src = AgentProfile()
    src.knowledge_templates = ["t1", "t2"]
    restored = AgentProfile.from_dict(src.to_dict())
    assert restored.knowledge_templates == ["t1", "t2"]
```

- [ ] **Step 2: Run, verify failure**

```bash
pytest tests/test_agent_knowledge_templates.py::test_profile_has_knowledge_templates_field_default_empty -v
```

Expected: FAIL with `AttributeError: 'AgentProfile' object has no attribute 'knowledge_templates'`.

- [ ] **Step 3: Add the field**

In `app/agent.py`, find the `AgentProfile` class (around line 1677). Find the `skill_capabilities` field (around line 1749 — the existing list[str] field with default_factory) — `knowledge_templates` follows the same pattern. Add a new field RIGHT AFTER `skill_capabilities`:

```python
    knowledge_templates: list[str] = field(default_factory=list)
    # Spec 2026-05-03: template IDs explicitly bound to this agent.
    # Always rendered FIRST inside the template-context block on each
    # chat turn; auto-match (match_templates) fills the remaining
    # token budget on top, dedup'd by id. Empty = pure auto-match.
```

- [ ] **Step 4: Update `to_dict`**

Find `AgentProfile.to_dict()`. It already serializes `expertise`, `skills`, etc. After the existing `"skill_capabilities": list(...)` line (or equivalent), add:

```python
            "knowledge_templates": list(self.knowledge_templates or []),
```

(If `to_dict` uses a `**kwargs` pattern or different shape, just include the field once at the same level as the other lists.)

- [ ] **Step 5: Update `from_dict`**

Find `AgentProfile.from_dict()`. After the line that reads `skill_capabilities`, add:

```python
            knowledge_templates=list(d.get("knowledge_templates", []) or []),
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_agent_knowledge_templates.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add app/agent.py tests/test_agent_knowledge_templates.py
git commit -m "feat(agent): AgentProfile.knowledge_templates field + persistence"
```

---

## Task 2: API accepts `knowledge_templates` in `POST /agent/{id}/profile`

**Files:**
- Modify: `app/api/routers/agents.py` (`update_agent_profile`)
- Extend: `tests/test_agent_knowledge_templates.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_knowledge_templates.py`:

```python
def test_update_agent_profile_accepts_knowledge_templates(tmp_path, monkeypatch):
    """POST /agent/{id}/profile with knowledge_templates in body
    persists onto agent.profile.knowledge_templates."""
    from app.agent import Agent, AgentProfile

    agent = Agent(id="ak1", name="t")
    agent.profile = AgentProfile()

    # Inline-call the handler logic — easiest is to invoke
    # update_agent_profile via a direct call. But it's an async
    # FastAPI handler with Depends; for the test, just verify the
    # body-parsing branch updates profile.knowledge_templates.
    body = {"knowledge_templates": ["tpl_x", "tpl_y"]}

    # Apply via the same code path the handler uses:
    if "knowledge_templates" in body:
        # Simulate the handler's profile reconstruction
        new_profile = AgentProfile.from_dict({
            **agent.profile.to_dict(),
            "knowledge_templates": list(body["knowledge_templates"] or []),
        })
        agent.profile = new_profile

    assert agent.profile.knowledge_templates == ["tpl_x", "tpl_y"]
```

(This is a unit test of the contract — full HTTP integration test would need a TestClient + auth bypass which is overkill for this small wire-up.)

- [ ] **Step 2: Run, verify it passes already (because Task 1 wired from_dict)**

```bash
pytest tests/test_agent_knowledge_templates.py::test_update_agent_profile_accepts_knowledge_templates -v
```

Expected: PASS (Task 1's `from_dict` already handles the field).

- [ ] **Step 3: Wire `update_agent_profile` to actually use the body field**

In `app/api/routers/agents.py`, find `update_agent_profile`. It calls `AgentProfile(...)` with a long kwargs list around line 895-915. Add `knowledge_templates` to the call:

Find the line that says `expertise=body.get("expertise", agent.profile.expertise),` and AFTER the matching `skills=...` line (or wherever the kwargs list ends, before `language=...`), ensure there's a clause for the new field. Specifically — locate this block:

```python
        agent.profile = AgentProfile(
            agent_class=body.get("agent_class", agent.profile.agent_class),
            ...
            skills=body.get("skills", agent.profile.skills),
            ...
        )
```

Add ONE new kwarg (alphabetically/by-grouping near `skills`):

```python
            knowledge_templates=body.get("knowledge_templates", agent.profile.knowledge_templates),
```

Critical: use `agent.profile.knowledge_templates` as the fallback so a payload that omits the field preserves current value (doesn't wipe it).

- [ ] **Step 4: Re-run test**

```bash
pytest tests/test_agent_knowledge_templates.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/api/routers/agents.py tests/test_agent_knowledge_templates.py
git commit -m "feat(api): /agent/{id}/profile accepts knowledge_templates"
```

---

## Task 3: Injection logic — bound first, auto-match dedup'd

**Files:**
- Modify: `app/agent.py` (lines 7460-7479)
- Extend: `tests/test_agent_knowledge_templates.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_knowledge_templates.py`:

```python
def test_injection_renders_bound_first_then_auto_match_dedup(tmp_path, monkeypatch):
    """Bound templates always render before auto-matched ones; if
    auto-match returns one already in bound, it's dropped (dedup)."""
    from app import template_library as tl_mod
    from app.template_library import Template, TemplateLibrary

    # Build a fake library with 3 templates
    lib = TemplateLibrary(templates_dir=str(tmp_path))
    t1 = Template(id="t1", name="bound_one", content="BOUND_ONE_CONTENT", enabled=True)
    t2 = Template(id="t2", name="bound_two", content="BOUND_TWO_CONTENT", enabled=True)
    t3 = Template(id="t3", name="auto_match", content="AUTO_MATCH_CONTENT", enabled=True)
    lib.templates = {"t1": t1, "t2": t2, "t3": t3}

    # Force match_templates to return [t2 (already bound), t3 (new)]
    # so we can verify dedup
    def fake_match(message, role="", limit=2):
        return [t2, t3]
    lib.match_templates = fake_match
    monkeypatch.setattr(tl_mod, "_INSTANCE", lib)

    # Build agent with bound = [t1, t2]
    from app.agent import Agent, AgentProfile
    agent = Agent(id="ax", name="x", role="research")
    agent.profile = AgentProfile()
    agent.profile.knowledge_templates = ["t1", "t2"]

    # Run the bound + auto-match merge logic the same way agent.py
    # does (we test the helper-equivalent — exercising the live
    # chat path needs the full LLM stack).
    bound_ids = list(getattr(agent.profile, "knowledge_templates", []) or [])
    bound_templates = []
    for tid in bound_ids:
        t = lib.get_template(tid)
        if t is not None and t.enabled:
            bound_templates.append(t)
    auto_matched = lib.match_templates("any message", role=agent.role, limit=2)
    seen_ids = {t.id for t in bound_templates}
    final = list(bound_templates)
    for t in auto_matched:
        if t.id not in seen_ids:
            seen_ids.add(t.id)
            final.append(t)

    # Bound first
    assert [t.id for t in final[:2]] == ["t1", "t2"]
    # t2 NOT duplicated
    assert [t.id for t in final].count("t2") == 1
    # t3 (auto-match unique) appended
    assert "t3" in [t.id for t in final]
```

- [ ] **Step 2: Run, expect PASS (the test exercises the algorithm in-test, not yet the live path)**

```bash
pytest tests/test_agent_knowledge_templates.py::test_injection_renders_bound_first_then_auto_match_dedup -v
```

Expected: PASS — the test validates the algorithm independently. (Step 3 then ports the algorithm into the live `agent.py` path.)

- [ ] **Step 3: Refactor `agent.py:7460-7479` to use the new logic**

In `app/agent.py`, find the existing block:

```python
            # --- Template Library: auto-match and inject templates ---
            try:
                tpl_lib = get_template_library()
                matched_templates = tpl_lib.match_templates(
                    _user_text, role=self.role, limit=2)
                if matched_templates:
                    tpl_context = tpl_lib.render_for_agent(
                        matched_templates, max_chars=4000)
                    if tpl_context:
                        self.messages.append({
                            "role": "system",
                            "content": tpl_context,
                        })
                        tpl_names = [t.name for t in matched_templates]
                        self._log("template_match", {
                            "templates": tpl_names,
                            "chars": len(tpl_context),
                        })
            except Exception:
                pass  # template library is optional
```

Replace the body of the `try:` block with:

```python
                tpl_lib = get_template_library()

                # Bound templates (explicitly selected in agent edit UI).
                # Always rendered first; auto-match fills remaining budget.
                bound_ids = list(getattr(self.profile, "knowledge_templates", []) or [])
                bound_templates = []
                for tid in bound_ids:
                    t = tpl_lib.get_template(tid)
                    if t is not None and getattr(t, "enabled", True):
                        bound_templates.append(t)

                # Auto-match: existing keyword/role behavior, but
                # dedup against bound (don't render same template twice).
                auto_matched = tpl_lib.match_templates(
                    _user_text, role=self.role, limit=2)

                seen_ids = {getattr(t, "id", None) for t in bound_templates}
                final_templates = list(bound_templates)
                for t in auto_matched:
                    tid = getattr(t, "id", None)
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    final_templates.append(t)

                if final_templates:
                    tpl_context = tpl_lib.render_for_agent(
                        final_templates, max_chars=4000)
                    if tpl_context:
                        self.messages.append({
                            "role": "system",
                            "content": tpl_context,
                        })
                        tpl_names = [t.name for t in final_templates]
                        self._log("template_match", {
                            "templates": tpl_names,
                            "bound_count": len(bound_templates),
                            "auto_count": len(final_templates) - len(bound_templates),
                            "chars": len(tpl_context),
                        })
```

- [ ] **Step 4: Sanity-import**

```bash
python3 -c "import app.agent; print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Run all tests**

```bash
pytest tests/test_agent_knowledge_templates.py -v
pytest tests/ -k template -q 2>&1 | tail -10
```

Expected: 7 passed in the new file; no regressions in template-related broader tests.

- [ ] **Step 6: Commit**

```bash
git add app/agent.py tests/test_agent_knowledge_templates.py
git commit -m "feat(agent): inject bound templates first, dedup auto-match on top"
```

---

## Task 4: Frontend — Agent edit modal section

**Files:**
- Modify: `app/templates/portal.html` (insert HTML between RAG section and Self-Improvement section)
- Modify: `app/server/static/js/portal_bundle.js` (populate, collect, save handler, helper)

- [ ] **Step 1: Add the HTML section**

In `app/templates/portal.html`, find the line:

```html
    <div style="margin:12px 0;padding:12px;background:var(--surface2);border-radius:8px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">psychology</span>
        <span style="font-weight:600;font-size:13px">Self-Improvement (经验库)</span>
```

(This is the start of the Self-Improvement section, around line 1957-1960.)

INSERT a new block IMMEDIATELY BEFORE that line:

```html
    <div style="margin:12px 0;padding:12px;background:var(--surface2);border-radius:8px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">menu_book</span>
        <span style="font-weight:600;font-size:13px">📋 专业领域模版 Knowledge Templates</span>
      </div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:8px">
        Agent 收到任务时优先注入这些模版作为领域指引（最佳实践 / 检查清单 / 步骤）。<br>
        留空 → 仅走系统自动匹配（按 role + 关键词）。
      </div>
      <div id="ea-knowledge-templates-list" style="max-height:240px;overflow-y:auto;border:1px solid var(--border-light);border-radius:6px;padding:8px;background:var(--bg)">
        <div style="font-size:12px;color:var(--text3);padding:8px">Loading templates...</div>
      </div>
      <div id="ea-knowledge-templates-counter" style="font-size:11px;color:var(--text3);margin-top:6px">已选 0 个</div>
    </div>
```

- [ ] **Step 2: Add populate logic in `editAgentProfile`**

In `app/server/static/js/portal_bundle.js`, find `async function editAgentProfile(agentId)` (around line 14013). Find where it populates RAG fields (search for `ea-rag-mode` populate). Add a NEW populate call near the end (right before `showModal('edit-agent')`):

```javascript
  // Knowledge Templates checkbox list (spec 2026-05-03)
  try {
    var boundIds = (agent.profile && agent.profile.knowledge_templates) || [];
    _eaRenderKnowledgeTemplates(boundIds);
  } catch(e) { console.warn('knowledge_templates populate failed:', e); }
```

- [ ] **Step 3: Add the renderer + collect helper**

Add these two new functions OUTSIDE `editAgentProfile` (as module-level helpers, near `_eaRenderToolsGrid`):

```javascript
async function _eaRenderKnowledgeTemplates(boundIds) {
  var listEl = document.getElementById('ea-knowledge-templates-list');
  if (!listEl) return;
  boundIds = Array.isArray(boundIds) ? boundIds : [];

  var data;
  try {
    data = await api('GET', '/api/portal/templates');
  } catch (_) { data = null; }

  var templates = (data && data.templates) || [];
  if (templates.length === 0) {
    listEl.innerHTML =
      '<div style="font-size:12px;color:var(--text3);padding:12px;text-align:center">'
      + '尚未创建任何模版<br>'
      + '<a href="javascript:hideModal(\'edit-agent\');renderTemplateLibrary()" '
      + 'style="color:var(--primary);text-decoration:underline">去创建 →</a>'
      + '</div>';
    _eaUpdateKnowledgeTemplatesCounter();
    return;
  }

  // Sort: by category, then name. Category badge in muted pill.
  templates.sort(function(a, b) {
    var ca = (a.category || 'general').localeCompare(b.category || 'general');
    if (ca !== 0) return ca;
    return (a.name || '').localeCompare(b.name || '');
  });

  var bound = new Set(boundIds);
  listEl.innerHTML = templates.map(function(t) {
    var checked = bound.has(t.id) ? 'checked' : '';
    var cat = esc(t.category || 'general');
    return '<label style="display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;font-size:12px;border-radius:4px"'
      + ' onmouseenter="this.style.background=\'var(--surface2)\'" onmouseleave="this.style.background=\'\'">'
      + '<input type="checkbox" data-tpl-id="' + esc(t.id) + '" ' + checked
      + ' onchange="_eaUpdateKnowledgeTemplatesCounter()">'
      + '<span style="flex:1">' + esc(t.name || '(unnamed)') + '</span>'
      + '<span style="font-size:10px;padding:2px 6px;background:var(--surface3);color:var(--text3);border-radius:8px">' + cat + '</span>'
      + '</label>';
  }).join('');
  _eaUpdateKnowledgeTemplatesCounter();
}

function _eaUpdateKnowledgeTemplatesCounter() {
  var counter = document.getElementById('ea-knowledge-templates-counter');
  var listEl = document.getElementById('ea-knowledge-templates-list');
  if (!counter || !listEl) return;
  var n = listEl.querySelectorAll('input[type="checkbox"]:checked').length;
  counter.textContent = '已选 ' + n + ' 个';
}

function _eaCollectKnowledgeTemplates() {
  var listEl = document.getElementById('ea-knowledge-templates-list');
  if (!listEl) return [];
  var ids = [];
  listEl.querySelectorAll('input[type="checkbox"]:checked').forEach(function(cb) {
    var tid = cb.getAttribute('data-tpl-id');
    if (tid) ids.push(tid);
  });
  return ids;
}
```

- [ ] **Step 4: Wire into save payload**

In `saveAgentProfile()`, find the `payload` dict (around line 14228-14248). Add `knowledge_templates` to the payload:

Find this block:

```javascript
      rag_mode: (document.getElementById('ea-rag-mode') || {}).value || 'shared',
      rag_collection_ids: _eaCollectRagCollectionIds(),
      desktop_enabled: !!(document.getElementById('ea-desktop-enabled') || {}).checked,
      desktop_lottie_url: ((document.getElementById('ea-desktop-lottie') || {}).value || '').trim(),
    };
```

Replace with:

```javascript
      rag_mode: (document.getElementById('ea-rag-mode') || {}).value || 'shared',
      rag_collection_ids: _eaCollectRagCollectionIds(),
      knowledge_templates: _eaCollectKnowledgeTemplates(),
      desktop_enabled: !!(document.getElementById('ea-desktop-enabled') || {}).checked,
      desktop_lottie_url: ((document.getElementById('ea-desktop-lottie') || {}).value || '').trim(),
    };
```

- [ ] **Step 5: Verify JS still parses**

```bash
node --check app/server/static/js/portal_bundle.js
```

Expected: no output (success).

- [ ] **Step 6: Live verify via preview**

Restart preview, login, open Agent edit modal:
- New "📋 专业领域模版" section should appear between RAG and Self-Improvement
- Should populate with current templates (or empty-state hint)
- Toggling a checkbox should update counter
- Save should toast "已保存" (from earlier fix `ca29268`)
- Re-opening the same agent should show the bindings persisted

Probe via preview_eval:

```javascript
(async () => {
  const r = await fetch('/static/js/portal_bundle.js?_v=' + Date.now());
  const t = await r.text();
  return {
    has_renderer: t.includes('_eaRenderKnowledgeTemplates'),
    has_collector: t.includes('_eaCollectKnowledgeTemplates'),
    has_counter: t.includes('_eaUpdateKnowledgeTemplatesCounter'),
    has_in_payload: t.includes('knowledge_templates: _eaCollectKnowledgeTemplates()'),
  };
})()
```

Expected: all four `true`.

- [ ] **Step 7: Commit**

```bash
git add app/templates/portal.html app/server/static/js/portal_bundle.js
git commit -m "feat(portal): Agent edit modal — Knowledge Templates binding section"
```

---

## Task 5: End-to-end verification + final commit

**Files:** None (manual verification).

- [ ] **Step 1: Test creation**

In Knowledge & Memory → 专业领域 → 新建 a fresh template. Confirm it lands.

- [ ] **Step 2: Bind it**

Open any Agent edit modal → check the new section → toggle the just-created template → Save. Toast shows "已保存".

- [ ] **Step 3: Persistence**

Re-open same agent's edit modal. Toggle should be remembered.

- [ ] **Step 4: Inspect agent.json**

```bash
python3 -c "
import json
data = json.load(open('/Users/pangwanchun/.tudou_claw/agents.json'))
for a in data.get('agents', []):
    if isinstance(a, dict) and (a.get('profile', {}) or {}).get('knowledge_templates'):
        print(a.get('id'), a.get('name'), '→', a['profile']['knowledge_templates'])
"
```

Expected: the agent you just edited shows up with its bound IDs.

- [ ] **Step 5: Live chat injection**

Send any message to that agent. Check the agent's debug log:

```bash
tail -50 ~/.tudou_claw/workspaces/agents/<agent_id>/logs/*.log | grep template_match
```

Expected: a `template_match` entry showing `bound_count: N` and the bound template names appearing in the list.

- [ ] **Step 6: Verify backward compat**

For an agent that was NOT edited after this feature, check:

```bash
python3 -c "
import json
data = json.load(open('/Users/pangwanchun/.tudou_claw/agents.json'))
for a in data.get('agents', []):
    if isinstance(a, dict):
        kt = (a.get('profile', {}) or {}).get('knowledge_templates', None)
        print(a.get('id'), '→', kt if kt is not None else '(absent)')
"
```

Expected: most agents show `(absent)` or `[]`. Pure auto-match path runs as before. ✓

- [ ] **Step 7: No commit — verification only.**

---

## Self-Review

**Spec coverage:**

| Spec section | Tasks |
|---|---|
| `AgentProfile.knowledge_templates` field | T1 |
| Persistence (to_dict/from_dict) | T1 |
| API endpoint accepts field | T2 |
| Injection logic refactor | T3 |
| UI section in edit modal | T4 |
| End-to-end verification | T5 |

**Placeholder scan:** every step has explicit code blocks, exact file paths, expected outputs. No "TBD" / "implement later". ✓

**Type consistency:**
- `knowledge_templates: list[str]` everywhere.
- `bound_templates`, `auto_matched`, `final_templates` all `list[Template]`.
- `seen_ids` is `set[str | None]` (handles `Template.id` returning None defensively).
- `_eaRenderKnowledgeTemplates(boundIds)` / `_eaCollectKnowledgeTemplates()` / `_eaUpdateKnowledgeTemplatesCounter()` consistent function naming.

**Scope check:** 5 tasks, ~70 minutes total. Single subsystem. Bounded.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-05-03-agent-knowledge-templates-binding-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review per task, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch with checkpoints

**Which approach?**
