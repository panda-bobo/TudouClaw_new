// TudouClaw desktop floating-agent widget.
//
// Polls the local FastAPI on 127.0.0.1:9090 every 5 s for agents whose
// admin has flipped on the desktop toggle. Click avatar → expand card.
// Card shows: name, status, persona (soul_md), and a chat input.
//
// Auth: the /agents/desktop endpoint is loopback-only and skips JWT.
// /agent/{id}/chat still requires JWT — sending messages will fail
// with 401 until phase 3 adds a token-pickup flow. The card surfaces
// that error inline so you can see it's wired.

const API_BASE = 'http://127.0.0.1:9090/api/portal';
const STATIC_BASE = 'http://127.0.0.1:9090/static';
const API_HOST = 'http://127.0.0.1:9090';
const POLL_MS = 5000;
const PERSONA_MAX_CHARS = 320;

// Phase 4: each window is bound to one agent via ?agent_id=<id>.
// Rust supervisor spawns one window per enabled agent. Empty string
// means legacy single-window mode (this branch is dormant when the
// Rust supervisor is active — the keepalive "main" window stays
// hidden — but keeps the file usable in standalone testing).
const URL_AGENT_ID = (() => {
  try { return new URLSearchParams(location.search).get('agent_id') || ''; }
  catch (_) { return ''; }
})();

let agents = [];
let currentAgent = null;
let dragMoved = false;
let dragStartX = 0, dragStartY = 0;

const $ = (sel) => document.querySelector(sel);
const avatar = $('#avatar');
const card = $('#card');
const picker = $('#agent-picker');
const statusDot = $('#status-dot');
const statusPill = $('#agent-status');

// ── Drag-vs-click detection ───────────────────────
// data-tauri-drag-region triggers OS drag, but we also want a click to
// expand the card. Track mousedown→up movement; treat as click only if
// the cursor barely moved.
avatar.addEventListener('mousedown', (e) => {
  dragMoved = false;
  dragStartX = e.screenX; dragStartY = e.screenY;
});
avatar.addEventListener('mousemove', (e) => {
  if (Math.hypot(e.screenX - dragStartX, e.screenY - dragStartY) > 4) {
    dragMoved = true;
  }
});
avatar.addEventListener('mouseup', () => {
  if (!dragMoved) toggleCard();
});

$('#close-card').addEventListener('click', toggleCard);
$('#chat-send').addEventListener('click', sendChat);
$('#chat-input').addEventListener('keypress', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
picker.addEventListener('change', () => {
  const id = picker.value;
  const a = agents.find((x) => x.id === id);
  if (a) { currentAgent = a; renderAgent(); clearChat(); }
});

// ── Top-bar wiring ──────────────────────────────────
$('#btn-pin').addEventListener('click', _togglePin);
$('#btn-new-chat').addEventListener('click', () => {
  clearChat();
  appendChat('sys', '已清空对话');
});

// ── Quick-action chips (preset templates) ───────────
// Templates plug straight into the input box — user can edit before
// sending. Keep these short and obviously editable so the user is
// nudged to fill blanks rather than blindly send the boilerplate.
const TEMPLATES = {
  quick: {
    title: '一句话简答',
    text: '请用一句话回答：',
  },
  write: {
    title: '帮我写作',
    text: '帮我写一段文字：\n- 主题：\n- 长度：约 100 字\n- 风格：\n- 受众：',
  },
  bug: {
    title: '解释这个错误',
    text: '帮我看下这个报错是什么原因、怎么修：\n```\n（贴日志）\n```',
  },
  summary: {
    title: '总结要点',
    text: '帮我总结以下内容的要点（3-5 条 bullet）：\n\n',
  },
  translate: {
    title: '翻译',
    text: '把下面这段翻译成中文（保留专有名词不译）：\n\n',
  },
  outline: {
    title: '列大纲',
    text: '帮我列一份关于「__」的大纲（H1 + H2,3-5 个一级章节）',
  },
};

document.querySelectorAll('.quickbar-chip[data-template]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const key = btn.dataset.template;
    const t = TEMPLATES[key];
    if (!t) return;
    _applyTemplate(t.text);
  });
});

$('#qb-more').addEventListener('click', _toggleTemplateMenu);
$('#qb-attach').addEventListener('click', () => {
  appendChat('sys', '附件功能尚未启用');
});
$('#qb-mic').addEventListener('click', () => {
  appendChat('sys', '语音输入尚未启用');
});

