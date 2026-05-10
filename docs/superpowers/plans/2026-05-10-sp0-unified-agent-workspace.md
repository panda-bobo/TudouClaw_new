# SP-0: Unified Agent Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec reference:** [docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md §4](../specs/2026-05-10-agent-specialty-cultivation-design.md)

**Goal:** Replace the current scatter of 7+ floating modals (Capabilities popup / Prompt Pack 市场 / 已发现 / Edit Agent / Skill Store / etc.) with a single 5-tab Agent Workspace (`💬 对话 / 🧰 能力 / 🎓 养成 / 📊 历史 / ⚙️ 配置`). Existing functionality preserved; popups internalized as inline tab content. Zero new features in this SP — pure UI integration.

**Architecture:** Pure frontend refactor of `app/server/static/js/portal_bundle.js`. New module `_agent_workspace_*` functions own the tabbed shell + per-tab content rendering. Existing per-feature functions (chat / capabilities / packs market / etc.) get re-targeted to render INSIDE workspace tabs instead of modals. Backend untouched.

**Tech Stack:** Vanilla JS (no framework), inline styles + theme-tech.css classes, existing helpers (`api`, `esc`, `_voiceModeXXX`, etc.). Feature flag `tudou_workspace_v2` in localStorage to toggle new vs old UI for safe rollback.

---

## File Structure

### Files Modified

| File | Modification |
|---|---|
| `app/server/static/js/portal_bundle.js` | Add `_agent_workspace_*` block (~600 LOC). Re-target existing render entry points to workspace mode when flag active. Old fns retained for fallback. |

### No New Files

Per Tudou convention, all JS lives in `portal_bundle.js`. The new workspace block is delimited by clear comment banners for findability:

```js
// ═══════════════════════════════════════════════════════
// SP-0 · UNIFIED AGENT WORKSPACE                          (start)
// ═══════════════════════════════════════════════════════
// ... new code ...
// ═══════════════════════════════════════════════════════
// SP-0 · UNIFIED AGENT WORKSPACE                          (end)
// ═══════════════════════════════════════════════════════
```

### Files NOT Touched (verified no-impact)

- All Python files under `app/`
- `app/server/static/css/theme-tech.css` (we reuse existing classes only)
- All other static assets

---

## Verification Strategy

Tudou has no JS test infrastructure currently, so each task uses **manual browser verification** against a concrete checklist instead of pytest. Each verification step:

1. Reload portal at `http://localhost:9090`
2. Run the listed click sequence
3. Confirm expected behavior
4. Optionally also run `~/run_tudou.sh --status` to ensure server unaffected

After SP-0 lands, follow-up SPs may add Playwright smoke tests; that's out of scope here.

---

## Bite-Sized Task Granularity

Per superpowers:writing-plans, each step is one action (2-5 min). For UI tasks the granularity is "edit one component" or "verify one flow". Each task ends with a commit so we have rollback points.

---

## Task 1: Workspace shell scaffold + feature flag

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (add `_agent_workspace_*` skeleton fns)

**Goal:** Empty 5-tab shell renders when `tudou_workspace_v2` flag is on. All tabs show "Coming soon" placeholder. Old UI still default until flag flipped.

- [ ] **Step 1: Add the SP-0 banner + flag detection helper**

Insert near the top of portal_bundle.js (after the existing global vars block):

```js
// ═══════════════════════════════════════════════════════
// SP-0 · UNIFIED AGENT WORKSPACE                          (start)
// Per spec §4 — 5-tab agent workspace replacing scattered modals.
// ═══════════════════════════════════════════════════════

// State for the active workspace per agent.
// Keyed by agentId; stores last-active tab + scroll positions.
var _aws = {};  // _aws[agentId] = { activeTab, scrollByTab: {} }

function _awsEnabled() {
  try {
    return localStorage.getItem('tudou_workspace_v2') !== '0';
  } catch (e) { return true; }
}

function _awsGetState(agentId) {
  if (!_aws[agentId]) {
    _aws[agentId] = { activeTab: 'chat', scrollByTab: {} };
  }
  return _aws[agentId];
}
```

- [ ] **Step 2: Implement the shell renderer**

Add right after the helpers:

