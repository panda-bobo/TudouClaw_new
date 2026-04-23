}

// ============ Agents List (Category-based: Advisor / Enterprise / Personal) ============
var _AGENT_CLASSES = {
  advisor:    { label: '专业领域顾问', icon: 'school',       color: '#f0883e', desc: '法律、财务、医疗等领域专家，深度 RAG 知识库驱动' },
  enterprise: { label: '企业办公智能体', icon: 'apartment',   color: '#cbc9ff', desc: '编排、执行、守护型，完整记忆与工具链' },
  personal:   { label: '个人应用智能体', icon: 'person',      color: '#3fb950', desc: '轻量执行，支持有记忆/无记忆模式' }
};

function toggleAgentsOffice() {
  window._agentsOfficeOpen = !window._agentsOfficeOpen;
  renderAgentsList();
}

// Open create-agent modal pre-filled for a specific class
function openCreateAgentForClass(cls) {
  window._presetAgentClass = cls;
  showModal('create-agent');
  // Pre-select agent_class in the modal after it renders
  setTimeout(function() {
    var sel = document.getElementById('ca-agent-class');
    if (sel) sel.value = cls;
    _onAgentClassChange(cls);
  }, 50);
}

// Apply class-specific defaults when agent_class changes
function _onAgentClassChange(cls) {
  var memSel  = document.getElementById('ca-memory-mode');
  var execSel = document.getElementById('ca-exec-policy');
  var memLen  = document.getElementById('ca-memory');
  var tempEl  = document.getElementById('ca-temperature');
  var ragSel  = document.getElementById('ca-rag-mode');
  if (!memSel) return;
  if (cls === 'advisor') {
    memSel.value = 'full';
    if (execSel) execSel.value = 'deny';
    if (tempEl)  { tempEl.value = '0.3'; tempEl.dispatchEvent(new Event('input')); }
    if (ragSel)  { ragSel.value = 'private'; _onRagModeChange('private'); }
  } else if (cls === 'personal') {
    memSel.value = 'light';
    if (execSel) execSel.value = 'ask';
    if (memLen)  memLen.value = 'short';
    if (tempEl)  { tempEl.value = '0.7'; tempEl.dispatchEvent(new Event('input')); }
    if (ragSel)  { ragSel.value = 'none'; _onRagModeChange('none'); }
  } else {
    memSel.value = 'full';
    if (execSel) execSel.value = 'ask';
    if (memLen)  memLen.value = 'medium';
    if (tempEl)  { tempEl.value = '0.7'; tempEl.dispatchEvent(new Event('input')); }
    if (ragSel)  { ragSel.value = 'shared'; _onRagModeChange('shared'); }
  }
}

// Show/hide domain KB selector based on mode
function _onRagModeChange(mode) {
  var dkbArea = document.getElementById('ca-rag-domain-kb-area');
  if (dkbArea) {
    dkbArea.style.display = (mode === 'private' || mode === 'both') ? 'block' : 'none';
    if (mode === 'private' || mode === 'both') {
      _loadDomainKbOptions();
    }
  }
}

// Load domain knowledge bases into the create-agent dropdown
async function _loadDomainKbOptions() {
  var sel = document.getElementById('ca-rag-domain-kb');
  if (!sel) return;
  // Keep the default option, clear the rest
  while (sel.options.length > 1) sel.remove(1);
  try {
    var data = await api('POST', '/api/portal/domain-kb/list');
    var kbs = (data && data.knowledge_bases) || [];
    kbs.forEach(function(kb) {
      var opt = document.createElement('option');
      opt.value = kb.id;
      opt.textContent = kb.name + ' (' + kb.doc_count + ' 文档块)';
      sel.appendChild(opt);
    });
  } catch(e) { console.debug('load domain KBs:', e); }
  // Show info on selection change
  sel.onchange = function() {
    var info = document.getElementById('ca-rag-domain-kb-info');
    if (!info) return;
    if (!sel.value) { info.style.display = 'none'; return; }
    api('POST', '/api/portal/domain-kb/list').then(function(data) {
      var kbs = (data && data.knowledge_bases) || [];
      var kb = kbs.find(function(k){ return k.id === sel.value; });
      if (kb) {
        info.style.display = 'block';
        info.innerHTML = '<b>'+esc(kb.name)+'</b> — '+esc(kb.description||'无描述')
          + '<br>'+kb.doc_count+' 文档块 · Collection: '+esc(kb.collection)
          + (kb.tags.length ? '<br>标签: '+kb.tags.map(function(t){return esc(t)}).join(', ') : '');
      }
    });
  };
}