// Click outside to close the template menu.
document.addEventListener('click', (e) => {
  const menu = $('#template-menu');
  if (!menu || menu.classList.contains('hidden')) return;
  if (e.target.closest('#template-menu') || e.target.closest('#qb-more')) return;
  menu.classList.add('hidden');
});

function _applyTemplate(text) {
  const inp = $('#chat-input');
  if (!inp) return;
  inp.value = text;
  inp.focus();
  // Move cursor to end so user can keep typing.
  try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (_) {}
  // Hide menu if visible.
  const menu = $('#template-menu');
  if (menu) menu.classList.add('hidden');
}

function _toggleTemplateMenu() {
  const menu = $('#template-menu');
  if (!menu) return;
  if (menu.classList.contains('hidden')) {
    // Render entries on every open so we can extend later (per-role
    // templates, MRU sorting, etc.) without rebuilding HTML.
    menu.innerHTML = Object.entries(TEMPLATES)
      .map(([key, t]) => (
        `<div class="menu-item" data-tk="${key}">
           <div class="menu-title">${_esc(t.title)}</div>
           <div class="menu-hint">${_esc(t.text.slice(0, 60).replace(/\n/g, ' / '))}…</div>
         </div>`
      )).join('');
    menu.querySelectorAll('.menu-item').forEach((row) => {
      row.addEventListener('click', () => {
        const k = row.dataset.tk;
        if (TEMPLATES[k]) _applyTemplate(TEMPLATES[k].text);
      });
    });
    menu.classList.remove('hidden');
  } else {
    menu.classList.add('hidden');
  }
}

function _esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Pin (always-on-top) toggle ──────────────────────
let _pinOn = true;  // Tauri config defaults alwaysOnTop=true

async function _togglePin() {
  try {
    const T = window.__TAURI__;
    if (!T || !T.window) return;
    const w = T.window.getCurrentWindow ? T.window.getCurrentWindow() : T.window.appWindow;
    if (!w || typeof w.setAlwaysOnTop !== 'function') return;
    _pinOn = !_pinOn;
    await w.setAlwaysOnTop(_pinOn);
    const btn = $('#btn-pin');
    if (btn) {
      btn.classList.toggle('active', _pinOn);
      btn.title = _pinOn ? '已置顶 · 点击关闭' : '未置顶 · 点击置顶';
    }
  } catch (e) {
    console.warn('[floater] pin toggle failed:', e);
  }
}

// ── Agent fetch loop ──────────────────────────────
async function loadAgents() {
  try {
    const res = await fetch(`${API_BASE}/agents/desktop`);
    if (!res.ok) return;
    const data = await res.json();
    let list = Array.isArray(data.agents) ? data.agents : [];
    // Per-agent window mode: filter to just this window's agent.
    if (URL_AGENT_ID) {
      list = list.filter((a) => a.id === URL_AGENT_ID);
    }
    agents = list;
    rebuildPicker();
    if (!currentAgent && agents.length) {
      currentAgent = agents[0];
      renderAgent();
    } else if (currentAgent) {
      const fresh = agents.find((a) => a.id === currentAgent.id);
      if (fresh) { currentAgent = fresh; renderAgent(); }
    }
  } catch (e) {
    // Silent — server likely not running yet
  }
}

function rebuildPicker() {
  if (!picker) return;
  // In per-agent mode there's nothing to pick — the window IS the
  // agent. Hide unconditionally so the card chrome stays clean.
  if (URL_AGENT_ID) { picker.style.display = 'none'; return; }
  const prev = picker.value;
  picker.innerHTML = '';
  for (const a of agents) {
    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = a.name || a.id.slice(0, 6);
    picker.appendChild(opt);
  }
  if (prev && agents.find((a) => a.id === prev)) picker.value = prev;
  picker.style.display = agents.length > 1 ? '' : 'none';
}