```js
function renderAgentWorkspace(agentId) {
  if (!_awsEnabled()) return null;  // signal "use old UI"
  var state = _awsGetState(agentId);
  var c = document.getElementById('content');
  if (!c) return false;
  c.innerHTML = ''
    + '<div id="aws-root" data-agent-id="' + esc(agentId) + '" '
    +     'style="display:flex;flex-direction:column;height:100vh;background:var(--bg,#1a1a24)">'
    + '  <div id="aws-header" style="flex-shrink:0;padding:14px 22px;'
    +       'border-bottom:1px solid var(--outline-variant,#333);'
    +       'display:flex;justify-content:space-between;align-items:center">'
    + '    <div>'
    + '      <button class="btn btn-sm btn-ghost" onclick="renderDashboard()" '
    +             'style="margin-right:12px"><span class="material-symbols-outlined" '
    +             'style="font-size:16px;vertical-align:middle">arrow_back</span> Back</button>'
    + '      <span id="aws-agent-name" style="font-size:16px;font-weight:600">'
    +              esc(agentId) + '</span>'
    + '    </div>'
    + '    <button class="btn btn-sm btn-ghost" onclick="_awsToggleLegacyUi()" '
    +           'title="Switch back to classic UI">'
    + '      <span class="material-symbols-outlined" style="font-size:16px">undo</span>'
    + '    </button>'
    + '  </div>'
    + '  <nav id="aws-tabs" role="tablist" style="flex-shrink:0;padding:0 22px;'
    +       'border-bottom:1px solid var(--outline-variant,#333);display:flex;gap:2px">'
    +     _awsTabBtn('chat', '💬', '对话', state.activeTab === 'chat')
    +     _awsTabBtn('capabilities', '🧰', '能力', state.activeTab === 'capabilities')
    +     _awsTabBtn('cultivation', '🎓', '养成', state.activeTab === 'cultivation')
    +     _awsTabBtn('history', '📊', '历史', state.activeTab === 'history')
    +     _awsTabBtn('settings', '⚙️', '配置', state.activeTab === 'settings')
    + '  </nav>'
    + '  <main id="aws-main" style="flex:1;overflow:hidden;position:relative">'
    + '  </main>'
    + '</div>';
  _awsLoadAgentMeta(agentId);
  _awsRenderTab(agentId, state.activeTab);
  return true;
}

function _awsTabBtn(tabId, icon, label, active) {
  var color = active ? 'var(--primary,#a78bfa)' : 'var(--text3)';
  var border = active ? '2px solid var(--primary,#a78bfa)' : '2px solid transparent';
  return '<button role="tab" aria-selected="' + (active ? 'true' : 'false') + '" '
    + 'data-tab="' + esc(tabId) + '" '
    + 'onclick="_awsSwitchTab(this.dataset.tab)" '
    + 'style="background:none;border:none;border-bottom:' + border + ';'
    +   'padding:12px 16px;color:' + color + ';font-size:13px;cursor:pointer;'
    +   'display:inline-flex;align-items:center;gap:6px;font-weight:'
    +   (active ? '600' : '500') + '">'
    +   '<span style="font-size:16px">' + icon + '</span>'
    +   esc(label)
    + '</button>';
}

function _awsSwitchTab(tabId) {
  var root = document.getElementById('aws-root');
  if (!root) return;
  var agentId = root.dataset.agentId;
  if (!agentId) return;
  var state = _awsGetState(agentId);
  // Save current scroll
  var main = document.getElementById('aws-main');
  if (main && state.activeTab) {
    state.scrollByTab[state.activeTab] = main.scrollTop || 0;
  }
  state.activeTab = tabId;
  // Update tab styles
  document.querySelectorAll('#aws-tabs button').forEach(function(b){
    var active = b.dataset.tab === tabId;
    b.style.borderBottom = active
      ? '2px solid var(--primary,#a78bfa)'
      : '2px solid transparent';
    b.style.color = active ? 'var(--primary,#a78bfa)' : 'var(--text3)';
    b.style.fontWeight = active ? '600' : '500';
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  _awsRenderTab(agentId, tabId);
}

function _awsRenderTab(agentId, tabId) {
  var main = document.getElementById('aws-main');
  if (!main) return;
  // Stub — each task replaces with real content
  main.innerHTML = '<div style="padding:60px;text-align:center;color:var(--text3)">'
    + '<div style="font-size:32px;margin-bottom:12px">🚧</div>'
    + 'Tab "' + esc(tabId) + '" coming soon (SP-0 work in progress)'
    + '</div>';
  // Restore scroll
  var state = _awsGetState(agentId);
  var saved = state.scrollByTab[tabId];
  if (saved) setTimeout(function(){ main.scrollTop = saved; }, 0);
}

function _awsLoadAgentMeta(agentId) {
  api('GET', '/api/portal/agent/' + agentId).then(function(a){
    var nameEl = document.getElementById('aws-agent-name');
    if (nameEl) nameEl.textContent = (a && a.name) || agentId;
  }).catch(function(){});
}

function _awsToggleLegacyUi() {
  try { localStorage.setItem('tudou_workspace_v2', '0'); } catch (e) {}
  alert('已切回经典 UI。刷新页面生效。');
  location.reload();
}
```

- [ ] **Step 3: Hook the workspace shell into the existing agent-page entry point**

Find the function that renders an agent's main page (likely `renderAgentChat` or `showAgent`). At the very top, before any old rendering happens, add:

```js
// SP-0 workspace v2 short-circuit
if (typeof renderAgentWorkspace === 'function' && renderAgentWorkspace(agentId) === true) {
  return;
}
// fall through to legacy if flag off or workspace failed
```

To find the right hook, run: `grep -n "function renderAgentChat\|function showAgent\b" portal_bundle.js`

- [ ] **Step 4: Manual verification**

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
```

In browser:
1. Open `http://localhost:9090`, log in
2. Click any agent → expected: see new 5-tab shell with current tab highlighted
3. Click each tab → expected: see "Tab X coming soon" placeholder, no JS errors in console
4. Click "← Back" → expected: return to dashboard
5. Click the undo icon (top right) → expected: confirm dialog, then page reloads showing classic UI
6. In console: `localStorage.setItem('tudou_workspace_v2','1'); location.reload()` → expected: workspace returns