// Load RAG providers into dropdown
async function _loadRagProviders() {
  try {
    var data = await api('GET', '/api/portal/rag/providers');
    var sel = document.getElementById('ca-rag-provider');
    if (!sel || !data || !data.providers) return;
    // Keep the default local option, add remote providers
    data.providers.forEach(function(p) {
      if (!p.enabled) return;
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + ' (' + p.kind + (p.base_url ? ' — ' + p.base_url : '') + ')';
      sel.appendChild(opt);
    });
  } catch(e) { console.debug('load rag providers:', e); }
}

function _renderAgentCard(a) {
  var statusColor = a.status === 'idle' ? '#3fb950' : a.status === 'busy' ? '#f0883e' : a.status === 'error' ? '#f85149' : 'var(--text3)';
  var statusLabel = a.status === 'idle' ? 'Idle' : a.status === 'busy' ? 'Busy' : a.status === 'error' ? 'Error' : (a.status || 'Unknown');
  // Shared helper across chat header, info cards, dashboard — keeps
  // every surface showing the same clean label for the same raw ID.
  var _pp = window._prettyModelLabel || function(x){
    return (x||'default').split('/').pop().substring(0,20);
  };
  var modelShort = _pp(a.model) || 'default';
  if (modelShort.length > 20) modelShort = modelShort.substring(0,18) + '..';
  var roleBadge = a.role ? '<span style="padding:2px 6px;border-radius:4px;font-size:9px;font-weight:700;background:rgba(203,201,255,0.12);color:var(--primary);text-transform:uppercase;letter-spacing:0.3px">'+esc(a.role)+'</span>' : '';
  return '<div onclick="showAgentView(\''+a.id+'\')" style="background:var(--surface);border-radius:12px;padding:14px 16px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s;display:flex;align-items:center;gap:14px" onmouseenter="this.style.borderColor=\'var(--primary)\';this.style.transform=\'translateY(-1px)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\';this.style.transform=\'none\'">' +
    '<div style="width:40px;height:40px;border-radius:10px;background:var(--surface3);display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="material-symbols-outlined" style="font-size:22px;color:var(--primary)">smart_toy</span></div>' +
    '<div style="flex:1;min-width:0">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">' +
        '<span style="font-size:13px;font-weight:700;color:var(--text);font-family:\'Plus Jakarta Sans\',sans-serif">'+esc(a.name)+'</span>' +
        roleBadge +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text3)">' +
        '<span style="display:flex;align-items:center;gap:3px"><span style="width:6px;height:6px;border-radius:50%;background:'+statusColor+';display:inline-block"></span>'+statusLabel+'</span>' +
        '<span style="opacity:0.4">|</span>' +
        '<span title="'+esc(a.model||'default')+'">'+esc(modelShort)+'</span>' +
      '</div>' +
    '</div>' +
    '<span class="material-symbols-outlined" style="font-size:18px;color:var(--text3)">chevron_right</span>' +
  '</div>';
}

// Track expanded state: { 'enterprise:nodeId': true, ... }
var _agentsExpandedNodes = {};

function _toggleAgentNode(cls, nodeId) {
  var key = cls + ':' + nodeId;
  _agentsExpandedNodes[key] = !_agentsExpandedNodes[key];
  renderAgentsList();
}

function _getNodeForAgent(a) {
  // Determine which node an agent belongs to
  if (a.node_id && a.node_id !== 'local') return a.node_id;
  // Fall back to finding by node.agents list
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (n.agents) {
      var ids = n.agents.map(function(x){ return typeof x === 'string' ? x : x.id; });
      if (ids.indexOf(a.id) !== -1) return n.node_id;
    }
    if (n.is_self && (!a.node_id || a.node_id === 'local' || a.node_id === n.node_id)) return n.node_id;
  }
  return nodes.length > 0 ? nodes[0].node_id : 'unknown';
}