// ── Per-agent identity ────────────────────────────
// Deterministic hue from agent.id so the same agent always gets the
// same color. djb2-ish; output 0..359.
function avatarHue(agent) {
  const seed = (agent && (agent.id || agent.name)) || 'x';
  let h = 5381;
  for (let i = 0; i < seed.length; i++) {
    h = ((h << 5) + h + seed.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

// First grapheme of the agent name (handles Chinese, emoji, etc).
function avatarInitial(agent) {
  const name = ((agent && agent.name) || '').trim();
  if (!name) return '?';
  const ch = Array.from(name)[0] || '?';
  return /[a-z]/i.test(ch) ? ch.toUpperCase() : ch;
}

// ── Lottie animation (optional, per agent.desktop_lottie_url) ──
// The animation replaces the initial/robot/face layers when active.
// On fetch failure (404, malformed JSON, etc) we silently fall back
// to whatever the identity-layer logic would have shown otherwise.
let _lottieAnim = null;
let _lottieLoadedFor = '';   // url currently rendered (or '' if none)

function _resolveLottieUrl(raw) {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith('/')) return API_HOST + s;
  return s;  // relative — let the webview resolve
}

function _destroyLottie() {
  if (_lottieAnim) {
    try { _lottieAnim.destroy(); } catch (_) {}
    _lottieAnim = null;
  }
  _lottieLoadedFor = '';
  const mount = document.getElementById('lottie-mount');
  if (mount) mount.innerHTML = '';
}

// Returns true if Lottie is now (or was already) rendering this URL.
// Calls onFail() asynchronously if the JSON fetch fails or parses bad.
function _ensureLottie(url, onFail) {
  if (!url) { _destroyLottie(); return false; }
  if (typeof lottie === 'undefined') { return false; }
  if (url === _lottieLoadedFor && _lottieAnim) { return true; }

  _destroyLottie();
  const mount = document.getElementById('lottie-mount');
  if (!mount) return false;

  try {
    _lottieAnim = lottie.loadAnimation({
      container: mount,
      renderer: 'svg',
      loop: true,
      autoplay: true,
      path: url,
    });
    _lottieLoadedFor = url;
    _lottieAnim.addEventListener('data_failed', () => {
      console.warn('[lottie] failed to load', url);
      _destroyLottie();
      if (onFail) onFail();
    });
    return true;
  } catch (e) {
    console.warn('[lottie] init error:', e);
    _destroyLottie();
    return false;
  }
}

function renderAgent() {
  if (!currentAgent) {
    avatar.classList.add('empty');
    // Misleading "未连接到 TudouClaw" implied a network/IPC failure.
    // Real state: TudouClaw IS reachable, we just got an empty
    // /agents/desktop list — meaning no agent has the desktop toggle
    // flipped on. Surface a CTA-style hint instead.
    avatar.title = '尚未启用桌面 Agent — 请到 portal 智能体页打开『桌面悬浮』开关';
    return;
  }
  avatar.classList.remove('empty');

  $('#agent-name').textContent = currentAgent.name || 'Agent';
  $('#agent-role').textContent = currentAgent.role_title || ('id ' + (currentAgent.id || '').slice(0, 6));
  const persona = (currentAgent.soul_md || '').trim();
  $('#agent-persona').textContent = persona.length > PERSONA_MAX_CHARS
    ? persona.slice(0, PERSONA_MAX_CHARS) + '…'
    : (persona || '（未填写性格设定）');

  // Per-agent hue — set on BOTH the small floating avatar (closed
  // state) and the big hero avatar (card-open state).
  const hue = String(avatarHue(currentAgent));
  avatar.style.setProperty('--avatar-hue', hue);
  const heroAv = $('#hero-avatar');
  if (heroAv) heroAv.style.setProperty('--avatar-hue', hue);

  // Identity layer (priority): lottie > robot_avatar > initial > face.
  // Each layer hides the others; on async failure we fall back one
  // step. `initialEl` is always pre-set so any fallback has content.
  const robotImg = $('#avatar-robot');
  const initialEl = $('#avatar-initial');
  const faceSvg = $('#avatar-face');
  faceSvg.style.display = 'none';
  initialEl.textContent = avatarInitial(currentAgent);

  const lottieUrl = _resolveLottieUrl(currentAgent.desktop_lottie_url);
  const lottieOk = _ensureLottie(lottieUrl, () => {
    if (currentAgent.robot_avatar) { robotImg.hidden = false; }
    else { initialEl.hidden = false; }
  });

  if (lottieOk) {
    robotImg.hidden = true;
    initialEl.hidden = true;
  } else if (currentAgent.robot_avatar) {
    robotImg.src = `${STATIC_BASE}/robots/${currentAgent.robot_avatar}.svg`;
    robotImg.hidden = false;
    initialEl.hidden = true;
    robotImg.onerror = () => {
      robotImg.hidden = true;
      initialEl.hidden = false;
    };
  } else {
    robotImg.hidden = true;
    initialEl.hidden = false;
  }

  // Mirror the identity layer onto the hero (card-open state). The
  // hero block uses parallel ids prefixed with "hero-" so we don't
  // double-handle lottie animation (single instance is enough; we
  // just clone the visible static layer). Robot image / initial / face
  // are re-set; lottie shows in the small avatar OR the hero, but not
  // both simultaneously to avoid lottie's single-instance limitation.
  const heroRobot = $('#hero-robot');
  const heroInitial = $('#hero-initial');
  const heroFace = $('#hero-face');
  if (heroRobot && heroInitial && heroFace) {
    heroFace.style.display = 'none';
    heroInitial.textContent = avatarInitial(currentAgent);
    if (currentAgent.robot_avatar) {
      heroRobot.src = `${STATIC_BASE}/robots/${currentAgent.robot_avatar}.svg`;
      heroRobot.hidden = false;
      heroInitial.hidden = true;
      heroRobot.onerror = () => {
        heroRobot.hidden = true;
        heroInitial.hidden = false;
      };
    } else if (lottieOk) {
      // No robot image — keep just the lottie visible on the small
      // avatar; the hero shows the initial as a fallback.
      heroRobot.hidden = true;
      heroInitial.hidden = false;
    } else {
      heroRobot.hidden = true;
      heroInitial.hidden = false;
    }
  }

  // Status (drives animation + status dot — color stays per-agent)
  const status = (currentAgent.status || 'idle').toLowerCase();
  statusPill.textContent = status;
  statusPill.dataset.status = status;
  avatar.classList.remove('idle', 'busy', 'error');
  avatar.classList.add(['idle', 'busy', 'error'].includes(status) ? status : 'idle');

  // Hero status dot color (mirrors avatar status)
  const heroDot = $('#hero-status-dot');
  if (heroDot) {
    if (status === 'busy') heroDot.style.background = '#fbbf24';
    else if (status === 'error') heroDot.style.background = '#ef4444';
    else heroDot.style.background = '#4ade80';
  }

  // Hover tooltip (system tooltip, ~500ms delay built-in)
  avatar.title = `${currentAgent.name || 'Agent'}` +
    (currentAgent.role_title ? ` · ${currentAgent.role_title}` : '') +
    ` · ${status}`;
}

// ── Card open/close ───────────────────────────────
//
// The Tauri window is sized for the floating avatar (140×140). When
// the user opens the card we need a much bigger surface — header +
// persona + chat log + input row don't fit in 128 usable pixels.
//
// Strategy: resize the window via the Tauri JS API on every toggle.
// `withGlobalTauri:true` in tauri.conf.json exposes `window.__TAURI__`,
// so we don't need to import an ES module. If the API is unavailable
// (older builds, browser preview, etc.) we silently fall back to the
// CSS-only toggle — the card still opens, just constrained.

const CARD_W = 360;
const CARD_H = 600;
const AVATAR_W = 140;
const AVATAR_H = 140;

async function _resizeFloaterWindow(width, height) {
  try {
    var T = (typeof window !== 'undefined') ? window.__TAURI__ : null;
    if (!T || !T.window) return;
    var win = T.window.getCurrentWindow
      ? T.window.getCurrentWindow()
      : (T.window.appWindow || null);
    if (!win || typeof win.setSize !== 'function') return;
    var Logical = T.window.LogicalSize;
    if (!Logical) return;
    await win.setSize(new Logical(width, height));
  } catch (e) {
    console.warn('[floater] resize failed:', e);
  }
}

function toggleCard() {
  const wasHidden = card.classList.contains('hidden');
  card.classList.toggle('hidden');
  avatar.style.display = wasHidden ? 'none' : '';
  // Fire-and-forget; don't await — keeps the UI snappy. The window
  // resize lands on the next frame.
  if (wasHidden) {
    _resizeFloaterWindow(CARD_W, CARD_H);
  } else {
    _resizeFloaterWindow(AVATAR_W, AVATAR_H);
  }
}

// ── Chat (write side — read side is Phase 3) ──────
function clearChat() { $('#chat-log').innerHTML = ''; }

function appendChat(role, text) {
  const log = $('#chat-log');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = (role === 'user' ? '› ' : role === 'sys' ? '⚠ ' : '') + text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// Streaming state — reset on every new send. The bubble is created
// when we open the EventSource so text_delta can append in place;
// closing/cancelling guarantees we don't leak stale streams when the
// user fires another message before the previous reply finished.
let _activeStream = null;          // EventSource | null
let _activeBubble = null;          // <div.msg.agent> currently being filled
let _sendInFlight = false;

async function sendChat() {
  if (_sendInFlight) return;
  if (!currentAgent) { appendChat('sys', '没有可用的 Agent'); return; }
  const inp = $('#chat-input');
  const sendBtn = $('#chat-send');
  const msg = inp.value.trim();
  if (!msg) return;
  appendChat('user', msg);
  inp.value = '';
  _sendInFlight = true;
  if (sendBtn) sendBtn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/agents/desktop/${currentAgent.id}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    if (res.status === 409) {
      // NO_LLM_CONFIGURED — agent has no provider/model
      const data = await res.json().catch(() => ({}));
      const m = (data.detail && data.detail.message) || 'Agent 未配置 LLM';
      appendChat('sys', m);
      return;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const m = (data.detail && (data.detail.message || data.detail)) || `HTTP ${res.status}`;
      appendChat('sys', typeof m === 'string' ? m : JSON.stringify(m));
      return;
    }
    const data = await res.json();
    if (data.task_id) {
      _streamResponse(data.task_id);
    } else {
      appendChat('sys', 'no task_id returned');
    }
  } catch (e) {
    appendChat('sys', e.message || String(e));
  } finally {
    _sendInFlight = false;
    if (sendBtn) sendBtn.disabled = false;
  }
}

function _closeActiveStream() {
  if (_activeStream) {
    try { _activeStream.close(); } catch (_) {}
    _activeStream = null;
  }
  _activeBubble = null;
}

function _streamResponse(taskId) {
  _closeActiveStream();
  const log = $('#chat-log');
  _activeBubble = document.createElement('div');
  _activeBubble.className = 'msg agent';
  _activeBubble.textContent = '…';
  log.appendChild(_activeBubble);
  log.scrollTop = log.scrollHeight;

  const url = `${API_BASE}/agents/desktop/chat-task/${taskId}/stream`;
  const es = new EventSource(url);
  _activeStream = es;

  let firstDelta = true;

  es.onmessage = (ev) => {
    if (ev.data === '[DONE]') { _closeActiveStream(); return; }
    let evt;
    try { evt = JSON.parse(ev.data); }
    catch (_) { return; }
    _handleStreamEvent(evt, () => { firstDelta = false; }, () => firstDelta);
  };
  es.onerror = () => { _closeActiveStream(); };
}

function _handleStreamEvent(evt, markDelta, isFirstDelta) {
  const log = $('#chat-log');
  switch (evt.type) {
    case 'text_delta':
      if (!_activeBubble) return;
      if (isFirstDelta()) { _activeBubble.textContent = ''; markDelta(); }
      _activeBubble.textContent += evt.content || '';
      log.scrollTop = log.scrollHeight;
      break;
    case 'text':
      // Final assembled message — replace whatever was streaming.
      if (!_activeBubble) return;
      if (evt.content) {
        _activeBubble.textContent = evt.content;
        markDelta();
        log.scrollTop = log.scrollHeight;
      }
      break;
    case 'tool_call': {
      const note = document.createElement('div');
      note.className = 'msg agent';
      note.style.fontStyle = 'italic';
      note.style.color = '#9aa0b4';
      note.textContent = '→ ' + (evt.name || 'tool');
      log.appendChild(note);
      log.scrollTop = log.scrollHeight;
      break;
    }
    case 'error':
      appendChat('sys', evt.content || 'error');
      break;
    case 'done':
    case 'status':
      // status is a heartbeat; done = task finished (next iter sends [DONE]).
      break;
    default:
      // ignore: thinking, plan_update, tool_result, etc. (not surfaced in MVP)
  }
}

// ── Bootstrap ─────────────────────────────────────
loadAgents();
setInterval(loadAgents, POLL_MS);