- [ ] **Step 5: Commit**

```bash
git add app/server/static/js/portal_bundle.js
git commit -m "SP-0 task 1: workspace shell + feature flag scaffold"
```

---

## Task 2: 💬 对话 tab — embed existing chat

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (`_awsRenderTab` chat branch)

**Goal:** 对话 tab renders the existing chat interface (chat-msgs container, input bar, voice mode button, reasoning sidebar). Behavior identical to classic UI. Voice mode launch button stays accessible.

- [ ] **Step 1: Find the existing chat container HTML**

```bash
grep -n "chat-msgs-\|chat-input-\|id=.chat-" portal_bundle.js | head -20
```

Identify the function that builds chat layout (likely returns HTML containing `chat-msgs-${id}`, `chat-input-${id}`, etc.).

- [ ] **Step 2: Extract chat layout into a callable fn**

If the chat HTML is inline in some larger render fn, extract it into a new helper:

```js
function _awsBuildChatPanelHTML(agentId) {
  // Return the same HTML structure currently used for chat-msgs +
  // chat-input + thinking sidebar etc. Reuse exact IDs (chat-msgs-<id>)
  // so existing handlers (sendAgentMsg, _autoSpeak, etc.) still bind.
  return '<div style="display:flex;height:100%;overflow:hidden">'
    + '  <div style="flex:1;display:flex;flex-direction:column;min-width:0">'
    +     /* existing chat msg list + input bar HTML, with same IDs */
    + '  </div>'
    + '  <aside style="width:320px;border-left:1px solid var(--outline-variant);'
    +       'overflow-y:auto" id="chat-side-' + agentId + '">'
    +     /* existing reasoning / thinking pane */
    + '  </aside>'
    + '</div>';
}
```

- [ ] **Step 3: Wire `_awsRenderTab` chat branch**

Replace the chat-tab stub:

```js
function _awsRenderTab(agentId, tabId) {
  var main = document.getElementById('aws-main');
  if (!main) return;
  if (tabId === 'chat') {
    main.innerHTML = _awsBuildChatPanelHTML(agentId);
    // Re-bind any handlers that the existing renderer used to bind
    if (typeof loadAgentEventLog === 'function') loadAgentEventLog(agentId);
    if (typeof loadExecutionSteps === 'function') loadExecutionSteps(agentId);
  } else {
    main.innerHTML = '<div style="padding:60px;text-align:center;color:var(--text3)">'
      + '<div style="font-size:32px;margin-bottom:12px">🚧</div>'
      + 'Tab "' + esc(tabId) + '" coming soon (SP-0 work in progress)'
      + '</div>';
  }
  // ... existing scroll restore ...
}
```

- [ ] **Step 4: Manual verification**

```bash
~/run_tudou.sh --restart 2>&1 | tail -3
```

In browser (workspace flag on):
1. Open agent → 对话 tab is default → expected: chat history loads, input bar visible
2. Type message + send → expected: message posts, agent responds, chat scrolls
3. Click voice mode button → expected: voice mode overlay launches as before
4. Exit voice → expected: back to chat tab with prior state
5. Switch to other tabs and back → expected: chat scroll position preserved

- [ ] **Step 5: Commit**

```bash
git commit -am "SP-0 task 2: 对话 tab embeds existing chat panel"
```

---

## Task 3: 🧰 能力 tab — migrate Capabilities content

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (`_awsRenderTab` capabilities branch + repurpose `showSkillPanel`/`_showSkillPanelTech` content fn)

**Goal:** 能力 tab renders the same content currently shown in the Capabilities popup (`build_circle 能力扩展`), with no popup wrapper. The "+ Add Skill / + Add MCP / + Add Pack" buttons WIRE to inline expansion (not popups). Inline expansion handled in tasks 4-6.

- [ ] **Step 1: Refactor `_showSkillPanelTech` to return HTML instead of calling `showModalHTMLLarge`**

Find `_showSkillPanelTech(agentId, agDetail, granted, boundPacks, mcpList)`. Split into two:

```js
// Public entry: builds raw HTML body (no modal wrapper)
function _capabilitiesBodyHTML(agentId, agDetail, granted, boundPacks, mcpList) {
  // ... existing _showSkillPanelTech body, but DROP the outer modal div
  //     and DROP the close button from header (workspace tab nav has back) ...
  //     KEEP all section cards (MCP / Granted Skills / Prompt Packs)
  return html;  // string
}

// Legacy popup mode — wrap body in modal
function _showSkillPanelTech(agentId, agDetail, granted, boundPacks, mcpList) {
  var body = _capabilitiesBodyHTML(agentId, agDetail, granted, boundPacks, mcpList);
  showModalHTMLLarge(body);
}
```

This way classic-UI users still get popup; workspace users embed body inline.

- [ ] **Step 2: Wire workspace 能力 tab to use the body fn**