function _getNodeLabel(nodeId) {
  var n = nodes.find(function(x){ return x.node_id === nodeId; });
  if (!n) return nodeId || 'Unknown Node';
  var label = (n.name && n.name !== 'undefined') ? n.name : (n.node_id || 'Node');
  return n.is_self ? (label + ' (Local)') : label;
}

function _getNodeStatus(nodeId) {
  var n = nodes.find(function(x){ return x.node_id === nodeId; });
  return n ? n.status : 'offline';
}

function renderAgentsList() {
  var c = document.getElementById('content');

  if (nodes.length === 0) {
    c.innerHTML = '<div style="color:var(--text3);padding:40px;text-align:center"><span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:12px">device_hub</span>No nodes found.</div>';
    return;
  }

  var isOfficeOpen = !!window._agentsOfficeOpen;
  // AI Office 入口已下线（per user request 2026-04-23）
  var officeBtn = '';

  c.innerHTML = officeBtn
    + (isOfficeOpen ? '<div id="agents-office-wrap" style="height:360px;margin-bottom:16px;border:1px solid var(--border-light);border-radius:12px;overflow:hidden;background:#1a1a2e;position:relative"><canvas id="office-canvas" style="width:100%;height:100%;display:block"></canvas><div style="position:absolute;top:8px;left:16px;right:16px;display:flex;align-items:center;gap:6px;z-index:10"><span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">apartment</span><span style="font-family:monospace;font-size:11px;color:var(--primary);font-weight:800">AI AGENT OFFICE</span><span id="office-agent-count" style="font-size:10px;color:var(--text3);margin-left:auto;background:rgba(255,255,255,0.1);padding:2px 8px;border-radius:10px"></span></div></div>' : '')
    + '<div id="agents-list-content"></div>';
  if (isOfficeOpen) {
    setTimeout(function(){ try { _initOfficeScene(); } catch(e) { console.error('office init failed:', e); } }, 0);
  }

  // Classify ALL agents by class, then by node within each class
  var allGrouped = { advisor: {}, enterprise: {}, personal: {} };
  var classTotals = { advisor: 0, enterprise: 0, personal: 0 };

  agents.forEach(function(a) {
    var cls = (a.agent_class || (a.profile && a.profile.agent_class) || 'enterprise');
    if (!allGrouped[cls]) cls = 'enterprise';
    var nid = _getNodeForAgent(a);
    if (!allGrouped[cls][nid]) allGrouped[cls][nid] = [];
    allGrouped[cls][nid].push(a);
    classTotals[cls]++;
  });

  var sc = document.getElementById('agents-list-content');

  // === Render 3 big category blocks (豆腐块) ===
  var html = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:28px">';
  ['advisor', 'enterprise', 'personal'].forEach(function(cls) {
    var meta = _AGENT_CLASSES[cls];
    var count = classTotals[cls];
    var nodeCount = Object.keys(allGrouped[cls]).length;
    html += '<div style="background:var(--surface);border-radius:16px;padding:24px 20px;border:1px solid var(--border-light);position:relative;overflow:hidden;transition:all 0.2s" onmouseenter="this.style.borderColor=\''+meta.color+'\';this.style.transform=\'translateY(-2px)\';this.style.boxShadow=\'0 8px 24px rgba(0,0,0,0.15)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\';this.style.transform=\'none\';this.style.boxShadow=\'none\'">'
      + '<div style="position:absolute;top:0;left:0;right:0;height:3px;background:'+meta.color+';opacity:0.6"></div>'
      + '<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">'
        + '<div style="width:48px;height:48px;border-radius:14px;background:rgba('+_hexToRgb(meta.color)+',0.12);display:flex;align-items:center;justify-content:center"><span class="material-symbols-outlined" style="font-size:26px;color:'+meta.color+'">'+meta.icon+'</span></div>'
        + '<div><div style="font-size:15px;font-weight:700;color:var(--text);font-family:\'Plus Jakarta Sans\',sans-serif">'+meta.label+'</div>'
        + '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+count+' 个智能体' + (nodeCount > 0 ? '，分布在 ' + nodeCount + ' 个节点' : '') + '</div></div>'
      + '</div>'
      + '<div style="font-size:12px;color:var(--text3);line-height:1.5;margin-bottom:14px">'+meta.desc+'</div>'
      + '<div onclick="openCreateAgentForClass(\''+cls+'\')" style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:'+meta.color+';font-weight:600;cursor:pointer;padding:4px 0" onmouseenter="this.style.opacity=\'0.8\'" onmouseleave="this.style.opacity=\'1\'"><span class="material-symbols-outlined" style="font-size:16px">add_circle</span>创建新智能体</div>'
    + '</div>';
  });
  html += '</div>';

  // === Render each category with Node sub-groups ===
  ['advisor', 'enterprise', 'personal'].forEach(function(cls) {
    var nodesMap = allGrouped[cls];
    var nodeIds = Object.keys(nodesMap);
    if (nodeIds.length === 0) return;
    var meta = _AGENT_CLASSES[cls];
    var totalCount = classTotals[cls];

    html += '<div style="margin-bottom:24px">'
      + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;padding-left:4px">'
        + '<span class="material-symbols-outlined" style="font-size:20px;color:'+meta.color+'">'+meta.icon+'</span>'
        + '<span style="font-size:14px;font-weight:700;color:var(--text);font-family:\'Plus Jakarta Sans\',sans-serif">'+meta.label+'</span>'
        + '<span style="font-size:11px;color:var(--text3);background:var(--surface2);padding:2px 10px;border-radius:10px">'+totalCount+'</span>'
      + '</div>';

    // Render each node as an expandable group
    nodeIds.forEach(function(nid) {
      var nodeAgents = nodesMap[nid];
      var nodeLabel = _getNodeLabel(nid);
      var nodeStatus = _getNodeStatus(nid);
      var key = cls + ':' + nid;
      var isExpanded = !!_agentsExpandedNodes[key];
      var statusDot = nodeStatus === 'online'
        ? '<span style="width:7px;height:7px;border-radius:50%;background:#3fb950;display:inline-block"></span>'
        : '<span style="width:7px;height:7px;border-radius:50%;background:var(--text3);display:inline-block"></span>';

      html += '<div style="margin-bottom:10px;margin-left:8px">'
        // Node header (clickable to expand/collapse)
        + '<div onclick="_toggleAgentNode(\''+cls+'\',\''+nid+'\')" style="display:flex;align-items:center;gap:10px;padding:10px 16px;background:var(--surface);border:1px solid var(--border-light);border-radius:10px;cursor:pointer;transition:all 0.15s;user-select:none'+(isExpanded?';border-color:'+meta.color+';border-bottom-left-radius:0;border-bottom-right-radius:0':'')+'" onmouseenter="this.style.background=\'var(--surface2)\'" onmouseleave="this.style.background=\'var(--surface)\'">'
          + statusDot
          + '<span class="material-symbols-outlined" style="font-size:18px;color:var(--text3)">device_hub</span>'
          + '<span style="font-size:13px;font-weight:600;color:var(--text)">'+esc(nodeLabel)+'</span>'
          + '<span style="font-size:11px;color:'+meta.color+';background:rgba('+_hexToRgb(meta.color)+',0.12);padding:2px 10px;border-radius:10px;font-weight:600">'+nodeAgents.length+' 个 Agent</span>'
          + '<span class="material-symbols-outlined" style="font-size:18px;color:var(--text3);margin-left:auto;transition:transform 0.2s;transform:rotate('+(isExpanded?'180':'0')+'deg)">expand_more</span>'
        + '</div>';

      // Expanded agent cards
      if (isExpanded) {
        html += '<div style="padding:12px 16px;background:var(--surface);border:1px solid '+ meta.color +';border-top:none;border-radius:0 0 10px 10px">'
          + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px">'
          + nodeAgents.map(function(a){ return _renderAgentCard(a); }).join('')
          + '</div></div>';
      }

      html += '</div>';
    });

    html += '</div>';
  });

  // Empty state
  if (agents.length === 0) {
    html += '<div style="color:var(--text3);padding:20px;text-align:center;font-size:13px">暂无智能体，点击上方分类卡片创建</div>';
  }

  sc.innerHTML = html;
}

// Helper: hex color to rgb values string
function _hexToRgb(hex) {
  var r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return r+','+g+','+b;
