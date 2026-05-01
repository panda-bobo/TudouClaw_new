}

// ============ Model selection helpers ============
let _availableModels = {};  // {provider_id: [model, ...]}
let _providerList = [];     // [{id, name, kind, ...}, ...]
let _defaultProvider = '';
let _defaultModel = '';

async function loadAvailableModels() {
  const cfg = await api('GET', '/api/portal/config');
  if (cfg) {
    _availableModels = cfg.available_models || {};
    _providerList = (cfg.providers || []).filter(p => p.enabled);
    // No silent fallback to ollama — agents must explicitly bind a provider.
    _defaultProvider = cfg.provider || '';
    _defaultModel = cfg.model || '';
    // Populate dynamic provider selects
    populateProviderSelects();
  }
}

function populateProviderSelects() {
  // Fill all provider <select> elements that have a default option only
  // ea-learning-provider / ea-multimodal-provider use a different blank label
  // ("use default") but otherwise share the same option list.
  var selIds = [
    'ca-provider', 'ea-provider',
    'ea-learning-provider', 'ea-multimodal-provider'
  ];
  selIds.forEach(selId => {
    const sel = document.getElementById(selId);
    if (!sel) return;
    const curVal = sel.value;
    var blankLabel = (selId === 'ea-learning-provider' || selId === 'ea-multimodal-provider')
      ? '（使用默认）'
      : '— 请选择 Provider —';
    sel.innerHTML = '<option value="">' + blankLabel + '</option>';
    _providerList.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      if (curVal === p.id) opt.selected = true;
      sel.appendChild(opt);
    });
  });
}

function updateModelSelect(providerSelectId, modelSelectId, currentModel) {
  const providerEl = document.getElementById(providerSelectId);
  const modelEl = document.getElementById(modelSelectId);
  if (!providerEl || !modelEl) return;
  // No silent fallback to _defaultProvider — only show models for the
  // explicitly-picked provider, so the user can't accidentally save a
  // model under provider="".
  const provider = providerEl.value;
  const models = provider ? (_availableModels[provider] || []) : [];
  modelEl.innerHTML = provider
    ? '<option value="">— 请选择 Model —</option>'
    : '<option value="">(先选择 Provider)</option>';
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    if (currentModel && m === currentModel) opt.selected = true;
    modelEl.appendChild(opt);
  });
  // Update multimodal tools hint when multimodal provider changes
  if (providerSelectId === 'ea-multimodal-provider') {
    _updateMmToolsHint();
  }
}

// ── Multimodal supports-tools hint ──
// Cloud providers (OpenAI, Claude, etc.) usually support vision+tools;
// local models (Ollama) usually don't.
var _CLOUD_PROVIDER_KINDS = ['openai', 'claude', 'anthropic'];

function _updateMmToolsHint() {
  var hintEl = document.getElementById('ea-mm-tools-hint');
  var cb = document.getElementById('ea-mm-supports-tools');
  var provEl = document.getElementById('ea-multimodal-provider');
  if (!hintEl || !cb || !provEl) return;

  var provId = provEl.value || '';
  if (!provId) { hintEl.style.display = 'none'; return; }

  // Find provider info
  var pInfo = _providerList.find(function(p) { return p.id === provId; });
  var kind = pInfo ? (pInfo.kind || '').toLowerCase() : '';
  var name = pInfo ? pInfo.name : provId;
  var isCloud = _CLOUD_PROVIDER_KINDS.indexOf(kind) >= 0
    || (kind === 'openai' || /openai|claude|anthropic|gemini|doubao|qwen|deepseek/i.test(name));
  var isLocal = (kind === 'ollama') || /ollama|lm.?studio|local/i.test(name);

  hintEl.style.display = 'block';
  if (isCloud) {
    hintEl.style.background = 'var(--primary-alpha, rgba(59,130,246,0.1))';
    hintEl.style.color = 'var(--primary, #3b82f6)';
    hintEl.innerHTML = '💡 <b>' + name + '</b> 为云端模型，通常支持 Vision + Tools，建议 <b>开启</b>';
    cb.checked = true;
  } else if (isLocal) {
    hintEl.style.background = 'var(--warning-alpha, rgba(245,158,11,0.1))';
    hintEl.style.color = 'var(--warning, #f59e0b)';
    hintEl.innerHTML = '💡 <b>' + name + '</b> 为本地模型，多数 Vision 模型不支持 Tools，建议 <b>关闭</b>';
    cb.checked = false;
  } else {
    hintEl.style.background = 'var(--surface3, #333)';
    hintEl.style.color = 'var(--text2, #999)';
    hintEl.innerHTML = '💡 请根据模型能力手动选择是否开启 Tools';
  }
}

// Load available models on startup