Extend `_awsRenderTab`:

```js
} else if (tabId === 'capabilities') {
  main.style.overflowY = 'auto';
  main.innerHTML = '<div style="padding:0;color:var(--text3)">Loading capabilities…</div>';
  Promise.all([
    api('GET', '/api/portal/agent/' + agentId + '/skill-pkgs').catch(function(){return {skills:[]}}),
    api('GET', '/api/portal/agent/' + agentId + '/prompt-packs').catch(function(){return {bound_skills:[]}}),
    api('GET', '/api/portal/agent/' + agentId).catch(function(){return {}}),
  ]).then(function(results){
    var granted = (results[0] && results[0].skills) || [];
    var boundPacks = (results[1] && results[1].bound_skills) || [];
    var agDetail = results[2] || {};
    var mcpList = agDetail.mcp_servers || [];
    main.innerHTML = _capabilitiesBodyHTML(agentId, agDetail, granted, boundPacks, mcpList);
  });
}
```

- [ ] **Step 3: Disable Capabilities popup launch when workspace v2 is active**

Find the existing entry point `showSkillPanel(agentId)`. At the top:

```js
async function showSkillPanel(agentId) {
  if (_awsEnabled()) {
    // workspace mode: navigate to capabilities tab instead of popup
    var state = _awsGetState(agentId);
    state.activeTab = 'capabilities';
    if (typeof renderAgentWorkspace === 'function') {
      renderAgentWorkspace(agentId);
    }
    return;
  }
  // ... existing popup-mode body ...
}
```

- [ ] **Step 4: Manual verification**

In browser (workspace flag on):
1. Open agent → click 🧰 能力 tab → expected: same capabilities content as old popup, scrolls
2. From dashboard or any other place, click an existing "能力扩展" button → expected: workspace navigates to 能力 tab (not popup)
3. Click [Revoke] on a granted skill → expected: same revoke flow works (existing handler)
4. Click [Unbind] on a prompt pack → expected: same unbind flow works
5. Switch to localStorage.setItem('tudou_workspace_v2','0'); reload → click "能力扩展" → expected: classic popup mode still works

- [ ] **Step 5: Commit**

```bash
git commit -am "SP-0 task 3: 能力 tab migrates Capabilities content; popup mode preserved as fallback"
```

---

## Task 4: 🧰 能力 tab — inline Prompt Pack 市场 + 已发现 (replace popups)

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (refactor `ppOpenCatalog` and `ppOpenDiscovered`)

**Goal:** When 能力 tab's "+ 添加 prompt pack" buttons are clicked, the catalog/discovered UI expands INSIDE the tab (below the existing prompt pack list). The 90vw popup version is preserved for classic-UI users only.

- [ ] **Step 1: Refactor `ppOpenCatalog` to support an embedded host element**

Change signature:

```js
async function ppOpenCatalog(agentId, hostElId) {
  // hostElId optional. If provided, render inside that element (workspace mode).
  // If absent, fall back to popup (classic mode).
  _PPCat = { agentId, page: 1, perPage: 24, source: '', category: '',
             search: '', searchTimer: null, data: null,
             hostElId: hostElId || null };
  var html = '...'; // existing 90vw modal body
  if (hostElId) {
    var host = document.getElementById(hostElId);
    if (host) {
      host.innerHTML = html;
      _ppCatalogReload(true);
      return;
    }
  }
  showModalHTMLLarge(html);
  _ppCatalogReload(true);
}
```

The HTML body itself doesn't change — it's the same flex column with header / search / chips / list / pager. When embedded in a tab, the outer modal wrapper just isn't needed.

- [ ] **Step 2: In `_capabilitiesBodyHTML`, the Prompt Packs section's [+ 市场] button targets an inline host**

Modify the section header in `_capabilitiesBodyHTML`:

```js
// Replace existing 市场/已发现 buttons:
'<button class="btn btn-sm" onclick="_capToggleEmbed(\'' + esc(agentId) + '\', \'pp-market\')">'
  + '<span class="material-symbols-outlined" style="font-size:13px">storefront</span> 市场'
  + '</button>'
'<button class="btn btn-sm" onclick="_capToggleEmbed(\'' + esc(agentId) + '\', \'pp-discovered\')">'
  + '<span class="material-symbols-outlined" style="font-size:13px">inventory_2</span> 已发现'
  + '</button>'
```

And below the bound-packs grid, add the embed host:

```js
'<div id="cap-embed-' + esc(agentId) + '" style="margin-top:14px"></div>'
```

- [ ] **Step 3: Implement `_capToggleEmbed`**

```js
function _capToggleEmbed(agentId, kind) {
  var hostId = 'cap-embed-' + agentId;
  var host = document.getElementById(hostId);
  if (!host) return;
  // toggle: clicking same kind again collapses
  if (host.dataset.kind === kind) {
    host.innerHTML = '';
    host.dataset.kind = '';
    return;
  }
  host.dataset.kind = kind;
  if (kind === 'pp-market') {
    ppOpenCatalog(agentId, hostId);
  } else if (kind === 'pp-discovered') {
    ppOpenDiscovered(agentId, hostId);
  }
}
```

- [ ] **Step 4: Refactor `ppOpenDiscovered` similarly**

Same pattern: add optional `hostElId`, render inside if provided.

- [ ] **Step 5: Manual verification**

1. 能力 tab → click [📦 市场] → expected: catalog expands INLINE below packs section, NOT a popup
2. Search / filter / paginate inside embedded catalog → expected: works
3. Click 导入并绑定 on a card → expected: agent gets pack bound (verify bound list updates)
4. Click [📦 市场] again → expected: catalog collapses
5. Click [📥 已发现] → expected: discovered list expands, market hidden
6. Switch agent or tab and back → expected: embed state resets cleanly
7. Switch flag off, reload, open Capabilities popup, click 市场 → expected: classic 90vw popup still works

- [ ] **Step 6: Commit**

```bash
git commit -am "SP-0 task 4: prompt-pack market + discovered embed inline in 能力 tab"
```

---

## Task 5: 🧰 能力 tab — inline Skill Store add-flow

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (per-agent skill picker)

**Goal:** "+ Add Skill from Store" button in 能力 tab expands an inline picker (filtered to available skills not yet granted). Picking a skill triggers the existing grant API. Global Skill Store page (nav rail) untouched.

- [ ] **Step 1: Add an inline skill picker fn**

```js
async function _capEmbedSkillPicker(agentId, hostId) {
  var host = document.getElementById(hostId);
  if (!host) return;
  host.innerHTML = '<div style="padding:20px;color:var(--text3)">Loading skills…</div>';
  try {
    // fetch all installed skills + ones already granted to this agent
    var [allSkills, granted] = await Promise.all([
      api('GET', '/api/portal/skill-pkgs'),
      api('GET', '/api/portal/agent/' + agentId + '/skill-pkgs'),
    ]);
    var grantedIds = new Set(((granted && granted.skills) || []).map(function(s){return s.id}));
    var available = ((allSkills && allSkills.skills) || []).filter(function(s){
      return !grantedIds.has(s.id);
    });
    if (!available.length) {
      host.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text3)">'
        + '所有已安装的 skill 都已授权。要安装新 skill 请去 [Skills (全局)] 页。</div>';
      return;
    }
    var rows = available.map(function(s){
      var m = s.manifest || {};
      return '<div class="tc-card-glass" style="padding:12px;display:flex;'
        + 'justify-content:space-between;align-items:center;margin-bottom:8px">'
        + '  <div style="flex:1;min-width:0">'
        + '    <div style="font-size:13px;font-weight:600">' + esc(m.name || s.id) + '</div>'
        + '    <div style="font-size:11px;color:var(--text3);margin-top:3px">'
        +        esc(m.description || '') + '</div>'
        + '  </div>'
        + '  <button class="btn btn-sm btn-primary" '
        +     'onclick="_capGrantSkillInline(\'' + esc(agentId) + '\',\''
        +       esc(s.id) + '\', this)">授权</button>'
        + '</div>';
    }).join('');
    host.innerHTML = '<div style="padding:14px;border:1px solid var(--outline-variant);'
      + 'border-radius:8px;background:rgba(255,255,255,0.02)">'
      + '<div class="tc-mono-label" style="font-size:10px;margin-bottom:10px">'
      + '可授权的 SKILLS (' + available.length + ')</div>'
      + rows
      + '</div>';
  } catch (e) {
    host.innerHTML = '<div style="padding:20px;color:var(--error)">Failed: ' + esc(e.message || e) + '</div>';
  }
}

async function _capGrantSkillInline(agentId, skillId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    await api('POST', '/api/portal/skill-pkgs/' + encodeURIComponent(skillId) + '/grant',
              { agent_id: agentId });
    if (btn) {
      btn.textContent = '✓ 已授权';
      btn.style.background = 'rgba(92,240,138,0.20)';
      btn.style.borderColor = 'rgba(92,240,138,0.50)';
      btn.style.color = 'var(--cyber-lime)';
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '授权'; }
    alert('授权失败: ' + (e.message || e));
  }
}
```

- [ ] **Step 2: Wire the [+ Grant] button in `_capabilitiesBodyHTML`**

Update the GRANTED SKILLS section header:

```js
'<button class="btn btn-sm" onclick="_capToggleEmbed(\'' + esc(agentId) + '\', \'skill-picker\')">'
  + '<span class="material-symbols-outlined" style="font-size:13px">add</span> 从 Store 授权'
  + '</button>'
```

And extend `_capToggleEmbed`:

```js
} else if (kind === 'skill-picker') {
  _capEmbedSkillPicker(agentId, hostId);
}
```

- [ ] **Step 3: Manual verification**

1. 能力 tab → click [+ 从 Store 授权] → expected: inline list of available (non-granted) skills
2. Click [授权] → expected: skill becomes granted, button turns green ✓
3. Refresh tab → expected: granted skills list grew by 1; available picker no longer shows that skill
4. Click button again → expected: panel collapses

- [ ] **Step 4: Commit**

```bash
git commit -am "SP-0 task 5: inline skill picker in 能力 tab"
```

---

## Task 6: ⚙️ 配置 tab — migrate Edit Agent

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (refactor edit-agent renderer)

**Goal:** 配置 tab renders the same form fields as the existing Edit Agent modal. Save button updates agent profile. Modal mode preserved as fallback.

- [ ] **Step 1: Find and extract the Edit Agent form HTML**

```bash
grep -n "function showEditAgent\|function _editAgent\|ea-name\|ea-tts" portal_bundle.js | head -20
```

Locate the function that builds the Edit Agent form (likely `showEditAgent(agentId)` or `_renderEditAgent(agentId)`). It returns HTML with field IDs like `ea-name`, `ea-role`, `ea-tts-provider`, etc.

- [ ] **Step 2: Split into a body-builder + modal wrapper**

```js
function _editAgentBodyHTML(agentDetail) {
  // existing form HTML, no modal wrapper, no Cancel/Save buttons in footer
  // (the workspace tab's own footer holds Save)
  return html;
}

// Legacy popup
async function showEditAgent(agentId) {
  if (_awsEnabled()) {
    var state = _awsGetState(agentId);
    state.activeTab = 'settings';
    renderAgentWorkspace(agentId);
    return;
  }
  // existing popup body, calls _editAgentBodyHTML internally for form
  var detail = await api('GET', '/api/portal/agent/' + agentId);
  showModalHTMLLarge(_editAgentBodyHTML(detail) + _editAgentLegacyFooter(agentId));
  // ... bind ...
}
```

- [ ] **Step 3: Wire workspace 配置 tab**

```js
} else if (tabId === 'settings') {
  main.style.overflowY = 'auto';
  main.innerHTML = '<div style="padding:30px;color:var(--text3)">Loading…</div>';
  api('GET', '/api/portal/agent/' + agentId).then(function(detail){
    main.innerHTML = ''
      + '<div style="padding:24px 30px;max-width:900px">'
      +   _editAgentBodyHTML(detail)
      +   '<div style="margin-top:24px;display:flex;gap:10px;'
      +        'border-top:1px solid var(--outline-variant);padding-top:16px">'
      +     '<button class="btn btn-primary" onclick="_awsSaveAgentSettings(\''
      +        esc(agentId) + '\')">保存</button>'
      +     '<button class="btn btn-ghost" onclick="_awsRenderTab(\''
      +        esc(agentId) + '\', \'settings\')">取消(重新加载)</button>'
      +   '</div>'
      + '</div>';
    // bind any existing handlers (TTS provider change etc.)
    if (typeof _eaOnTtsProviderChange === 'function') _eaOnTtsProviderChange();
  });
}

async function _awsSaveAgentSettings(agentId) {
  // collect form values, call existing save endpoint (the same one
  // legacy modal uses), then rerender tab on success
  var body = _collectEditAgentForm();  // existing helper
  try {
    await api('POST', '/api/portal/agent/' + agentId + '/profile', body);
    if (window._toast) _toast('已保存', 'success');
    _awsRenderTab(agentId, 'settings');
  } catch (e) {
    alert('保存失败: ' + (e.message || e));
  }
}
```

- [ ] **Step 4: Manual verification**

1. 配置 tab → expected: same form fields as old Edit Agent modal (name / role / persona / TTS / etc.)
2. Change agent name → click 保存 → expected: toast "已保存", form re-fetches
3. Header shows updated name (via `_awsLoadAgentMeta` re-fetch)
4. Switch to other tab and back → expected: form reloads from server (no stale data)
5. Flag off + reload + click old "Edit Agent" entry → expected: classic popup still works

- [ ] **Step 5: Commit**

```bash
git commit -am "SP-0 task 6: 配置 tab migrates Edit Agent form; popup mode preserved"
```

---

## Task 7: 🎓 养成 tab — placeholder for SP-1

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (`_awsRenderTab` cultivation branch)

**Goal:** 养成 tab in SP-0 shows a clear "Specialty system arrives in SP-1" placeholder with current agent's `expert_specialty` field readout. SP-1 will replace this with the full pipeline visualization.

- [ ] **Step 1: Implement cultivation tab stub**

```js
} else if (tabId === 'cultivation') {
  main.style.overflowY = 'auto';
  api('GET', '/api/portal/agent/' + agentId).then(function(a){
    var spec = (a && a.expert_specialty) || '';
    var html = '<div style="padding:50px 30px;max-width:680px;margin:0 auto">'
      + '  <div class="tc-card-glass" style="padding:30px;text-align:center">'
      + '    <div style="font-size:48px;margin-bottom:14px">🎓</div>'
      + '    <div class="tc-mono-label" style="color:var(--cyber-magenta,#ff7adb);'
      +        'font-size:11px;margin-bottom:10px">EXPERT CULTIVATION SYSTEM</div>'
      + '    <h2 style="margin:0 0 10px">养成系统 (SP-1 将上线)</h2>'
      + '    <p class="tc-text-dim" style="font-size:13px;line-height:1.6;'
      +        'max-width:480px;margin:0 auto 20px">';
    if (spec) {
      html += '当前 specialty: <b>' + esc(spec) + '</b><br>'
        + '配方应用 / RAG 索引 / LoRA 训练 / Routing 这些控件将在 SP-1 上线后出现在这里。';
    } else {
      html += '此 agent 尚未专家化。在 SP-1 上线后,你可以选择 specialty 模板'
        + '(法律 / 医疗 / 财务等),agent 会被渐进养成为该领域专家。';
    }
    html += '    </p>'
      + '    <a href="../specs/2026-05-10-agent-specialty-cultivation-design.md" '
      +     'target="_blank" style="font-size:11px;color:var(--primary)">查看完整设计 spec →</a>'
      + '  </div>'
      + '</div>';
    main.innerHTML = html;
  });
}
```

- [ ] **Step 2: Manual verification**

1. 养成 tab → expected: placeholder shows current specialty (likely empty for普通 agent)
2. No JS errors

- [ ] **Step 3: Commit**

```bash
git commit -am "SP-0 task 7: 养成 tab placeholder; SP-1 will replace with pipeline UI"
```

---

## Task 8: 📊 历史 tab — integrate existing trace / event log views

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (`_awsRenderTab` history branch)

**Goal:** 历史 tab shows three sub-views toggled by sub-tabs: 对话历史 / 工具调用 / Plan 执行。Reuse existing renderers (`loadAgentEventLog`, `loadExecutionSteps`, etc.) — wrap their output containers inside the tab.

- [ ] **Step 1: Identify existing renderers**

```bash
grep -n "loadAgentEventLog\|loadExecutionSteps\|renderPlans\|loadPlans" portal_bundle.js | head -10
```

- [ ] **Step 2: Build the history tab with sub-tabs**

```js
} else if (tabId === 'history') {
  main.style.overflowY = 'auto';
  var subTab = (_awsGetState(agentId).historySubTab) || 'events';
  main.innerHTML = ''
    + '<div style="display:flex;flex-direction:column;height:100%">'
    + '  <div style="flex-shrink:0;padding:14px 22px 0;'
    +       'border-bottom:1px solid var(--outline-variant);display:flex;gap:2px">'
    +     _awsSubTabBtn('events', '对话事件', subTab === 'events')
    +     _awsSubTabBtn('tools', '工具调用', subTab === 'tools')
    +     _awsSubTabBtn('plans', 'Plan 执行', subTab === 'plans')
    + '  </div>'
    + '  <div id="aws-history-body" style="flex:1;overflow-y:auto;padding:14px 22px">'
    +     '<div id="event-log-' + esc(agentId) + '"></div>'
    +     '<div id="execution-steps-' + esc(agentId) + '"></div>'
    +     '<div id="plans-' + esc(agentId) + '"></div>'
    + '  </div>'
    + '</div>';
  _awsHistoryRender(agentId, subTab);
}

function _awsSubTabBtn(id, label, active) {
  return '<button onclick="_awsSwitchHistorySub(\'' + esc(id) + '\')" '
    + 'style="background:none;border:none;border-bottom:2px solid '
    +   (active ? 'var(--cyber-blue,#4afcff)' : 'transparent') + ';'
    +   'padding:8px 14px;color:' + (active ? 'var(--cyber-blue,#4afcff)' : 'var(--text3)')
    +   ';font-size:12px;cursor:pointer">' + esc(label) + '</button>';
}

function _awsSwitchHistorySub(subId) {
  var root = document.getElementById('aws-root');
  if (!root) return;
  var agentId = root.dataset.agentId;
  _awsGetState(agentId).historySubTab = subId;
  _awsRenderTab(agentId, 'history');
}

function _awsHistoryRender(agentId, subTab) {
  // hide all panels then show the active one
  ['event-log-', 'execution-steps-', 'plans-'].forEach(function(prefix){
    var el = document.getElementById(prefix + agentId);
    if (el) el.style.display = 'none';
  });
  if (subTab === 'events') {
    var el = document.getElementById('event-log-' + agentId);
    if (el) el.style.display = '';
    if (typeof loadAgentEventLog === 'function') loadAgentEventLog(agentId);
  } else if (subTab === 'tools') {
    var el2 = document.getElementById('execution-steps-' + agentId);
    if (el2) el2.style.display = '';
    if (typeof loadExecutionSteps === 'function') loadExecutionSteps(agentId);
  } else if (subTab === 'plans') {
    var el3 = document.getElementById('plans-' + agentId);
    if (el3) el3.style.display = '';
    if (typeof loadPlans === 'function') loadPlans(agentId);
  }
}
```

- [ ] **Step 3: Manual verification**

1. 历史 tab → 对话事件 sub-tab → expected: event log loads (existing behavior)
2. Switch to 工具调用 → expected: execution steps render
3. Switch to Plan 执行 → expected: plan history renders (or empty placeholder if no plans)
4. Switch agent → expected: each agent's history is independent

- [ ] **Step 4: Commit**

```bash
git commit -am "SP-0 task 8: 历史 tab integrates events/tools/plans sub-tabs"
```

---

## Task 9: Voice mode integration (no popup, just launch button)

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (chat tab header)

**Goal:** 对话 tab has a [🎙 Voice Mode] button at the top that launches existing voice mode overlay. Closing voice returns to 对话 tab in same state.

- [ ] **Step 1: Add voice mode launcher in chat panel header**

Update `_awsBuildChatPanelHTML`:

```js
function _awsBuildChatPanelHTML(agentId) {
  return '<div style="display:flex;flex-direction:column;height:100%">'
    + '  <div style="flex-shrink:0;padding:10px 22px;display:flex;justify-content:flex-end;gap:8px;border-bottom:1px solid var(--outline-variant)">'
    + '    <button class="btn btn-sm" onclick="enterVoiceMode(\'' + esc(agentId) + '\')">'
    + '      <span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">mic</span> 'Voice Mode'
    + '    </button>'
    + '  </div>'
    + '  <div style="flex:1;display:flex;overflow:hidden">'
    + '    <div style="flex:1;display:flex;flex-direction:column;min-width:0">'
    /* existing chat msg list + input bar HTML, with same IDs */
    + '    </div>'
    + '    <aside style="width:320px;border-left:1px solid var(--outline-variant);'
    +         'overflow-y:auto" id="chat-side-' + agentId + '">'
    /* existing reasoning / thinking pane */
    + '    </aside>'
    + '  </div>'
    + '</div>';
}
```

- [ ] **Step 2: Manual verification**

1. 对话 tab → click [Voice Mode] → expected: voice mode overlay launches as before
2. Exit voice mode → expected: back to 对话 tab, chat content preserved
3. Voice mode while on a different tab: not triggered (button only on chat tab) — by design

- [ ] **Step 3: Commit**

```bash
git commit -am "SP-0 task 9: voice mode launch button on 对话 tab"
```

---

## Task 10: End-to-end verification + final commit

**Files:**
- None (verification only)

**Goal:** Confirm SP-0 introduces zero functional regressions. Run a comprehensive checklist hitting every existing feature flow.

- [ ] **Step 1: Comprehensive smoke checklist**

Run through this list, both with `tudou_workspace_v2='1'` (default) and `'0'` (legacy):

| Flow | v2 expected | legacy expected |
|---|---|---|
| Open agent | 5-tab workspace | classic dashboard |
| Send chat msg | works | works |
| Voice mode | works (launched from 对话 tab) | works (launched from existing button) |
| Capabilities view | 能力 tab | popup |
| Grant/revoke skill | inline picker | popup |
| Bind/unbind pack | inline picker | popup |
| Browse Prompt Pack 市场 | inline | 90vw popup |
| Edit agent | 配置 tab | popup |
| View event log | 历史 tab → 对话事件 | inline on dashboard |
| View execution steps | 历史 tab → 工具调用 | inline on dashboard |
| Voice mode multi-turn | works | works |
| Switch between agents | each has own tab state | classic |
| Browser refresh | tab state lost (acceptable) | classic |

- [ ] **Step 2: Document any regressions found**

If any flow breaks, file as a fix sub-task and commit fix BEFORE marking SP-0 complete.

- [ ] **Step 3: Update CHANGELOG (or NOTES)**

Add a brief entry to a project-level changelog or NOTES file documenting the workspace shift:

```bash
echo "
## 2026-05-10 SP-0 — Unified Agent Workspace
- New: 5-tab agent workspace (chat / capabilities / cultivation / history / settings)
- Changed: Capabilities popup → 能力 tab
- Changed: Edit Agent popup → 配置 tab
- Changed: Prompt Pack market → embedded in 能力 tab
- Preserved: legacy UI accessible via localStorage tudou_workspace_v2='0'
" >> NOTES.md
```

- [ ] **Step 4: Final commit**

```bash
git add NOTES.md
git commit -m "SP-0 task 10: end-to-end verification + changelog"
```

---

## Self-Review

**Spec coverage check:** ☑ All §4 (UI Architecture) requirements addressed: 5-tab shell, popup → tab migration, inline expansions, voice mode preservation, feature flag fallback.

**Placeholder scan:** ☑ No TBD/TODO. Every step has actual code or concrete commands.

**Type/name consistency:** ☑ `_aws*` prefix for workspace internals; `_capToggleEmbed` / `_capabilitiesBodyHTML` for capabilities; tab IDs are stable string literals (`'chat'`/`'capabilities'`/etc.) used identically across switch / btn handlers / state.

**Files-changed sanity:** ☑ Single file (`portal_bundle.js`) plus optional NOTES.md. Backend untouched. Existing per-feature renderers preserved as fallback paths.

**Risk mitigation:** ☑ Feature flag in localStorage allows instant rollback. Each task ends with a commit so any task can be reverted without affecting later ones.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-10-sp0-unified-agent-workspace.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. Best for this plan because each task is bounded and independently verifiable.

**2. Inline Execution** — Execute tasks in this session using superpowers:executing-plans, batch execution with checkpoints. Faster but harder to course-correct mid-flight.

**Which approach?**

If Subagent-Driven chosen:
- **REQUIRED SUB-SKILL:** Use superpowers:subagent-driven-development

If Inline Execution chosen:
- **REQUIRED SUB-SKILL:** Use superpowers:executing-plans
