// ============ State ============
let agents = [], nodes = [], messages = [], approvals = [], auditLog = [], tokens = [], providers = [], projects = [];
let currentView = 'dashboard';
let currentAgent = null;
let currentProject = null;
let _renderEpoch = 0; // Guard: incremented on every view change to prevent async overwrites
// Admin context
let _adminCtx = { user_id: '', username: '', role: '', display_name: '', agent_ids: [], is_super: false };
let _settingsSubTab = 'providers'; // Current sub-tab inside Settings view
let _agentsSubTab = null; // Current node tab inside Agents view (null = first node)

// ============ Global soft toast (replaces alert) ============
(function() {
  window._toast = function(msg, type) {
    var old = document.getElementById('global-toast');
    if (old) old.remove();
    var isErr = type === 'err' || /失败|错误|error|fail|invalid/i.test(msg);
    var isOk = type === 'ok' || /成功|已|完成|saved|success|✓/i.test(msg);
    var bg = isErr ? '#ef4444' : isOk ? '#10b981' : '#60a5fa';
    var el = document.createElement('div');
    el.id = 'global-toast';
    el.style.cssText = 'position:fixed;top:24px;left:50%;transform:translateX(-50%);z-index:99999;'
      + 'padding:12px 28px;border-radius:8px;font-size:13px;color:#fff;background:' + bg + ';'
      + 'box-shadow:0 4px 16px rgba(0,0,0,.3);transition:opacity .4s;opacity:1;max-width:500px;text-align:center;'
      + 'white-space:pre-line;pointer-events:auto;cursor:pointer';
    el.textContent = msg;
    el.onclick = function() { el.remove(); };
    document.body.appendChild(el);
    setTimeout(function(){ el.style.opacity = '0'; }, 4000);
    setTimeout(function(){ el.remove(); }, 4500);
  };
  window.alert = function(msg) { window._toast(String(msg)); };
})();

// ============ API helper ============
async function api(method, url, body) {
  const opts = {method, credentials: 'same-origin', headers: {}};
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  if (resp.status === 401) { window.location.href = '/'; return null; }
  if (resp.status === 204) return {};
  const ct = resp.headers.get('Content-Type') || '';
  const raw = await resp.text();
  let data = null;
  if (ct.indexOf('application/json') >= 0 || (raw && (raw[0] === '{' || raw[0] === '['))) {
    try { data = JSON.parse(raw); }
    catch(e) {
      const snippet = raw.slice(0, 200);
      throw new Error('HTTP '+resp.status+' — invalid JSON from '+url+': '+snippet);
    }
  } else {
    const snippet = (raw || '').slice(0, 200).replace(/\s+/g,' ');
    throw new Error('HTTP '+resp.status+' '+method+' '+url+' — non-JSON response: '+snippet);
  }
  if (!resp.ok) { console.error('API error', url, data); if (data && typeof data === 'object') return data; return {error: 'HTTP ' + resp.status}; }
  return data;
}

function esc(s) { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

/* Resolve agent ID → "role-name" display string; falls back to ID */
function agentName(id) {
  if (!id) return 'Unknown';
  var ag = (window._cachedAgents || agents || []).find(function(a){ return a.id === id; });
  return ag ? (ag.role + '-' + ag.name) : id;
}

function showModal(name) {
  document.getElementById('modal-'+name).classList.remove('hidden');
  if (name === 'create-agent') {
    try { loadPersonasForCreate(); } catch(e) { console.warn('loadPersonasForCreate failed', e); }
  }
}
function hideModal(name) {
  document.getElementById('modal-'+name).classList.add('hidden');
  if(name==='create-token'){
    document.getElementById('ct-form').classList.remove('hidden');
    document.getElementById('ct-result').classList.add('hidden');
    document.getElementById('ct-name').value = '';
    document.getElementById('ct-role').value = 'admin';
    document.getElementById('ct-admin-bind').disabled = false;
    // Populate admin binding dropdown
    var sel = document.getElementById('ct-admin-bind');
    sel.innerHTML = '<option value="">-- No binding (legacy) --</option>';
    if (_adminCtx.is_super) {
      api('POST', '/api/portal/admins/list', {}).then(function(data) {
        if (data && data.admins) {
          data.admins.forEach(function(a) {
            var opt = document.createElement('option');
            opt.value = a.user_id;
            opt.textContent = a.display_name + ' (' + a.role + ')';
            sel.appendChild(opt);
          });
        }
      });
    } else if (_adminCtx.user_id) {
      var opt = document.createElement('option');
      opt.value = _adminCtx.user_id;
      opt.textContent = _adminCtx.display_name + ' (' + _adminCtx.role + ')';
      opt.selected = true;
      sel.appendChild(opt);
      sel.disabled = true;
    }
  }
}

function showChangePasswordModal() {
  var html = '<h3 style="margin-bottom:16px">修改密码</h3>';
  html += '<div class="form-group"><label>当前密码</label><input id="cpw-old" type="password" placeholder="Enter current password"></div>';
  html += '<div class="form-group"><label>新密码</label><input id="cpw-new" type="password" placeholder="At least 6 characters"></div>';
  html += '<div class="form-group"><label>确认新密码</label><input id="cpw-confirm" type="password" placeholder="Repeat new password"></div>';
  html += '<div class="form-actions"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button>';
  html += '<button class="btn btn-primary" onclick="submitChangePassword()">Save</button></div>';
  showCustomModal(html);
}
async function submitChangePassword() {
  var oldPw = document.getElementById('cpw-old').value;
  var newPw = document.getElementById('cpw-new').value;
  var confirmPw = document.getElementById('cpw-confirm').value;
  if (!oldPw || !newPw) { alert('请填写所有字段'); return; }
  if (newPw.length < 6) { alert('新密码至少6位'); return; }
  if (newPw !== confirmPw) { alert('两次密码不一致'); return; }
  var data = await api('POST', '/api/portal/admins/change-password', {
    user_id: _adminCtx.user_id, old_password: oldPw, new_password: newPw
  });
  if (data && data.ok) { closeModal(); alert('密码修改成功'); }
  else { alert((data && data.error) || '修改失败'); }
}

function logout() {
  // Call backend to clear HttpOnly cookie, then redirect
  fetch('/api/auth/logout', {method: 'POST', credentials: 'same-origin'})
    .finally(function() {
      // Also clear any non-HttpOnly remnants
      document.cookie = 'td_sess=; Path=/; SameSite=Lax; Expires=Thu, 01 Jan 1970 00:00:00 GMT';
      window.location.href = '/';
    });
}

function applyPortalModeRestrictions() {
  // In 'node' mode, hide admin-only sidebar items
  var isNode = portalMode === 'node';
  // Hide/disable Tokens menu (admin only)
  document.querySelectorAll('.nav-item[data-view="tokens"]').forEach(function(el) {
    el.style.display = isNode ? 'none' : '';
  });
  // Mark Settings as read-only in node mode (hide global config edit)
  var configNav = document.querySelector('.nav-item[data-view="config"]');
  if (configNav) {
    if (isNode) {
      configNav.innerHTML = configNav.innerHTML.replace('Settings', 'Settings <span style="font-size:9px;color:var(--text3)">(local)</span>');
    }
  }
  // In node mode, disable "Connect Node" button (can't manage remote nodes)
  if (isNode) {
    document.querySelectorAll('[onclick*="connectNode"]').forEach(function(el) {
      el.style.display = 'none';
    });
  }
  // Show mode indicator in header
  var header = document.querySelector('.portal-header') || document.querySelector('header');
  var modeTag = document.getElementById('portal-mode-tag');
  if (!modeTag && header) {
    modeTag = document.createElement('span');
    modeTag.id = 'portal-mode-tag';
    modeTag.style.cssText = 'font-size:11px;padding:2px 8px;border-radius:4px;margin-left:8px;font-weight:600;';
    header.appendChild(modeTag);
  }
  if (modeTag) {
    if (isNode) {
      modeTag.textContent = 'NODE';
      modeTag.style.background = 'var(--warning)';
      modeTag.style.color = '#000';
    } else {
      modeTag.textContent = 'HUB';
      modeTag.style.background = 'var(--success)';
      modeTag.style.color = '#000';
    }
  }
}

// ============ Refresh ============
var portalMode = 'hub';  // 'hub' or 'node'
async function refresh() {
  try {
    var data = await api('GET', '/api/portal/state');
    if (!data) return;
    agents = data.agents || [];
    window._cachedAgents = agents;  // Cache for robot avatar lookups
    nodes = data.nodes || [];
    try { _refreshNodeSwitcher(); } catch(e) {}
    messages = data.messages || [];
    portalMode = data.portal_mode || 'hub';
    var rawApprovals = data.approvals || {};
    approvals = [].concat(rawApprovals.pending || []).concat(rawApprovals.history || []);

    // Load projects list
    try {
      var pData = await api('GET', '/api/portal/projects');
      if (pData) projects = pData.projects || [];
    } catch(e){}

    // Load admin context
    try {
      var meData = await api('GET', '/api/portal/admin/me');
      if (meData && meData.admin) {
        _adminCtx = {
          user_id: meData.admin.user_id || '',
          username: meData.admin.username || '',
          role: meData.admin.role || '',
          display_name: meData.admin.display_name || '',
          agent_ids: meData.admin.agent_ids || [],
          is_super: meData.admin.role === 'superAdmin',
        };
      } else {
        // Token-based login — treat as superAdmin
        _adminCtx = { user_id: '', username: 'admin', role: 'superAdmin', display_name: 'Admin', agent_ids: [], is_super: true };
      }
    } catch(e) {
      _adminCtx = { user_id: '', username: 'admin', role: 'superAdmin', display_name: 'Admin', agent_ids: [], is_super: true };
    }
    // Update sidebar footer with admin info
    var footerUser = document.getElementById('footer-username');
    var footerRole = document.getElementById('footer-role');
    if (footerUser) footerUser.textContent = _adminCtx.display_name || _adminCtx.username || 'admin';
    if (footerRole) { footerRole.textContent = _adminCtx.role || 'admin'; footerRole.style.background = _adminCtx.is_super ? 'rgba(203,201,255,0.15)' : 'rgba(63,185,80,0.15)'; footerRole.style.color = _adminCtx.is_super ? 'var(--primary)' : 'var(--success)'; }
    // Show/hide admin management nav
    var adminNav = document.getElementById('nav-admin-manage');
    if (adminNav) adminNav.style.display = _adminCtx.is_super ? '' : 'none';
    // Show change password button for logged-in admin users
    var changePwBtn = document.getElementById('btn-change-pw');
    if (changePwBtn) changePwBtn.style.display = _adminCtx.user_id ? '' : 'none';

    // Apply portal mode UI restrictions
    applyPortalModeRestrictions();

    // Update agent count badge in sidebar
    var agentBadge = document.getElementById('agent-count-badge');
    if (agentBadge) agentBadge.textContent = agents.length;

    var nodeCountEl = document.getElementById('node-count');
    if (nodeCountEl) nodeCountEl.textContent = nodes.length;
    var pendingApprovals = approvals.filter(function(a){ return a.status === 'pending'; }).length;
    var approvalBadge = document.getElementById('approval-badge');
    var globalNotif = document.getElementById('global-notif-badge');
    if (globalNotif) {
      if (pendingApprovals > 0) { globalNotif.textContent = pendingApprovals; globalNotif.classList.remove('hidden'); }
      else { globalNotif.classList.add('hidden'); }
    }
    if (approvalBadge) {
      if (pendingApprovals > 0) {
        approvalBadge.textContent = pendingApprovals;
        approvalBadge.classList.remove('hidden');
      } else {
        approvalBadge.classList.add('hidden');
      }
    }

    // Pending manual reviews → dashboard sidebar badge
    try {
      var prData = await api('GET', '/api/portal/pending-reviews');
      var prCount = prData ? (prData.count || 0) : 0;
      var dashBadge = document.getElementById('dashboard-badge');
      if (dashBadge) {
        if (prCount > 0) {
          dashBadge.textContent = prCount;
          dashBadge.classList.remove('hidden');
          dashBadge.title = prCount + ' step(s) waiting for manual review';
        } else {
          dashBadge.classList.add('hidden');
        }
      }
      window._pendingReviews = prData || { count: 0, items: [] };
    } catch(e) {}

    buildAgentOptions();
    populateNodeSelect();
    renderCurrentView();
  } catch(e) { console.error('refresh error', e); }
}

async function refreshSidebar() {
  // Lightweight refresh: update agent status in sidebar without re-rendering current view
  try {
    var data = await api('GET', '/api/portal/state');
    if (!data) return;
    agents = data.agents || [];
    nodes = data.nodes || [];
    try { _refreshNodeSwitcher(); } catch(e) {}
    messages = data.messages || [];
    var rawApprovals = data.approvals || {};
    approvals = [].concat(rawApprovals.pending || []).concat(rawApprovals.history || []);
    var agentBadge2 = document.getElementById('agent-count-badge');
    if (agentBadge2) agentBadge2.textContent = agents.length;
    var nodeCountEl2 = document.getElementById('node-count');
    if (nodeCountEl2) nodeCountEl2.textContent = nodes.length;
    var pendingApprovals = approvals.filter(function(a){ return a.status === 'pending'; }).length;
    var approvalBadge = document.getElementById('approval-badge');
    if (approvalBadge) {
      if (pendingApprovals > 0) { approvalBadge.textContent = pendingApprovals; approvalBadge.classList.remove('hidden'); }
      else { approvalBadge.classList.add('hidden'); }
    }
    buildAgentOptions();
    // Update project count and global projects list
    try {
      var pData = await api('GET', '/api/portal/projects');
      if (pData) projects = pData.projects || [];
      var projCount = document.getElementById('project-count');
      if (projCount && pData) projCount.textContent = projects.length;
    } catch(e){}
    // Update event log and task count (but NOT chat messages)
    if (currentAgent) {
      loadAgentEventLog(currentAgent);
    }
  } catch(e) {}
}

// ============ Navigation ============
function showView(view, navEl) {
  // Clean up agent-specific timers when navigating away
  if (currentView === 'agent') _clearAllStepsTimers();
  currentView = view;
  currentAgent = null;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  if (navEl) navEl.classList.add('active');
  renderCurrentView();
}

function showAgentView(agentId, navEl) {
  // Clean up previous agent timers before switching
  _clearAllStepsTimers();
  currentView = 'agent';
  currentAgent = agentId;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  if (navEl) navEl.classList.add('active');
  renderCurrentView();
}

function renderCurrentView() {
  _renderEpoch++;
  const titleEl = document.getElementById('view-title');
  const actionsEl = document.getElementById('topbar-actions');
  const c = document.getElementById('content');
  c.style.padding = '24px';
  actionsEl.innerHTML = '';

  try {
  switch(currentView) {
    case 'dashboard': {
      titleEl.textContent = 'Overview';
      actionsEl.innerHTML = '<span style="font-size:11px;color:var(--text3);margin-right:8px">Tudou Claws v0.1</span><span style="padding:3px 8px;background:rgba(203,201,255,0.1);color:var(--primary);border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px">Active</span>';
      renderDashboard();
      break;
    }
    case 'agent': {
      var agentObj = agents.find(function(a){ return a.id === currentAgent; });
      titleEl.textContent = agentObj ? (agentObj.role||'general') + '-' + agentObj.name : 'Agent';
      actionsEl.innerHTML = '<button class="btn btn-ghost btn-sm" onclick="editAgentProfile(\''+currentAgent+'\')"><span class="material-symbols-outlined" style="font-size:16px">edit</span> Settings</button><button class="btn btn-ghost btn-sm" onclick="clearAgent(\''+currentAgent+'\')"><span class="material-symbols-outlined" style="font-size:16px">delete_sweep</span> Clear Chat</button><button class="btn btn-danger btn-sm" onclick="deleteAgent(\''+currentAgent+'\')"><span class="material-symbols-outlined" style="font-size:16px">delete</span> Delete</button>';
      // PERF/UX: if the chat DOM for this agent is already mounted, skip the full
      // re-render so we don't wipe streaming bubbles / approval cards / input focus.
      // Partial refreshers (loadAgentEventLog, loadExecutionSteps, loadAgentRuntimeStats)
      // take care of keeping side panels fresh.
      var existingChat = document.getElementById('chat-msgs-' + currentAgent);
      if (existingChat) {
        try { loadAgentEventLog(currentAgent); } catch(e) {}
        try { loadExecutionSteps(currentAgent); } catch(e) {}
        try { loadInterAgentMessages(currentAgent); } catch(e) {}
        try { loadAgentRuntimeStats(currentAgent); } catch(e) {}
        try { loadTasks(currentAgent); } catch(e) {}
        break;
      }
      renderAgentChat(currentAgent);
      break;
    }
    case 'nodes': {
      titleEl.textContent = 'Nodes';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-node\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Connect Node</button>';
      renderNodes();
      break;
    }
    case 'messages': {
      titleEl.textContent = 'Messages';
      renderMessages();
      break;
    }
    case 'channels': {
      titleEl.textContent = 'Channels';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-channel\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add Channel</button>';
      renderChannels();
      break;
    }
    case 'approvals': {
      titleEl.textContent = 'Tool Approvals & Policies';
      renderApprovals();
      break;
    }
    case 'audit': {
      titleEl.textContent = 'Audit Log';
      renderAudit();
      break;
    }
    case 'providers': {
      titleEl.textContent = 'LLM Providers';
      renderProviders();
      break;
    }
    case 'tokens': {
      titleEl.textContent = 'API Tokens';
      renderTokens();
      break;
    }
    case 'config': {
      titleEl.textContent = 'Configuration';
      renderConfig();
      break;
    }
    case 'nodeconfig': {
      titleEl.textContent = 'Node Configuration';
      renderNodeConfig();
      break;
    }
    case 'templates': {
      titleEl.textContent = '专业领域';
      renderTemplateLibrary();
      break;
    }
    case 'scheduler': {
      titleEl.textContent = 'Scheduled Tasks';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showCreateJob()"><span class="material-symbols-outlined" style="font-size:16px">add</span> New Job</button>';
      renderScheduler();
      break;
    }
    case 'mcpconfig': {
      titleEl.textContent = 'MCP Configuration';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showAddMCP()"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add MCP</button>';
      renderMCPConfig();
      break;
    }
    case 'projects': {
      titleEl.textContent = '项目与任务';
      renderProjectsHub();
      break;
    }
    case 'project_detail': {
      titleEl.textContent = 'Project';
      renderProjectDetail(currentProject);
      break;
    }
    case 'workflows': {
      titleEl.textContent = 'Workflows';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showCreateWorkflowModal()"><span class="material-symbols-outlined" style="font-size:16px">add</span> New Workflow</button>';
      renderWorkflows();
      break;
    }
    case 'self-improvement': {
      titleEl.textContent = 'Agent Self-Improvement (自我改进)';
      try { renderSelfImprovement(); } catch(re) { console.error('renderSelfImprovement error:', re); c.innerHTML = '<div style="color:var(--error);padding:20px">Self-Improvement render error: '+re.message+'</div>'; }
      break;
    }
    case 'agents-list': {
      titleEl.textContent = 'Agents';
      actionsEl.innerHTML = '';
      renderAgentsList();
      break;
    }
    case 'settings': {
      titleEl.textContent = 'Settings';
      renderSettingsPage();
      break;
    }
    case 'admin-manage': {
      titleEl.textContent = 'Admin Management';
      renderAdminManage();
      break;
    }
    case 'knowledge-memory': {
      titleEl.textContent = '知识与记忆';
      renderKnowledgeMemoryHub();
      break;
    }
    case 'roles-skills': {
      titleEl.textContent = '角色与技能';
      renderRolesSkillsHub();
      break;
    }
    case 'tools-approvals': {
      titleEl.textContent = '工具与审批';
      renderToolsApprovalsHub();
      break;
    }
    case 'integrations': {
      titleEl.textContent = '集成与通知';
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-channel\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add Channel</button>';
      renderIntegrationsHub();
      break;
    }
    case 'settings-hub': {
      titleEl.textContent = '系统设置';
      renderSettingsHub();
      break;
    }
    default: {
      c.innerHTML = '<div style="color:var(--text3);padding:40px;text-align:center">View "'+esc(currentView)+'" not found</div>';
      break;
    }
  }
  } catch(err) { console.error('renderCurrentView error:', err); c.innerHTML = '<div style="color:var(--error);padding:20px">Render error: '+err.message+'</div>'; }
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
  var modelShort = (a.model || 'default').split('/').pop().substring(0, 20);
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
  var officeBtn = '<div style="display:flex;justify-content:flex-end;margin-bottom:16px"><button onclick="toggleAgentsOffice()" style="padding:8px 14px;border:1px solid var(--border);background:'+(isOfficeOpen?'var(--primary)':'var(--surface2)')+';color:'+(isOfficeOpen?'#fff':'var(--text)')+';font-size:12px;font-weight:600;cursor:pointer;border-radius:8px;display:flex;align-items:center;gap:6px;font-family:inherit"><span class="material-symbols-outlined" style="font-size:16px">apartment</span>AI Office</button></div>';

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
}

// ============ Dashboard ============
function _dashFmtTokens(n) {
  if (!n || n === 0) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return '' + n;
}

function renderDashboard() {
  var c = document.getElementById('content');
  var epoch = _renderEpoch;
  var idle = agents.filter(function(a){return a.status==='idle'}).length;
  var busy = agents.filter(function(a){return a.status==='busy'}).length;
  var errorA = agents.filter(function(a){return a.status==='error'}).length;
  var safeApprovals = Array.isArray(approvals) ? approvals : [].concat((approvals||{}).pending||[]).concat((approvals||{}).history||[]);
  var pending = safeApprovals.filter(function(a){return a.status==='pending'}).length;

  // Token totals: separate In (sent to model) and Out (returned by model)
  var tokensIn = 0, tokensOut = 0;
  agents.forEach(function(a) {
    var tu = a.cost_summary || {};
    tokensIn += (tu.input_tokens || 0);
    tokensOut += (tu.output_tokens || 0);
  });
  var tokensTotal = tokensIn + tokensOut;

  var statusOrb = function(s) {
    if(s==='idle') return 'background:#3fb950;box-shadow:0 0 8px rgba(63,185,80,0.5)';
    if(s==='busy') return 'background:#d29922;box-shadow:0 0 8px rgba(210,153,34,0.5);animation:pulse 1.5s infinite';
    if(s==='error') return 'background:#f85149;box-shadow:0 0 8px rgba(248,81,73,0.5)';
    return 'background:var(--text3)';
  };
  var statusLabel = function(s, a) {
    if (s === 'busy') return 'Working';
    if (a && a.self_improvement && a.self_improvement.is_learning) return 'Learning';
    if (s === 'idle') return 'Online';
    if (s === 'error') return 'Error';
    return 'Offline';
  };

  // Build robot avatar helper
  var robotSrc = function(a) {
    var rid = a.robot_avatar || ('robot_' + (a.role || 'general'));
    return '/static/robots/' + rid + '.svg';
  };

  c.innerHTML = `
    <!-- Header -->
    <div style="margin-bottom:24px">
      <h2 style="font-family:'Plus Jakarta Sans',sans-serif;font-size:26px;font-weight:800;letter-spacing:-0.5px;line-height:1.2">System Overview</h2>
      <p style="color:var(--text3);font-size:13px;margin-top:4px">Tudou Claw — Multi-Agent Coordination Hub</p>
    </div>

    <!-- Row 1: 6 metric cards (clickable) -->
    <section style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:24px">
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="_settingsSubTab='nodes';showView('settings',null)"
           onmouseenter="this.style.borderColor='rgba(203,201,255,0.3)';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Agents</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:var(--primary);line-height:1">${agents.length}</div>
        <div style="display:flex;justify-content:center;gap:8px;margin-top:8px">
          <span style="font-size:10px;color:#3fb950;font-weight:600">${idle}<span style="color:var(--text3);font-weight:400"> on</span></span>
          <span style="font-size:10px;color:#d29922;font-weight:600">${busy}<span style="color:var(--text3);font-weight:400"> busy</span></span>
          <span style="font-size:10px;color:#f85149;font-weight:600">${errorA}<span style="color:var(--text3);font-weight:400"> err</span></span>
        </div>
      </div>
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="showView('projects',null)"
           onmouseenter="this.style.borderColor='rgba(63,185,80,0.3)';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Projects</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:var(--success);line-height:1" id="dash-project-count">-</div>
        <div style="font-size:10px;color:var(--text3);margin-top:8px">active</div>
      </div>
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="_settingsSubTab='providers';showView('settings',null)"
           onmouseenter="this.style.borderColor='rgba(33,150,243,0.3)';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Tokens In</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:#2196F3;line-height:1">${_dashFmtTokens(tokensIn)}</div>
        <div style="font-size:10px;color:var(--text3);margin-top:8px">sent to model</div>
      </div>
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="_settingsSubTab='providers';showView('settings',null)"
           onmouseenter="this.style.borderColor='rgba(76,175,80,0.3)';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Tokens Out</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:#4CAF50;line-height:1">${_dashFmtTokens(tokensOut)}</div>
        <div style="font-size:10px;color:var(--text3);margin-top:8px">from model</div>
      </div>
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="_settingsSubTab='providers';showView('settings',null)"
           onmouseenter="this.style.borderColor='rgba(255,255,255,0.1)';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Tokens Total</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:var(--text);line-height:1">${_dashFmtTokens(tokensTotal)}</div>
        <div style="font-size:10px;color:var(--text3);margin-top:8px">all agents</div>
      </div>
      <div style="background:var(--surface);border-radius:12px;padding:16px;border:1px solid var(--border-light);text-align:center;cursor:pointer;transition:all 0.15s"
           onclick="showView('approvals',null)"
           onmouseenter="this.style.borderColor='${pending>0?'rgba(210,153,34,0.3)':'rgba(255,255,255,0.1)'}';this.style.transform='translateY(-2px)'"
           onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
        <div style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:700;letter-spacing:1px;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Approvals</div>
        <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:28px;font-weight:800;color:${pending>0?'var(--warning)':'var(--text)'};line-height:1">${pending}</div>
        <div style="font-size:10px;color:var(--text3);margin-top:8px">pending</div>
      </div>
    </section>

    <!-- Row 2: Agent Cards (full width, responsive grid) -->
    <section style="margin-bottom:24px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h3 style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text);font-family:'Plus Jakarta Sans',sans-serif">Active Agents</h3>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px">
        ${agents.map(a => {
          var tu = a.cost_summary || {};
          var aIn = tu.input_tokens || 0;
          var aOut = tu.output_tokens || 0;
          var modelShort = (a.model||'default').split('/').pop().split(':')[0];
          if (modelShort.length > 16) modelShort = modelShort.substring(0,14) + '..';
          return `
          <div style="background:var(--surface);padding:12px 14px;border-radius:10px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s;display:flex;align-items:center;gap:10px;overflow:hidden"
               onclick="showAgentView('${a.id}',null)"
               onmouseenter="this.style.borderColor='rgba(203,201,255,0.2)';this.style.transform='translateY(-1px)'"
               onmouseleave="this.style.borderColor='var(--border-light)';this.style.transform='none'">
            <img src="${robotSrc(a)}" style="width:32px;height:32px;border-radius:8px;flex-shrink:0;background:var(--surface3)" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" alt="">
            <div style="width:32px;height:32px;border-radius:8px;background:var(--surface3);display:none;align-items:center;justify-content:center;flex-shrink:0">
              <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">smart_toy</span>
            </div>
            <div style="flex:1;min-width:0">
              <div style="font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%">${esc(a.name)}</div>
              <div style="display:flex;align-items:center;gap:5px;margin-top:2px">
                <span style="width:5px;height:5px;border-radius:50%;display:inline-block;flex-shrink:0;${statusOrb(a.status)}"></span>
                <span style="font-size:9px;color:var(--text3);text-transform:uppercase;font-weight:600">${statusLabel(a.status, a)}</span>
                <span style="font-size:9px;color:var(--text3);margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:80px" title="${esc(a.model||'default')}">${esc(modelShort)}</span>
              </div>
            </div>
            <div style="text-align:right;flex-shrink:0;border-left:1px solid var(--border-light);padding-left:10px;min-width:52px">
              <div style="font-size:9px;color:#2196F3;font-family:monospace;white-space:nowrap">${_dashFmtTokens(aIn)} in</div>
              <div style="font-size:9px;color:#4CAF50;font-family:monospace;white-space:nowrap;margin-top:1px">${_dashFmtTokens(aOut)} out</div>
            </div>
          </div>`;
        }).join('')}
        ${agents.length === 0 ? '<div style="color:var(--text3);padding:20px;grid-column:1/-1;font-size:13px">No agents deployed. Click "Deploy Agent" to create one.</div>' : ''}
      </div>
    </section>

    <!-- Row 3: Projects Status -->
    <section style="margin-bottom:24px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h3 style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text);font-family:'Plus Jakarta Sans',sans-serif">Projects</h3>
        <span style="color:var(--primary);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer" onclick="showView('projects',null)">View All</span>
      </div>
      <div id="dash-projects-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px">
        <div style="color:var(--text3);font-size:13px;padding:16px">Loading projects...</div>
      </div>
    </section>

    <!-- Row 3.5: Pending Manual Review (only renders when there are items) -->
    <section id="dash-review-section" style="margin-bottom:24px;display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h3 style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text);font-family:'Plus Jakarta Sans',sans-serif">
          <span style="color:#3b82f6">⏸ Pending Manual Review</span>
          <span id="dash-review-count" style="background:#3b82f6;color:#000;padding:1px 8px;border-radius:10px;font-size:10px;margin-left:8px;font-weight:800">0</span>
        </h3>
      </div>
      <div id="dash-review-list" style="display:flex;flex-direction:column;gap:8px"></div>
    </section>

    <!-- Row 4: System Modules (4 cards in a row) -->
    <section style="margin-bottom:24px">
      <h3 style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;font-family:'Plus Jakarta Sans',sans-serif">System Modules</h3>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px" id="dashboard-modules">
        <div style="background:var(--surface);border-radius:10px;padding:18px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s"
             onclick="showView('nodes',null)"
             onmouseenter="this.style.borderColor='rgba(203,201,255,0.2)'" onmouseleave="this.style.borderColor='var(--border-light)'">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary);flex-shrink:0">hub</span>
            <span style="font-size:12px;font-weight:600">Nodes</span>
          </div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:24px;font-weight:800;color:var(--text);line-height:1">${nodes.length}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:6px">${nodes.filter(n=>n.status==='online').length} online</div>
        </div>
        <div style="background:var(--surface);border-radius:10px;padding:18px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s"
             onclick="showView('channels',null)"
             onmouseenter="this.style.borderColor='rgba(203,201,255,0.2)'" onmouseleave="this.style.borderColor='var(--border-light)'">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary);flex-shrink:0">cable</span>
            <span style="font-size:12px;font-weight:600">Channels</span>
          </div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:24px;font-weight:800;color:var(--text);line-height:1" id="dash-channel-count">-</div>
          <div style="font-size:10px;color:var(--text3);margin-top:6px">IM integrations</div>
        </div>
        <div style="background:var(--surface);border-radius:10px;padding:18px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s"
             onclick="showView('providers',null)"
             onmouseenter="this.style.borderColor='rgba(203,201,255,0.2)'" onmouseleave="this.style.borderColor='var(--border-light)'">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="material-symbols-outlined" style="font-size:18px;color:var(--primary);flex-shrink:0">dns</span>
            <span style="font-size:12px;font-weight:600">Providers</span>
          </div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:24px;font-weight:800;color:var(--text);line-height:1" id="dash-provider-count">-</div>
          <div style="font-size:10px;color:var(--text3);margin-top:6px">model backends</div>
        </div>
        <div style="background:var(--surface);border-radius:10px;padding:18px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s"
             onclick="showView('approvals',null)"
             onmouseenter="this.style.borderColor='rgba(203,201,255,0.2)'" onmouseleave="this.style.borderColor='var(--border-light)'">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="material-symbols-outlined" style="font-size:18px;color:${pending>0?'var(--warning)':'var(--primary)'};flex-shrink:0">shield</span>
            <span style="font-size:12px;font-weight:600">Approvals</span>
          </div>
          <div style="font-family:'Plus Jakarta Sans',sans-serif;font-size:24px;font-weight:800;color:${pending>0?'var(--warning)':'var(--text)'};line-height:1">${pending}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:6px">pending</div>
        </div>
      </div>
    </section>
  `;

  // Async: load counts
  api('GET', '/api/portal/channels').then(data => {
    if (epoch !== _renderEpoch) return;
    const el = document.getElementById('dash-channel-count');
    if (el && data) el.textContent = (data.channels||[]).length;
  });
  api('GET', '/api/portal/providers').then(data => {
    if (epoch !== _renderEpoch) return;
    const el = document.getElementById('dash-provider-count');
    if (el && data) el.textContent = (data.providers||[]).length;
  });
  // Async: load projects for dashboard
  api('GET', '/api/portal/projects').then(async function(data) {
    if (epoch !== _renderEpoch) return;
    var grid = document.getElementById('dash-projects-grid');
    if (!grid || !data) return;
    var projects = data.projects || [];
    var countEl = document.getElementById('dash-project-count');
    if (countEl) countEl.textContent = projects.length;

    // ── Build pending-manual-review list ──
    // Prefer the dedicated endpoint (always fresh, always includes steps).
    // Fall back to walking the projects list when the endpoint hasn't run yet.
    var reviewItems = [];
    try {
      var prResp = await api('GET', '/api/portal/pending-reviews');
      if (prResp && prResp.items) {
        reviewItems = prResp.items.map(function(it) {
          return {
            proj_id: it.proj_id, proj_name: it.proj_name,
            task_id: it.task_id, task_title: it.task_title,
            assignee: it.assignee, step: it.step,
          };
        });
      }
    } catch(_e) {
      projects.forEach(function(p) {
        (p.tasks || []).forEach(function(t) {
          (t.steps || []).forEach(function(s) {
            if (s.status === 'awaiting_review') {
              reviewItems.push({
                proj_id: p.id, proj_name: p.name,
                task_id: t.id, task_title: t.title,
                assignee: t.assigned_to,
                step: s,
              });
            }
          });
        });
      });
    }
    var revSection = document.getElementById('dash-review-section');
    var revList = document.getElementById('dash-review-list');
    var revCount = document.getElementById('dash-review-count');
    if (revSection && revList) {
      if (reviewItems.length === 0) {
        revSection.style.display = 'none';
      } else {
        revSection.style.display = '';
        if (revCount) revCount.textContent = reviewItems.length;
        // Sort: oldest first (so the longest-waiting bubble to top)
        reviewItems.sort(function(a, b){
          return (a.step.completed_at || 0) - (b.step.completed_at || 0);
        });
        revList.innerHTML = reviewItems.map(function(it) {
          var ag = agents.find(function(a){ return a.id === it.assignee; });
          var assigneeName = ag ? (ag.role + '-' + ag.name) : (it.assignee || '?');
          var waitMin = '';
          if (it.step.completed_at) {
            var secs = Math.floor(Date.now()/1000 - it.step.completed_at);
            if (secs < 60) waitMin = secs + 's';
            else if (secs < 3600) waitMin = Math.floor(secs/60) + 'm';
            else if (secs < 86400) waitMin = Math.floor(secs/3600) + 'h';
            else waitMin = Math.floor(secs/86400) + 'd';
          }
          var draft = String(it.step.result || '').slice(0, 240);
          if ((it.step.result || '').length > 240) draft += '…';
          return '<div style="background:var(--surface);border-left:3px solid #3b82f6;border-radius:8px;padding:12px 14px;border:1px solid rgba(59,130,246,0.25)">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px;flex-wrap:wrap">' +
              '<div style="font-size:12px;font-weight:700;display:flex;align-items:center;gap:6px;flex-wrap:wrap">' +
                '<span style="color:var(--primary);cursor:pointer" onclick="openProject(\''+it.proj_id+'\')">📁 '+esc(it.proj_name)+'</span>' +
                '<span style="color:var(--text3)">›</span>' +
                '<span>'+esc(it.task_title)+'</span>' +
                '<span style="color:var(--text3)">›</span>' +
                '<span style="color:#3b82f6">'+esc(it.step.name)+'</span>' +
                '<span style="font-size:9px;background:rgba(59,130,246,0.15);color:#3b82f6;padding:1px 5px;border-radius:3px;margin-left:2px">👤 人工</span>' +
              '</div>' +
              '<div style="font-size:10px;color:var(--text3);display:flex;gap:8px;align-items:center">' +
                '<span>by '+esc(assigneeName)+'</span>' +
                (waitMin ? '<span title="等待时长">⏱ '+waitMin+'</span>' : '') +
              '</div>' +
            '</div>' +
            (draft ? '<div style="font-size:11px;color:var(--text2);background:rgba(59,130,246,0.06);border-radius:4px;padding:8px 10px;margin:6px 0;line-height:1.5;max-height:80px;overflow:hidden">'+esc(draft)+'</div>' : '') +
            '<div style="display:flex;gap:6px;justify-content:flex-end;margin-top:8px">' +
              '<button onclick="reviewStep(\''+esc(it.proj_id)+'\',\''+esc(it.task_id)+'\',\''+esc(it.step.id)+'\',\'reject\')" style="font-size:11px;background:rgba(239,68,68,0.15);color:#ef4444;border:1px solid rgba(239,68,68,0.4);border-radius:4px;padding:4px 12px;cursor:pointer;font-weight:600">✗ 驳回</button>' +
              '<button onclick="reviewStep(\''+esc(it.proj_id)+'\',\''+esc(it.task_id)+'\',\''+esc(it.step.id)+'\',\'approve\')" style="font-size:11px;background:#22c55e;color:#000;border:none;border-radius:4px;padding:4px 14px;cursor:pointer;font-weight:700">✓ 通过</button>' +
              '<button onclick="openProject(\''+esc(it.proj_id)+'\')" style="font-size:11px;background:transparent;color:var(--text3);border:1px solid rgba(255,255,255,0.1);border-radius:4px;padding:4px 10px;cursor:pointer">查看</button>' +
            '</div>' +
          '</div>';
        }).join('');
      }
    }
    if (projects.length === 0) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:24px;color:var(--text3);font-size:13px">No projects yet. <span style="color:var(--primary);cursor:pointer;font-weight:600" onclick="showView(\'projects\',null)">Create one</span></div>';
      return;
    }
    grid.innerHTML = projects.map(function(p) {
      var memberCount = (p.members||[]).length;
      var taskCount = (p.tasks||[]).length;
      var doneTasks = (p.tasks||[]).filter(function(t){return t.status==='done'}).length;
      var inProgress = (p.tasks||[]).filter(function(t){return t.status==='in_progress'}).length;
      var progress = taskCount > 0 ? Math.round(doneTasks / taskCount * 100) : 0;
      var progressColor = progress >= 100 ? 'var(--success)' : progress > 0 ? 'var(--primary)' : 'var(--text3)';
      // Build member avatars
      var memberAvatars = (p.members||[]).slice(0,5).map(function(m) {
        var ag = agents.find(function(a){ return a.id === m.agent_id; });
        var initial = ag ? (ag.name||'?')[0] : '?';
        return '<div style="width:26px;height:26px;border-radius:50%;background:var(--surface3);border:2px solid var(--bg);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;color:var(--primary);margin-left:-6px" title="'+esc(ag?(ag.role+'-'+ag.name):m.agent_id)+'">'+esc(initial)+'</div>';
      }).join('');
      var extraMembers = memberCount > 5 ? '<div style="width:26px;height:26px;border-radius:50%;background:var(--surface3);border:2px solid var(--bg);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:var(--text3);margin-left:-6px">+' + (memberCount-5) + '</div>' : '';
      return '<div style="background:var(--surface);border-radius:14px;padding:20px;border:1px solid var(--border-light);cursor:pointer;transition:all 0.15s" onclick="openProject(\''+p.id+'\')" onmouseenter="this.style.borderColor=\'rgba(203,201,255,0.2)\';this.style.transform=\'translateY(-2px)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\';this.style.transform=\'none\'">' +
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">' +
          '<div>' +
            '<div style="font-size:15px;font-weight:700;letter-spacing:0.1px">' + esc(p.name) + '</div>' +
            '<div style="font-size:11px;color:var(--text3);margin-top:2px;max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(p.description||'No description') + '</div>' +
          '</div>' +
          '<div style="display:flex;align-items:center;gap:4px;flex-shrink:0">' +
            '<span class="material-symbols-outlined" style="font-size:16px;color:var(--primary)">folder</span>' +
          '</div>' +
        '</div>' +
        '<!-- Progress -->' +
        '<div style="margin-bottom:12px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">' +
            '<span style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Progress</span>' +
            '<span style="font-size:11px;font-weight:700;color:' + progressColor + '">' + progress + '%</span>' +
          '</div>' +
          '<div style="width:100%;height:5px;background:var(--surface3);border-radius:4px;overflow:hidden">' +
            '<div style="height:100%;background:' + progressColor + ';border-radius:4px;transition:width 0.3s;width:' + progress + '%"></div>' +
          '</div>' +
        '</div>' +
        '<!-- Members + Tasks Row -->' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<div style="display:flex;align-items:center;padding-left:6px">' + memberAvatars + extraMembers + '</div>' +
          '<div style="display:flex;gap:12px;font-size:10px;color:var(--text3)">' +
            '<span title="In Progress"><span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle;color:var(--warning)">pending</span> ' + inProgress + '</span>' +
            '<span title="Done"><span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle;color:var(--success)">check_circle</span> ' + doneTasks + '/' + taskCount + '</span>' +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('');
  });
}

// ============ Agent Chat ============
function renderAgentChat(agentId) {
  var c = document.getElementById('content');
  c.style.padding = '0';
  c.style.display = 'flex';
  c.style.flexDirection = 'column';
  c.style.height = '100%';
  var ag = agents.find(function(a){ return a.id === agentId; }) || {};
  var agRole = esc(ag.role || 'general');
  var agName = esc(ag.name || 'Agent');
  var agDisplayName = agRole + '-' + agName;
  c.innerHTML = '' +
    '<!-- Chat Section: 60% height -->' +
    '<section style="display:flex;flex-direction:column;height:60%;flex-shrink:0;background:var(--surface);border-bottom:1px solid rgba(255,255,255,0.05);overflow:hidden;position:relative">' +
      '<div style="padding:10px 20px;border-bottom:1px solid rgba(255,255,255,0.05);display:flex;justify-content:space-between;align-items:center;background:var(--bg2);backdrop-filter:blur(16px)">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<img src="' + (ag.robot_avatar ? '/static/robots/'+ag.robot_avatar+'.svg' : '/static/robots/robot_'+agRole+'.svg') + '" style="width:28px;height:28px" onerror="this.outerHTML=\'<span class=material-symbols-outlined style=color:var(--primary);font-size:20px>smart_toy</span>\'">' +
          '<span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:14px;font-weight:600">' + agDisplayName + '</span>' +
          '<button class="btn btn-ghost btn-sm" onclick="showSoulEditor(\'' + agentId + '\')" title="Edit SOUL.md" style="padding:4px 8px;font-size:10px"><span class="material-symbols-outlined" style="font-size:14px">auto_awesome</span> SOUL</button>' +
          '<button class="btn btn-ghost btn-sm" onclick="showThinkingPanel(\'' + agentId + '\')" title="Active Thinking" style="padding:4px 8px;font-size:10px"><span class="material-symbols-outlined" style="font-size:14px">psychology</span> Think</button>' +
          '<button id="tts-btn-' + agentId + '" class="btn btn-ghost btn-sm" onclick="_toggleTTS(\'' + agentId + '\')" title="自动朗读新消息 (Auto TTS)" style="padding:4px 8px;font-size:10px"><span class="material-symbols-outlined" style="font-size:14px;color:var(--text3)">volume_up</span></button>' +
          '<button class="btn btn-ghost btn-sm" onclick="wakeAgent(\'' + agentId + '\')" title="唤醒：扫描所有项目里分配给该 agent 的未完成任务并继续执行" style="padding:4px 8px;font-size:10px"><span class="material-symbols-outlined" style="font-size:14px">notifications_active</span> Wake</button>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<select id="agent-quick-provider-' + agentId + '" onchange="quickSwitchModel(\'' + agentId + '\')" style="padding:4px 8px;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text2);font-size:11px;max-width:120px"></select>' +
          '<select id="agent-quick-model-' + agentId + '" onchange="quickSwitchModel(\'' + agentId + '\')" style="padding:4px 8px;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:11px;max-width:180px"></select>' +
          '<div style="display:flex;align-items:center;gap:6px"><div style="width:5px;height:5px;border-radius:50%;background:var(--primary);animation:pulse 1.5s infinite"></div><span style="font-size:10px;color:var(--primary);text-transform:uppercase;font-weight:700;letter-spacing:0.5px">Online</span></div>' +
        '</div>' +
      '</div>' +
      '<div class="chat-messages" id="chat-msgs-' + agentId + '" style="flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px"></div>' +
      '<div style="padding:14px 20px;background:var(--bg2);border-top:1px solid rgba(255,255,255,0.05)">' +
        '<div id="agent-attach-preview-' + agentId + '" style="display:none;flex-wrap:wrap;gap:6px;padding:6px 10px;margin-bottom:6px"></div>' +
        '<div style="display:flex;align-items:center;gap:10px;background:var(--surface3);padding:4px;border-radius:10px;border:1px solid rgba(255,255,255,0.05)">' +
          '<input id="chat-input-' + agentId + '" type="text" placeholder="Direct Agent Tasking..." style="flex:1;background:transparent;border:none;color:var(--text);font-size:14px;padding:10px 16px;outline:none" onkeydown="if(event.key===\'Enter\'&&!event.isComposing){event.preventDefault();sendAgentMsg(\'' + agentId + '\')}">' +
          '<input type="file" id="agent-file-input-' + agentId + '" multiple accept="image/*,.pdf,.doc,.docx,.pptx,.xlsx,.xls,.txt,.csv,.json,.yaml,.yml,.md,.py,.js,.ts,.html,.css" style="display:none" onchange="handleAgentAttach(\'' + agentId + '\',this)">' +
          '<button style="background:none;border:none;padding:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;border-radius:6px;transition:background .15s" onclick="document.getElementById(\'agent-file-input-' + agentId + '\').click()" title="上传图片/文件"><span class="material-symbols-outlined" style="font-size:20px;color:var(--text3)">attach_file</span></button>' +
          '<button id="stt-btn-' + agentId + '" style="background:none;border:none;padding:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;border-radius:6px;transition:background .15s" onclick="_toggleSTT(\'' + agentId + '\')" title="语音输入 (STT Microphone)"><span class="material-symbols-outlined" style="font-size:20px;color:var(--text3)">mic</span></button>' +
          '<button style="background:var(--primary);border:none;padding:10px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center" onclick="sendAgentMsg(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:20px;color:#282572">send</span></button>' +
        '</div>' +
      '</div>' +
    '</section>' +

    '<!-- Bottom Section: Task Execution + Logs -->' +
    '<section style="flex:1;overflow-y:auto;display:grid;grid-template-columns:12fr;gap:0">' +
      // --- Consolidated stat strip: 4 cards instead of 9 tofu blocks ---
      '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        // Card 1: Runtime — events / tasks / tokens / memory combined
        '<div style="background:var(--surface);border-radius:10px;padding:14px 16px;border:1px solid rgba(255,255,255,0.05)">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">' +
            '<span style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px">Runtime</span>' +
            '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">analytics</span>' +
          '</div>' +
          '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;font-size:11px">' +
            '<div><div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Events</div><div style="font-weight:700;font-size:15px;color:var(--text)">' + (ag.event_count||0) + '</div></div>' +
            '<div><div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Tasks</div><div style="font-weight:700;font-size:15px;color:var(--text)" id="agent-task-count-' + agentId + '">0</div></div>' +
            '<div title="LLM token usage (in / out)"><div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Tokens</div><div style="font-weight:600;font-size:11px;color:var(--text2)" id="agent-token-stats-' + agentId + '">— / —</div><div id="agent-token-calls-' + agentId + '" style="font-size:9px;color:var(--text3)">0 calls</div></div>' +
            '<div title="记忆占动态上下文比例"><div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Memory</div><div style="font-weight:600;font-size:11px;color:var(--text2)" id="agent-memory-ratio-' + agentId + '">—</div><div style="height:3px;background:rgba(255,255,255,0.06);border-radius:2px;margin-top:3px;overflow:hidden"><div id="agent-memory-bar-' + agentId + '" style="height:100%;width:0%;background:linear-gradient(90deg,#a78bfa,#7c5cfa);transition:width .3s"></div></div></div>' +
          '</div>' +
        '</div>' +
        // Card 2: Model + 专业领域
        '<div style="background:' + (ag.enhancement ? 'linear-gradient(135deg,var(--surface),rgba(203,201,255,0.08))' : 'var(--surface)') + ';border-radius:10px;padding:14px 16px;border:1px solid ' + (ag.enhancement ? 'rgba(203,201,255,0.3)' : 'rgba(255,255,255,0.05)') + ';cursor:pointer" onclick="showEnhancementPanel(\'' + agentId + '\')">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">' +
            '<span style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px">Model & 专业领域</span>' +
            '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">psychology</span>' +
          '</div>' +
          '<div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Model</div>' +
          '<div style="font-size:12px;font-weight:600;color:var(--text2);margin-bottom:8px;word-break:break-all">' + esc(ag.model||'default') + '</div>' +
          '<div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">专业领域</div>' +
          '<div style="font-size:12px;font-weight:600;color:' + (ag.enhancement ? 'var(--primary)' : 'var(--text3)') + '">' + (ag.enhancement ? (ag.enhancement.domain.split('+').length + ' loaded') : 'Off') + '</div>' +
        '</div>' +
        // Card 3: Capabilities (skills + MCPs + tools)
        '<div style="background:linear-gradient(135deg,var(--surface),rgba(167,139,250,0.08));border-radius:10px;padding:14px 16px;border:1px solid rgba(167,139,250,0.3);cursor:pointer" onclick="showSkillPanel(\'' + agentId + '\')">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">' +
            '<span style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px">Capabilities</span>' +
            '<span class="material-symbols-outlined" style="font-size:18px;color:#a78bfa">build_circle</span>' +
          '</div>' +
          (function(){
            // SKILLS column: authoritative granted skills from the
            // skill registry (defaults + third-party grants). We do
            // NOT use bound_prompt_packs here — those are prompt
            // enhancers, a separate capability class shown in the
            // detail dialog.
            var skills = (ag.granted_skills && ag.granted_skills.length) || 0;
            var mcps = (ag.mcp_servers && ag.mcp_servers.length) || 0;
            // TOOLS column: tools GAINED from bound MCPs (not the
            // full runtime pool of ~180 builtins). Server-side,
            // agent.to_dict() already filters this correctly.
            var tools = (ag.tools && ag.tools.length) || 0;
            return '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:11px">' +
              '<div><div style="color:var(--text3);font-size:9px;text-transform:uppercase">Skills</div><div style="font-weight:700;font-size:14px;color:#a78bfa">' + skills + '</div></div>' +
              '<div><div style="color:var(--text3);font-size:9px;text-transform:uppercase">MCPs</div><div style="font-weight:700;font-size:14px;color:#a78bfa">' + mcps + '</div></div>' +
              '<div><div style="color:var(--text3);font-size:9px;text-transform:uppercase">Tools</div><div style="font-weight:700;font-size:14px;color:#a78bfa">' + tools + '</div></div>' +
            '</div>';
          })() +
        '</div>' +
        // Card 4: Analysis + Role Growth (closed-loop feedback card)
        '<div style="background:linear-gradient(135deg,var(--surface),rgba(52,211,153,0.08));border-radius:10px;padding:14px 16px;border:1px solid rgba(52,211,153,0.3);display:flex;flex-direction:column;gap:8px">' +
          '<div style="display:flex;align-items:center;justify-content:space-between">' +
            '<span style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px">Growth & Analysis</span>' +
            '<span class="material-symbols-outlined" style="font-size:18px;color:#34d399">insights</span>' +
          '</div>' +
          '<div style="display:flex;gap:10px;flex:1">' +
            '<div style="flex:1;cursor:pointer" onclick="showAnalysisPanel(\'' + agentId + '\')" title="Execution Analysis">' +
              '<div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Analysis</div>' +
              '<div style="font-weight:700;font-size:13px;color:#34d399">' + ((ag.recent_analyses&&ag.recent_analyses.length) ? (ag.recent_analyses.length + ' recent') : 'Auto') + '</div>' +
            '</div>' +
            '<div style="flex:1;cursor:pointer" onclick="showGrowthPathPanel(\'' + agentId + '\')" title="Role Growth Path">' +
              '<div style="color:var(--text3);font-size:9px;text-transform:uppercase;letter-spacing:0.5px">Role Growth</div>' +
              '<div style="font-weight:700;font-size:13px;color:rgb(251,146,60)">' + (ag.growth_path ? (ag.growth_path.current_stage + ' ' + ag.growth_path.overall_progress + '%') : 'Init') + '</div>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>' +

      '<div style="display:grid;grid-template-columns:3fr 3fr 3fr 3fr;gap:0;flex:1;overflow:hidden">' +
        '<!-- Tasks -->' +
        '<div style="border-right:1px solid rgba(255,255,255,0.05);padding:16px 20px;overflow-y:auto">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px"><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text)">Task Queue</span><button class="btn btn-sm btn-ghost" onclick="addTaskDialog(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:14px">add</span> Add</button></div>' +
          '<div id="tasks-list-' + agentId + '" style="display:flex;flex-direction:column;gap:4px"></div>' +
        '</div>' +
        '<!-- Event Log -->' +
        '<div style="padding:16px 20px;overflow-y:auto;background:var(--bg);border-right:1px solid rgba(255,255,255,0.05)">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.05)"><div style="display:flex;align-items:center;gap:6px;color:var(--primary)"><span class="material-symbols-outlined" style="font-size:14px">terminal</span><span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px">Execution Log</span></div></div>' +
          '<div id="agent-event-log-' + agentId + '" style="font-family:monospace;font-size:11px;line-height:1.7;color:var(--text3)"></div>' +
        '</div>' +
        '<!-- Execution Steps Panel (like Claude Todo) -->' +
        '<div style="padding:16px 16px;overflow-y:auto;background:var(--surface);border-right:1px solid rgba(255,255,255,0.05)">' +
          '<div style="display:flex;align-items:center;gap:6px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.05)">' +
            '<span class="material-symbols-outlined" style="font-size:16px;color:var(--primary)">checklist</span>' +
            '<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text)">Execution Steps</span>' +
          '</div>' +
          '<div id="execution-steps-' + agentId + '" style="display:flex;flex-direction:column;gap:0">' +
            '<div style="color:var(--text3);font-size:12px;padding:8px 0">Waiting for agent to start a task...</div>' +
          '</div>' +
        '</div>' +
        '<!-- Inter-Agent Messages -->' +
        '<div style="padding:16px 20px;overflow-y:auto;background:var(--bg)">' +
          '<div style="display:flex;align-items:center;gap:6px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,0.05)">' +
            '<span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">mail</span>' +
            '<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text)">Agent Messages</span>' +
          '</div>' +
          '<div id="inter-agent-msgs-' + agentId + '" style="display:flex;flex-direction:column;gap:6px;font-size:12px"></div>' +
        '</div>' +
      '</div>' +
    '</section>' +

    '<!-- Workspace Info -->' +
    '<section style="padding:12px 20px;background:var(--surface2);border-top:1px solid rgba(255,255,255,0.05);font-size:12px;color:var(--text2)">' +
      '<div><span style="color:var(--text3)">Private Workspace:</span> <code style="color:var(--primary)">~/.tudou_claw/workspaces/' + esc(agentId) + '</code></div>' +
      (ag.shared_workspace ? '<div style="margin-top:4px"><span style="color:var(--text3)">Shared Workspace:</span> <code style="color:var(--primary)">' + esc(ag.shared_workspace) + '</code></div>' : '') +
    '</section>';
  loadAgentChat(agentId).then(function() {
    // After history is rendered, attach file cards to historical
    // bubbles by matching filenames mentioned in their text.
    try { attachHistoricalFileCards(agentId); } catch(e) {}
  });
  loadTasks(agentId);
  loadAgentEventLog(agentId);
  loadExecutionSteps(agentId);
  loadInterAgentMessages(agentId);
  populateQuickModelSwitch(agentId);
  loadAgentRuntimeStats(agentId);
  // 周期刷新 token / memory 统计（每 8 秒一次）
  if (window._agentRuntimeStatsTimer) clearInterval(window._agentRuntimeStatsTimer);
  window._agentRuntimeStatsTimer = setInterval(function(){
    loadAgentRuntimeStats(agentId);
  }, 8000);
  // Reconnect to active task stream if agent is busy
  _reconnectActiveStream(agentId);
}

// Load event log for agent bottom panel
async function loadAgentEventLog(agentId) {
  try {
    var data = await api('GET', '/api/portal/agent/' + agentId + '/events');
    if (!data) return;
    var el = document.getElementById('agent-event-log-' + agentId);
    if (!el) return;
    var events = (data.events || []).slice(-30).reverse();
    el.innerHTML = events.map(function(e) {
      var color = e.kind==='message'?'var(--primary)':e.kind==='tool_call'?'var(--warning)':e.kind==='tool_result'?'var(--success)':e.kind==='error'?'var(--error)':'var(--text3)';
      var time = new Date(e.timestamp*1000).toLocaleTimeString();
      var content = JSON.stringify(e.data||{}).slice(0,120);
      return '<p><span style="color:rgba(203,201,255,0.3)">[' + time + ']</span> <span style="color:' + color + '">' + esc(e.kind).toUpperCase() + ':</span> ' + esc(content) + '</p>';
    }).join('');
    // Update task count
    var countEl = document.getElementById('agent-task-count-' + agentId);
    if (countEl) {
      var taskData = await api('GET', '/api/portal/agent/' + agentId + '/tasks');
      if (taskData) countEl.textContent = (taskData.tasks||[]).length;
    }
  } catch(e) {}
}

// Load inter-agent messages
async function loadInterAgentMessages(agentId) {
  try {
    var data = await api('GET', '/api/portal/agent/' + agentId + '/events');
    if (!data) return;
    var el = document.getElementById('inter-agent-msgs-' + agentId);
    if (!el) return;

    // Filter for inter-agent messages (message_from_agent events)
    var interAgentEvents = (data.events || []).filter(function(e) {
      return e.kind === 'inter_agent_message' || (e.kind === 'message' && e.data && e.data.from_agent);
    });

    if (interAgentEvents.length === 0) {
      el.innerHTML = '<div style="color:var(--text3);font-size:11px">No inter-agent messages</div>';
      return;
    }

    el.innerHTML = interAgentEvents.slice(-20).reverse().map(function(e) {
      var fromAgent = (e.data && e.data.from_agent_name) || agentName((e.data && e.data.from_agent) || '') || 'Unknown Agent';
      var content = (e.data && e.data.content) || JSON.stringify(e.data||{});
      var time = new Date(e.timestamp * 1000).toLocaleTimeString();
      var msgType = (e.data && e.data.msg_type) || 'message';
      var typeLabel = msgType === 'task' ? '📋' : msgType === 'request' ? '❓' : '💬';

      return '<div style="background:var(--surface);padding:8px;border-radius:6px;border-left:2px solid var(--primary)">' +
        '<div style="font-size:10px;color:var(--primary);font-weight:700">' + typeLabel + ' ' + esc(fromAgent) + '</div>' +
        '<div style="font-size:11px;color:var(--text);margin-top:2px;word-wrap:break-word">' + esc(content.slice(0, 100)) + (content.length > 100 ? '...' : '') + '</div>' +
        '<div style="font-size:9px;color:var(--text3);margin-top:2px">' + time + '</div>' +
      '</div>';
    }).join('');
  } catch(e) {}
}

// Load workspace access controls
async function loadWorkspaceAccess(agentId) {
  try {
    var data = await api('POST', '/api/portal/agent/workspace/list', { agent_id: agentId });
    if (!data) return;

    // Load all agents to populate the dropdown
    var allAgents = await api('GET', '/api/portal/agents');
    if (!allAgents || !allAgents.agents) return;

    var selectEl = document.getElementById('ws-auth-select-' + agentId);
    if (!selectEl) return;

    // Populate dropdown with other agents
    var otherAgents = allAgents.agents.filter(function(a) { return a.id !== agentId; });
    selectEl.innerHTML = '<option value="">-- Select Agent --</option>' +
      otherAgents.map(function(a) {
        return '<option value="' + esc(a.id) + '">' + esc(a.name) + '</option>';
      }).join('');

    // Display authorized workspaces list
    var listEl = document.getElementById('authorized-ws-list-' + agentId);
    if (!listEl) return;

    var auth = data.authorized_workspaces || [];
    if (auth.length === 0) {
      listEl.innerHTML = '<div style="color:var(--text3);font-size:11px;margin-bottom:8px">No authorized workspaces</div>';
    } else {
      listEl.innerHTML = '<div style="margin-bottom:8px">' +
        auth.map(function(otherId) {
          var otherAgent = allAgents.agents.find(function(a) { return a.id === otherId; });
          var name = otherAgent ? otherAgent.name : otherId;
          return '<div style="display:flex;align-items:center;justify-content:space-between;padding:6px;background:var(--bg);border-radius:4px;margin-bottom:4px;border:1px solid rgba(255,255,255,0.05)">' +
            '<span style="font-size:11px;color:var(--text)">' + esc(name) + '</span>' +
            '<button style="cursor:pointer;padding:2px 8px;font-size:10px;background:rgba(255,0,0,0.1);border:1px solid rgba(255,0,0,0.3);color:#ff6b6b;border-radius:3px" onclick="revokeWorkspace(\'' + esc(agentId) + '\', \'' + esc(otherId) + '\')">' +
              'Revoke' +
            '</button>' +
          '</div>';
        }).join('') +
      '</div>';
    }
  } catch(e) {
    console.error('Error loading workspace access:', e);
  }
}

async function authorizeWorkspace(agentId) {
  var sel = document.getElementById('ws-auth-select-' + agentId);
  var targetId = sel ? sel.value : '';
  if (!targetId) return;
  try {
    await api('POST', '/api/portal/agent/workspace/authorize', {
      agent_id: agentId,
      target_agent_id: targetId
    });
    loadWorkspaceAccess(agentId);
  } catch(e) {
    console.error('Error authorizing workspace:', e);
  }
}

async function revokeWorkspace(agentId, targetId) {
  try {
    await api('POST', '/api/portal/agent/workspace/revoke', {
      agent_id: agentId,
      target_agent_id: targetId
    });
    loadWorkspaceAccess(agentId);
  } catch(e) {
    console.error('Error revoking workspace:', e);
  }
}

// ---- Execution Steps Panel ----
var _stepsTimers = {};
var _stepsLoading = {};  // Guard against concurrent loadExecutionSteps calls

function _clearAllStepsTimers() {
  // Clean up ALL execution step timers (call when navigating away from agent view)
  Object.keys(_stepsTimers).forEach(function(k) {
    clearInterval(_stepsTimers[k]);
    delete _stepsTimers[k];
  });
  _stepsLoading = {};
}

async function loadExecutionSteps(agentId) {
  // Prevent duplicate concurrent calls for the same agent
  if (_stepsLoading[agentId]) return;
  _stepsLoading[agentId] = true;

  // Clear any existing timer for this agent first
  if (_stepsTimers[agentId]) {
    clearInterval(_stepsTimers[agentId]);
    delete _stepsTimers[agentId];
  }

  try {
    var data = await api('GET', '/api/portal/agent/' + agentId + '/plans');
    if (!data) { _stepsLoading[agentId] = false; return; }
    renderExecutionSteps(agentId, data.current_plan);
    // Only start polling if plan is not completed
    if (data.current_plan && data.current_plan.status === 'completed') {
      _stepsLoading[agentId] = false;
      return;
    }
  } catch(e) { _stepsLoading[agentId] = false; return; }

  _stepsLoading[agentId] = false;

  // Poll every 3s while agent is busy — only for the CURRENTLY viewed agent
  if (currentView !== 'agent' || currentAgent !== agentId) return;
  _stepsTimers[agentId] = setInterval(async function() {
    // Stop polling if we navigated away from this agent
    if (currentView !== 'agent' || currentAgent !== agentId) {
      clearInterval(_stepsTimers[agentId]);
      delete _stepsTimers[agentId];
      return;
    }
    var ag = agents.find(function(a){ return a.id === agentId; });
    if (!ag) return;
    try {
      var d = await api('GET', '/api/portal/agent/' + agentId + '/plans');
      if (!d || !d.current_plan) return;
      renderExecutionSteps(agentId, d.current_plan);
      // Stop polling if plan is completed
      if (d.current_plan.status === 'completed') {
        clearInterval(_stepsTimers[agentId]);
        delete _stepsTimers[agentId];
      }
    } catch(e) {}
  }, 3000);
}

function renderExecutionSteps(agentId, plan) {
  var el = document.getElementById('execution-steps-' + agentId);
  if (!el) return;
  if (!plan || !plan.steps || plan.steps.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px 0;display:flex;align-items:center;gap:6px"><span class="material-symbols-outlined" style="font-size:16px">hourglass_empty</span>Waiting for agent to start a task...</div>';
    return;
  }

  var progress = plan.progress || {};
  var html = '';

  // Task summary header
  html += '<div style="font-size:12px;color:var(--text2);font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:6px">';
  html += '<span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">task</span>';
  html += esc(plan.task_summary || 'Task').slice(0,60);
  html += '</div>';

  // Steps — show completed ones compactly, active one prominently, hide pending
  var hasActive = false;
  plan.steps.forEach(function(step, idx) {
    var isCompleted = step.status === 'completed';
    var isInProgress = step.status === 'in_progress';
    var isFailed = step.status === 'failed';
    var isSkipped = step.status === 'skipped';
    var isPending = !isCompleted && !isInProgress && !isFailed && !isSkipped;

    if (isCompleted) {
      // Compact completed step
      html += '<div style="display:flex;align-items:center;gap:6px;padding:3px 0;animation:fadeInUp 0.3s ease">';
      html += '<span class="material-symbols-outlined" style="font-size:14px;color:var(--success)">check_circle</span>';
      html += '<span style="font-size:11px;color:var(--text3);text-decoration:line-through">' + esc(step.title) + '</span>';
      html += '</div>';
    } else if (isInProgress) {
      hasActive = true;
      // Prominent active step with working indicator
      html += '<div class="working-indicator" style="animation:fadeInUp 0.3s ease;margin:6px 0">';
      html += '<span class="wi-dot"></span>';
      html += '<div style="flex:1;min-width:0">';
      html += '<div class="wi-text">' + esc(step.title) + '</div>';
      if (step.result_summary) {
        html += '<div style="font-size:10px;color:var(--text3);margin-top:2px">' + esc(step.result_summary).slice(0,80) + '</div>';
      }
      html += '</div>';
      html += '</div>';
    } else if (isFailed) {
      html += '<div style="display:flex;align-items:center;gap:6px;padding:3px 0">';
      html += '<span class="material-symbols-outlined" style="font-size:14px;color:var(--error)">error</span>';
      html += '<span style="font-size:11px;color:var(--error);text-decoration:line-through">' + esc(step.title) + '</span>';
      html += '</div>';
    } else if (isSkipped) {
      html += '<div style="display:flex;align-items:center;gap:6px;padding:3px 0">';
      html += '<span class="material-symbols-outlined" style="font-size:14px;color:var(--text3)">skip_next</span>';
      html += '<span style="font-size:11px;color:var(--text3);text-decoration:line-through">' + esc(step.title) + '</span>';
      html += '</div>';
    }
    // Pending steps are hidden — only show next step count
  });

  // Show remaining pending count
  var pendingCount = plan.steps.filter(function(s){ return s.status !== 'completed' && s.status !== 'in_progress' && s.status !== 'failed' && s.status !== 'skipped'; }).length;
  if (pendingCount > 0 && plan.status !== 'completed') {
    html += '<div style="font-size:10px;color:var(--text3);padding:4px 0;margin-top:2px;display:flex;align-items:center;gap:4px">';
    html += '<span class="material-symbols-outlined" style="font-size:12px">more_horiz</span>';
    html += pendingCount + ' more step' + (pendingCount>1?'s':'') + ' remaining';
    html += '</div>';
  }

  // Completion message
  if (plan.status === 'completed') {
    html += '<div style="margin-top:8px;padding:8px 12px;background:rgba(76,175,80,0.08);border-radius:8px;display:flex;align-items:center;gap:6px;animation:fadeInUp 0.3s ease">';
    html += '<span class="material-symbols-outlined" style="font-size:16px;color:var(--success)">task_alt</span>';
    html += '<span style="font-size:12px;color:var(--success);font-weight:600">All steps completed!</span>';
    html += '</div>';
  }

  el.innerHTML = html;
}

function populateQuickModelSwitch(agentId) {
  var ag = agents.find(function(a){ return a.id === agentId; }) || {};
  var provEl = document.getElementById('agent-quick-provider-' + agentId);
  var modelEl = document.getElementById('agent-quick-model-' + agentId);
  if (!provEl || !modelEl) return;

  // Populate provider select - use _providerList for names, _availableModels for models
  // Build combined list: all providers from _providerList + any extra keys in _availableModels
  var provOptions = [];
  var seenIds = {};
  (_providerList || []).forEach(function(prov) {
    seenIds[prov.id] = true;
    provOptions.push({id: prov.id, name: prov.name, kind: prov.kind});
  });
  Object.keys(_availableModels).forEach(function(k) {
    if (!seenIds[k]) provOptions.push({id: k, name: k, kind: k});
  });
  // Match agent's current provider (could be id, kind, or name)
  var agProv = ag.provider || _defaultProvider;
  provEl.innerHTML = provOptions.map(function(p) {
    var selected = (agProv === p.id || agProv === p.kind || agProv === p.name) ? ' selected' : '';
    return '<option value="' + esc(p.id) + '"' + selected + '>' + esc(p.name) + '</option>';
  }).join('');

  // Populate model select based on selected provider
  var selProvider = provEl.value || _defaultProvider;
  var models = _availableModels[selProvider] || [];
  // Also try matching by kind if no models found by id
  if (models.length === 0) {
    var matchProv = provOptions.find(function(p){ return p.id === selProvider; });
    if (matchProv) {
      Object.keys(_availableModels).forEach(function(k) {
        if (k === matchProv.kind && _availableModels[k].length > 0) models = _availableModels[k];
      });
    }
  }
  modelEl.innerHTML = models.map(function(m) {
    return '<option value="' + esc(m) + '"' + (ag.model === m ? ' selected' : '') + '>' + esc(m) + '</option>';
  }).join('');

  // On provider change, update models
  provEl.onchange = function() {
    var pid = provEl.value;
    var ms = _availableModels[pid] || [];
    modelEl.innerHTML = ms.map(function(m) {
      return '<option value="' + esc(m) + '">' + esc(m) + '</option>';
    }).join('');
    quickSwitchModel(agentId);
  };
}

async function quickSwitchModel(agentId) {
  var provEl = document.getElementById('agent-quick-provider-' + agentId);
  var modelEl = document.getElementById('agent-quick-model-' + agentId);
  if (!provEl || !modelEl) return;
  var provider = provEl.value;
  var model = modelEl.value;
  await api('POST', '/api/portal/agent/' + agentId + '/profile', { provider: provider, model: model });
  // Update local agent data
  var ag = agents.find(function(a){ return a.id === agentId; });
  if (ag) { ag.provider = provider; ag.model = model; }
  // Update the model stat card
  var modelCard = document.getElementById('content');
  if (modelCard) {
    var spans = modelCard.querySelectorAll('[id^="agent-task-count"]');
  }
}

async function loadAgentChat(agentId, _retryCount) {
  _retryCount = _retryCount || 0;
  try {
    const data = await api('GET', `/api/portal/agent/${agentId}/events`);
    if (!data) return;
    const el = document.getElementById('chat-msgs-'+agentId);
    if(!el) return;
    // If chat already has REAL messages (not just thinking bubbles), preserve them.
    const hasMessages = el.querySelectorAll('.chat-msg:not(.thinking), .chat-msg-row').length > 0;

    // If API returned empty/error events but chat already has messages, preserve them
    if (!data.events || data.events.length === 0) {
      if (hasMessages) {
        return;  // Preserve existing messages
      }
      // Agent might be busy — retry once after a short delay to allow
      // events to become available (thread-safety / timing edge case).
      var ag = (window._cachedAgents || []).find(function(a){ return a.id === agentId; });
      if (ag && ag.status === 'busy' && _retryCount < 2) {
        await new Promise(function(r){ setTimeout(r, 800); });
        return loadAgentChat(agentId, _retryCount + 1);
      }
      // Empty API response and no existing messages - clear the chat
      el.innerHTML = '';
      return;
    }

    if (!hasMessages) {
      el.innerHTML = '';
      for(const evt of (data.events||[])) {
        if(evt.kind==='message') {
          const role = evt.data.role||'assistant';
          if(role==='system') continue;
          var content = evt.data.content||'';
          // Skip empty assistant messages (intermediate tool-call turns with no text)
          if(role==='assistant' && !content.trim()) continue;
          addChatBubble(agentId, role, content, evt.timestamp||0);
        }
        // tool_call and tool_result are hidden from chat — only shown in Execution Log
      }
    }
  } catch(e) {
    // On error, retry once if agent is busy
    if (_retryCount < 1) {
      await new Promise(function(r){ setTimeout(r, 500); });
      return loadAgentChat(agentId, _retryCount + 1);
    }
  }
}

// Open an image in a fullscreen lightbox overlay. Click anywhere to close.
function _openImagePreview(src) {
  if (!src) return;
  try {
    var box = document.createElement('div');
    box.className = 'chat-img-lightbox';
    var img = document.createElement('img');
    img.src = src;
    box.appendChild(img);
    box.addEventListener('click', function(){ box.remove(); });
    document.body.appendChild(box);
  } catch(e) {}
}

// Rewrite a raw path or URL into an inline-viewable image src.
// - http/https URLs → pass through
// - data: URIs → pass through
// - anything else is treated as a local file under the agent's workspace
//   and routed through /api/portal/attachment for path-whitelisted serving.
function _resolveImageSrc(raw, agentId) {
  if (!raw) return '';
  var url = String(raw).trim();
  if (!url) return '';
  if (/^(https?:)?\/\//i.test(url) || /^data:/i.test(url)) return url;
  // Strip `file://` prefix if the model emitted one
  url = url.replace(/^file:\/\//i, '');
  // Strip `attachment://` (we also accept this shorthand)
  url = url.replace(/^attachment:\/\//i, '');
  var qs = 'path=' + encodeURIComponent(url);
  if (agentId) qs += '&agent_id=' + encodeURIComponent(agentId);
  return '/api/portal/attachment?' + qs;
}

// Simple markdown → HTML renderer for assistant messages
function _renderSimpleMarkdown(text, agentId) {
  if (!text) return '';
  // Trim leading/trailing whitespace and collapse 3+ consecutive newlines
  // to 2 so a spammy model can't inflate bubble height with empty lines.
  var s = String(text).replace(/^\s+|\s+$/g, '').replace(/\n{3,}/g, '\n\n');
  if (!s) return '';
  // Escape HTML first
  s = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Code blocks: ```lang\n...\n```
  s = s.replace(/```(\w*)\n([\s\S]*?)```/g, function(m,lang,code){
    return '<pre><code class="lang-'+lang+'">' + code + '</code></pre>';
  });
  // Inline code: `...`
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // Headers: ### text (process before inline formatting)
  s = s.replace(/^### (.+)$/gm, '<strong style="font-size:1.05em">$1</strong>');
  s = s.replace(/^## (.+)$/gm, '<strong style="font-size:1.1em">$1</strong>');
  s = s.replace(/^# (.+)$/gm, '<strong style="font-size:1.15em">$1</strong>');
  // Unordered list items: - text (process before bold/italic)
  s = s.replace(/^- (.+)$/gm, '<li>$1</li>');
  // Ordered list items: 1. text
  s = s.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
  // Bold: **text**
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Italic: *text* (bold already converted, so remaining single * are italic)
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  // Images: ![alt](path) — MUST run before the link regex, otherwise ![..](..)
  // would be matched as a link and the leading ! stranded.
  //
  // Two paths:
  //   1. src looks like an actual image (png/jpg/gif/svg/webp/bmp/avif)
  //      → render as <img>, rewrite local paths via _resolveImageSrc
  //   2. src looks like a non-image binary (mp4/mp3/pdf/docx/zip/...)
  //      → DROP the image syntax entirely. The agent has been told via
  //        <file_display> not to write this, but legacy / mistaken
  //        outputs would otherwise show as a permanent broken image.
  //        The corresponding FileCard will appear via the artifact_refs
  //        envelope (live) or attachHistoricalFileCards (history).
  s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function(m, alt, src){
    var rawSrc = String(src || '').trim();
    var lower = rawSrc.toLowerCase().split('?')[0].split('#')[0];
    var imageExts = ['.png','.jpg','.jpeg','.gif','.svg','.webp','.bmp','.avif','.ico'];
    var isImage = false;
    for (var i = 0; i < imageExts.length; i++) {
      if (lower.endsWith(imageExts[i])) { isImage = true; break; }
    }
    // Also accept data: URLs and obvious http image paths even without ext
    if (!isImage && /^data:image\//i.test(rawSrc)) isImage = true;
    if (!isImage) {
      // Drop the broken image syntax — emit nothing (FileCard handles it).
      return '';
    }
    var url = _resolveImageSrc(rawSrc, agentId);
    var safeAlt = String(alt || '').replace(/"/g, '&quot;');
    return '<img class="chat-inline-img" src="' + url + '" alt="' + safeAlt +
           '" loading="lazy" onclick="_openImagePreview(this.src)" '+
           'onerror="this.onerror=null;this.classList.add(\'chat-inline-img-err\');this.alt=\'[image not found: \'+(this.alt||\'\')+\']\';">';
  });
  // Links: [text](url)
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Wrap consecutive <li> in <ul>
  s = s.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
  // Line breaks (but not inside pre)
  s = s.replace(/\n/g, '<br>');
  // Clean up <br> inside pre/code blocks
  s = s.replace(/<pre><code([^>]*)>([\s\S]*?)<\/code><\/pre>/g, function(m,cls,code){
    return '<pre><code'+cls+'>' + code.replace(/<br>/g, '\n') + '</code></pre>';
  });
  // Clean up <br> inside <ul>
  s = s.replace(/<ul>([\s\S]*?)<\/ul>/g, function(m,inner){
    return '<ul>' + inner.replace(/<br>/g, '') + '</ul>';
  });
  // Wrap [File: ...] blocks in collapsible containers
  s = _wrapFileBlocks(s);
  return s;
}

/**
 * Detect [File: filename] blocks in HTML and wrap them in collapsible containers.
 * Works on already-rendered HTML (after markdown → HTML conversion).
 */
function _wrapFileBlocks(html) {
  // Match: [File: filename]<br>content... up to the next [File: or end of string
  return html.replace(/\[File:\s*([^\]]+)\](?:<br\s*\/?>)([\s\S]*?)(?=\[File:\s*[^\]]+\](?:<br)|$)/gi, function(m, fname, body) {
    if (!body || !body.trim()) {
      return '<strong>[File: ' + fname.trim() + ']</strong>';
    }
    var uid = 'fb-' + Math.random().toString(36).substr(2, 8);
    var brCount = (body.match(/<br\s*\/?>/gi) || []).length;
    if (brCount < 8 && body.length < 500) {
      return '<div class="chat-file-block">' +
        '<div class="chat-file-block-header"><span class="material-symbols-outlined">description</span>' + fname.trim() + '</div>' +
        '<div class="chat-file-block-body expanded">' + body + '</div></div>';
    }
    return '<div class="chat-file-block">' +
      '<div class="chat-file-block-header" onclick="_toggleFileBlock(\'' + uid + '\')"><span class="material-symbols-outlined">description</span>' + fname.trim() + '</div>' +
      '<div id="' + uid + '" class="chat-file-block-body collapsed">' + body + '</div>' +
      '<div class="chat-file-block-toggle" onclick="_toggleFileBlock(\'' + uid + '\')">Show more · 展开全部</div></div>';
  });
}

/** Toggle file content block expand/collapse */
function _toggleFileBlock(uid) {
  var el = document.getElementById(uid);
  if (!el) return;
  var toggle = el.nextElementSibling;
  if (el.classList.contains('collapsed')) {
    el.classList.remove('collapsed');
    el.classList.add('expanded');
    if (toggle) toggle.textContent = 'Show less · 收起';
  } else {
    el.classList.remove('expanded');
    el.classList.add('collapsed');
    if (toggle) toggle.textContent = 'Show more · 展开全部';
  }
}

// Format a unix-epoch timestamp (seconds) into a localized time string.
// If the date is today, show only HH:MM; otherwise show MM-DD HH:MM.
function _formatChatTime(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var now = new Date();
  var hh = String(d.getHours()).padStart(2, '0');
  var mm = String(d.getMinutes()).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) {
    return hh + ':' + mm;
  }
  var mon = String(d.getMonth() + 1).padStart(2, '0');
  var day = String(d.getDate()).padStart(2, '0');
  return mon + '-' + day + ' ' + hh + ':' + mm;
}

function addChatBubble(agentId, role, text, timestamp) {
  const el = document.getElementById('chat-msgs-'+agentId);
  if(!el) return;
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;

  // Build time label (shared by both user and assistant bubbles)
  // timestamp=0 means "streaming, will be set later"; undefined/null means "use now"
  var ts = (typeof timestamp === 'number') ? timestamp : (Date.now() / 1000);
  var timeLabel = _formatChatTime(ts);

  // For assistant messages, wrap in row: [robot avatar] [bubble]
  if (role.indexOf('assistant') !== -1 && role.indexOf('thinking') === -1) {
    var row = document.createElement('div');
    row.className = 'chat-msg-row';
    // Robot avatar on left
    var robotSrc = _getAgentRobotSrc(agentId);
    var avatarImg = document.createElement('img');
    avatarImg.className = 'chat-msg-avatar';
    avatarImg.src = robotSrc;
    avatarImg.onerror = function(){ this.outerHTML='<span class="material-symbols-outlined chat-msg-avatar" style="font-size:28px;color:var(--primary);background:var(--surface3);display:flex;align-items:center;justify-content:center">smart_toy</span>'; };
    row.appendChild(avatarImg);
    // Bubble content
    var contentDiv = document.createElement('div');
    contentDiv.className = 'chat-msg-content';
    // Use markdown rendering for history-loaded text, plain for streaming (updated later)
    if (text) {
      contentDiv.innerHTML = _renderSimpleMarkdown(text, agentId);
    }
    // Store raw text for copy/TTS
    contentDiv._rawText = text || '';
    // Store timestamp on the content div for later update (streaming)
    contentDiv._timestamp = ts;
    div.appendChild(contentDiv);
    // Time stamp label
    var timeSpan = document.createElement('span');
    timeSpan.className = 'chat-msg-time';
    timeSpan.textContent = timeLabel;
    timeSpan.style.cssText = 'display:block;font-size:11px;color:var(--text3,#999);margin-top:2px;';
    div.appendChild(timeSpan);
    var actionBar = document.createElement('div');
    actionBar.className = 'chat-msg-actions';
    actionBar.innerHTML = '<button class="chat-action-btn" onclick="_speakBubble(this)" title="朗读此消息"><span class="material-symbols-outlined" style="font-size:14px">volume_up</span></button>' +
      '<button class="chat-action-btn" onclick="_saveToFile(this,\''+agentId+'\')" title="Save to file"><span class="material-symbols-outlined" style="font-size:14px">save</span> Save</button>' +
      '<button class="chat-action-btn" onclick="_copyToClipboard(this)" title="Copy"><span class="material-symbols-outlined" style="font-size:14px">content_copy</span></button>';
    div.appendChild(actionBar);
    row.appendChild(div);
    div._contentDiv = contentDiv;
    el.appendChild(row);
    el.scrollTop = el.scrollHeight;
    return contentDiv;
  } else {
    // User message bubble — use collapsible rendering if file content present
    if (text && /\[File:\s*[^\]]+\]/.test(text)) {
      var escaped = String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
      div.innerHTML = _wrapFileBlocks(escaped);
      div.style.whiteSpace = 'normal';
    } else {
      div.textContent = text;
    }
    var timeSpan = document.createElement('span');
    timeSpan.className = 'chat-msg-time';
    timeSpan.textContent = timeLabel;
    timeSpan.style.cssText = 'display:block;font-size:11px;color:var(--text3,#999);margin-top:2px;text-align:right;';
    div.appendChild(timeSpan);
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
    return div;
  }
}

// ----- artifact_refs FileCard renderer (phase-2 envelope injection) -----
// Maps an artifact `kind` to a Material Symbols icon name. Used for
// every type except "image", which gets a real thumbnail in the icon
// slot instead.
function _fileCardIconName(ref) {
  var kind = (ref && ref.kind) || '';
  var cat  = (ref && ref.category) || '';
  if (kind === 'image') return 'image';
  if (kind === 'video') return 'movie';
  if (kind === 'audio') return 'music_note';
  if (kind === 'archive') return 'folder_zip';
  if (kind === 'document') {
    if (cat === 'pdf') return 'picture_as_pdf';
    if (cat === 'spreadsheet') return 'table_view';
    if (cat === 'presentation') return 'slideshow';
    if (cat === 'office') return 'description';
    if (cat === 'text' || cat === 'code') return 'description';
    return 'description';
  }
  return 'draft';
}

function _formatFileSize(n) {
  if (n == null || n < 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
}

function _renderFileCard(ref) {
  var url = ref.url || '';
  var name = ref.filename || ref.label || 'file';
  var hint = ref.render_hint || 'card';
  var sizeStr = _formatFileSize(ref.size);
  var metaBits = [];
  if (ref.kind) metaBits.push(ref.kind);
  if (sizeStr) metaBits.push(sizeStr);
  // anchor — let the browser's Content-Disposition decide
  // inline (new tab preview) vs attachment (download dialog)
  var a = document.createElement('a');
  a.className = 'chat-file-card';
  a.href = url;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.title = name;
  // icon slot — image kind gets a real thumbnail, everything else
  // gets a Material Symbols glyph
  var iconWrap = document.createElement('div');
  iconWrap.className = 'chat-file-card-icon';
  if (ref.kind === 'image' && url) {
    var img = document.createElement('img');
    img.src = url;
    img.alt = name;
    img.onerror = function() {
      // image fetch failed — fall back to icon glyph
      this.outerHTML = '<span class="material-symbols-outlined">image</span>';
    };
    iconWrap.appendChild(img);
  } else {
    var span = document.createElement('span');
    span.className = 'material-symbols-outlined';
    span.textContent = _fileCardIconName(ref);
    iconWrap.appendChild(span);
  }
  a.appendChild(iconWrap);
  // body — filename + meta
  var body = document.createElement('div');
  body.className = 'chat-file-card-body';
  var nm = document.createElement('div');
  nm.className = 'chat-file-card-name';
  nm.textContent = name;
  body.appendChild(nm);
  if (metaBits.length) {
    var meta = document.createElement('div');
    meta.className = 'chat-file-card-meta';
    meta.textContent = metaBits.join(' · ');
    body.appendChild(meta);
  }
  a.appendChild(body);
  // action glyph — visual hint for inline vs download
  var action = document.createElement('span');
  action.className = 'material-symbols-outlined chat-file-card-action';
  var inlineHints = ['inline_video', 'inline_image', 'inline_audio', 'inline_pdf'];
  action.textContent = inlineHints.indexOf(hint) >= 0 ? 'open_in_new' : 'download';
  a.appendChild(action);
  return a;
}

// Walk the chat history and attach file cards to historical bubbles
// using timestamp adjacency. The mental model: a file produced by a
// turn shows up as a card INSIDE that turn's bubble. For live turns
// the SSE artifact_refs envelope handles this. For historical turns
// (loaded from disk before shadow was wired up) we backfill by
// matching each file's mtime to the assistant turn whose timestamp
// is the closest match (preferring the latest assistant turn at or
// before the file's mtime).
//
// Why timestamp instead of filename match: agent prose often refers
// to a file by a Chinese/display name (e.g. "酱板鸭_升级版.mp4")
// while the actual file on disk has an English/codename basename
// (e.g. "jiangbanya_huili_v2.mp4"). Filename matching misses every
// such case. mtime matching is robust against renames and i18n.
//
// GET /api/portal/agent/<id>/files lazily installs shadow, rescans
// deliverable_dir, and returns every recognised file. Each file ref
// carries `produced_at` which scan_deliverable_dir sets to the file's
// mtime (not scan time). The bubbles' timestamps come from the same
// /events stream loadAgentChat consumes, so we re-fetch events here
// — cheap, cached server-side.
async function attachHistoricalFileCards(agentId) {
  if (!agentId) return;
  var panel = document.getElementById('chat-msgs-' + agentId);
  if (!panel) return;
  var data;
  try {
    data = await api('GET', '/api/portal/agent/' + agentId + '/files');
  } catch (e) {
    return;
  }
  if (!data) return;

  // Assistant bubbles in document order. The selector mirrors
  // loadAgentChat's filter (kind=message, role=assistant, non-empty
  // content), so bubbles[i] corresponds to the i-th non-empty assistant
  // turn in the event stream — i.e. the same `index` the backend uses
  // in /files response `turns[].index`.
  var bubbles = panel.querySelectorAll('.chat-msg.assistant .chat-msg-content');
  console.log('[fileCards] shadow=', data.shadow,
              'turns=', (data.turns || []).length,
              'orphans=', (data.orphans || []).length,
              'bubbles=', bubbles.length,
              'total_assistant_turns(server)=', data.total_assistant_turns);
  if (!bubbles.length) return;

  // Index-based deterministic attach (no timestamps, no heuristics).
  var turns = data.turns || [];
  var attached = 0;
  for (var i = 0; i < turns.length; i++) {
    var t = turns[i];
    if (typeof t.index !== 'number' || t.index < 0 || t.index >= bubbles.length) {
      console.log('[fileCards] turn index out of range', t.index);
      continue;
    }
    try {
      _appendFileCards(bubbles[t.index], t.refs || []);
      attached += (t.refs || []).length;
    } catch (e) {
      console.log('[fileCards] attach failed', e);
    }
  }

  // Orphans (files that exist in deliverable_dir but were never
  // mentioned in any tool_result OR assistant prose) are intentionally
  // NOT shown — dumping them on the last bubble pollutes the chat with
  // workspace meta files (Tasks.md, Project.md, ...). They remain
  // available via the persistent file list endpoint if needed.
  var orphans = data.orphans || [];
  console.log('[fileCards] attached', attached, 'cards across',
              turns.length, 'turns; ignored', orphans.length, 'orphans');
}

function _appendFileCards(msgDiv, refs) {
  if (!msgDiv || !refs || !refs.length) return;
  // Find or create the cards container next to the bubble text
  var container = msgDiv.querySelector(':scope > .chat-file-cards');
  if (!container) {
    container = document.createElement('div');
    container.className = 'chat-file-cards';
    msgDiv.appendChild(container);
  }
  // Dedup by ref.id — re-pushing the same envelope shouldn't double up
  var existing = {};
  Array.prototype.forEach.call(container.children, function(el) {
    var id = el.dataset && el.dataset.refId;
    if (id) existing[id] = true;
  });
  for (var i = 0; i < refs.length; i++) {
    var r = refs[i];
    if (!r || existing[r.id]) continue;
    var card = _renderFileCard(r);
    card.dataset.refId = r.id || '';
    container.appendChild(card);
    existing[r.id] = true;
  }
  // scroll the chat panel so the cards are visible
  var panel = msgDiv.closest('.chat-msgs') ||
              document.getElementById('chat-msgs-' + (msgDiv.dataset.agentId || ''));
  if (panel) panel.scrollTop = panel.scrollHeight;
}

function addApprovalBubble(agentId, evt) {
  const el = document.getElementById('chat-msgs-'+agentId);
  if(!el) return;
  const div = document.createElement('div');
  div.className = 'chat-msg approval';
  // Store approval_id directly on the DOM element for reliable lookup
  var approvalId = evt.approval_id || '';
  div.dataset.approvalId = approvalId;
  const tool = esc(evt.tool||'');
  const reason = esc(evt.reason||'Requires approval');
  const argsStr = esc(JSON.stringify(evt.arguments||{}).slice(0,200));
  div.innerHTML = '' +
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">' +
      '<span class="material-symbols-outlined" style="font-size:20px;color:var(--warning)">shield</span>' +
      '<span style="font-weight:700;font-size:13px;color:var(--warning)">Authorization Required</span>' +
    '</div>' +
    '<div style="font-size:13px;margin-bottom:6px"><strong>' + tool + '</strong></div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:4px">' + reason + '</div>' +
    (argsStr !== '{}' ? '<div style="font-size:10px;color:var(--text3);font-family:monospace;background:var(--bg);padding:6px 8px;border-radius:6px;margin-bottom:12px;word-break:break-all;max-height:60px;overflow:auto">' + argsStr + '</div>' : '') +
    '<div style="display:flex;gap:8px">' +
      '<button class="btn btn-sm btn-danger" onclick="chatApprovalAction(\'' + agentId + '\',\'deny\',this)" style="font-size:11px;padding:6px 12px"><span class="material-symbols-outlined" style="font-size:14px">block</span> Deny</button>' +
      '<button class="btn btn-sm btn-success" onclick="chatApprovalAction(\'' + agentId + '\',\'approve\',this)" style="font-size:11px;padding:6px 12px"><span class="material-symbols-outlined" style="font-size:14px">check</span> Approve</button>' +
      '<button class="btn btn-sm" onclick="chatApprovalAction(\'' + agentId + '\',\'approve_session\',this)" style="font-size:11px;padding:6px 12px;background:var(--primary);color:#0e141b"><span class="material-symbols-outlined" style="font-size:14px">verified</span> Approve (Session)</button>' +
    '</div>';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div;
}

function addLoginRequestBubble(agentId, evt) {
  const el = document.getElementById('chat-msgs-'+agentId);
  if(!el) return;
  const div = document.createElement('div');
  div.className = 'chat-msg login-request';
  var reqId = evt.request_id || '';
  div.dataset.requestId = reqId;
  div.dataset.loginUrl = evt.login_url || evt.url || '';
  div.dataset.siteName = evt.site_name || 'Website';
  div.dataset.agentId = agentId;
  var siteName = esc(evt.site_name||'Website');
  var url = esc(evt.url||'');
  var loginUrl = esc(evt.login_url || evt.url || '');
  var reason = esc(evt.reason||'Agent needs authenticated access');
  var iframeId = 'login-iframe-' + reqId.slice(0,8);
  div.innerHTML = '' +
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">' +
      '<span class="material-symbols-outlined" style="font-size:22px;color:#3b82f6">login</span>' +
      '<span style="font-weight:700;font-size:14px;color:#3b82f6">Login Required · 需要登录</span>' +
    '</div>' +
    '<div style="font-size:13px;margin-bottom:4px"><strong>' + siteName + '</strong></div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:8px">' + reason + '</div>' +
    '<div style="display:flex;gap:0;margin-bottom:8px;border-bottom:1px solid var(--border)">' +
      '<button class="login-tab active" data-tab="iframe" onclick="_switchLoginTab(this,\'iframe\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text);cursor:pointer;border-bottom:2px solid #3b82f6;font-weight:600">🌐 网页登录</button>' +
      '<button class="login-tab" data-tab="cred" onclick="_switchLoginTab(this,\'cred\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent">🔑 账号密码</button>' +
      '<button class="login-tab" data-tab="cookie" onclick="_switchLoginTab(this,\'cookie\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent">🍪 Cookie / Token</button>' +
    '</div>' +
    '<div class="login-panel" data-panel="iframe">' +
      '<div class="login-iframe-wrap">' +
        '<iframe id="' + iframeId + '" src="' + loginUrl + '" sandbox="allow-scripts allow-same-origin allow-forms allow-popups" referrerpolicy="no-referrer"></iframe>' +
        '<div class="login-iframe-blocked">' +
          '<span class="material-symbols-outlined">block</span>' +
          '该网站禁止在 iframe 中加载<br>' +
          '<a href="' + loginUrl + '" target="_blank" rel="noopener" style="color:#3b82f6;text-decoration:underline;font-size:13px;display:inline-block;margin-top:8px">↗ 在新标签页中打开登录</a><br>' +
          '<span style="font-size:11px;color:var(--text3);margin-top:6px;display:inline-block">登录完成后，回到此页面点击下方「✓ 已登录完成」</span>' +
        '</div>' +
      '</div>' +
      '<div style="display:flex;gap:8px;align-items:center;margin-top:6px">' +
        '<button class="btn btn-sm btn-primary" onclick="_confirmIframeLogin(\'' + agentId + '\',this)" style="font-size:12px;padding:6px 16px"><span class="material-symbols-outlined" style="font-size:14px">check_circle</span> 已登录完成</button>' +
        '<a href="' + loginUrl + '" target="_blank" rel="noopener" style="font-size:11px;color:#3b82f6;text-decoration:none">↗ 新标签页打开</a>' +
        '<span class="login-status" style="font-size:11px;color:var(--text3)"></span>' +
      '</div>' +
    '</div>' +
    '<div class="login-panel" data-panel="cred" style="display:none">' +
      '<div style="margin-bottom:8px"><label style="font-size:11px;color:var(--text2);display:block;margin-bottom:3px">Username / 用户名</label>' +
      '<input type="text" class="login-username" placeholder="username / email / phone"></div>' +
      '<div style="margin-bottom:10px"><label style="font-size:11px;color:var(--text2);display:block;margin-bottom:3px">Password / 密码</label>' +
      '<input type="password" class="login-password" placeholder="password"></div>' +
      '<div style="display:flex;gap:8px;align-items:center">' +
        '<button class="btn btn-sm btn-primary" onclick="_submitLoginCredentials(\'' + agentId + '\',this)" style="font-size:12px;padding:6px 16px"><span class="material-symbols-outlined" style="font-size:14px">send</span> 提交</button>' +
        '<span class="login-status" style="font-size:11px;color:var(--text3)"></span>' +
      '</div>' +
    '</div>' +
    '<div class="login-panel" data-panel="cookie" style="display:none">' +
      '<div style="margin-bottom:8px"><label style="font-size:11px;color:var(--text2);display:block;margin-bottom:3px">Cookies (从浏览器复制)</label>' +
      '<textarea class="login-cookies" rows="3" placeholder="name=value; name2=value2; ..."></textarea></div>' +
      '<div style="margin-bottom:10px"><label style="font-size:11px;color:var(--text2);display:block;margin-bottom:3px">Token (Bearer / API Key)</label>' +
      '<input type="text" class="login-token" placeholder="Bearer xxx 或 API key"></div>' +
      '<div style="display:flex;gap:8px;align-items:center">' +
        '<button class="btn btn-sm btn-primary" onclick="_submitLoginCredentials(\'' + agentId + '\',this)" style="font-size:12px;padding:6px 16px"><span class="material-symbols-outlined" style="font-size:14px">send</span> 提交</button>' +
        '<span class="login-status" style="font-size:11px;color:var(--text3)"></span>' +
      '</div>' +
    '</div>' +
    '<div style="margin-top:8px;text-align:right">' +
      '<button class="btn btn-sm" onclick="_skipLoginRequest(\'' + agentId + '\',this)" style="font-size:11px;padding:4px 12px;color:var(--text3)">跳过登录</button>' +
    '</div>';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  _detectIframeBlock(div, iframeId);
  return div;
}

function _detectIframeBlock(card, iframeId) {
  var iframe = document.getElementById(iframeId);
  if (!iframe) return;
  var wrap = iframe.closest('.login-iframe-wrap');
  if (!wrap) return;
  var blocked = wrap.querySelector('.login-iframe-blocked');

  function showBlocked() {
    if (iframe) iframe.style.display = 'none';
    if (blocked) blocked.style.display = 'block';
  }

  iframe.onerror = showBlocked;
  setTimeout(function() {
    try {
      var doc = iframe.contentDocument || iframe.contentWindow.document;
      if (!doc || !doc.body || doc.body.innerHTML === '') showBlocked();
    } catch(e) {
      // Cross-origin = loaded OK, user can interact
    }
  }, 4000);
}

function _switchLoginTab(btn, tab) {
  var card = btn.closest('.chat-msg');
  if (!card) return;
  card.querySelectorAll('.login-tab').forEach(function(t){
    t.style.borderBottomColor = 'transparent';
    t.style.color = 'var(--text3)';
    t.style.fontWeight = '400';
  });
  btn.style.borderBottomColor = '#3b82f6';
  btn.style.color = 'var(--text)';
  btn.style.fontWeight = '600';
  card.querySelectorAll('.login-panel').forEach(function(p){
    p.style.display = p.dataset.panel === tab ? '' : 'none';
  });
}

async function _confirmIframeLogin(agentId, btn) {
  var card = btn.closest('.chat-msg');
  if (!card) return;
  var reqId = card.dataset.requestId || '';
  var status = card.querySelector('.login-status');
  btn.disabled = true;
  if (status) status.textContent = '通知 Agent 获取会话...';
  try {
    var r = await api('POST', '/api/portal/submit-login', {
      request_id: reqId,
      username: '', password: '',
      cookies: '__BROWSER_SESSION__',
      token: ''
    });
    if (r && r.ok) {
      card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0">' +
        '<span class="material-symbols-outlined" style="color:#10b981">check_circle</span>' +
        '<span style="color:#10b981;font-weight:600;font-size:13px">✓ 已确认登录，Agent 正在获取会话继续执行...</span></div>';
    } else {
      if (status) status.textContent = '⚠ ' + ((r && r.error) || '提交失败');
      btn.disabled = false;
    }
  } catch(e) {
    if (status) status.textContent = '⚠ ' + (e.message || '请求失败');
    btn.disabled = false;
  }
}

async function _submitLoginCredentials(agentId, btn) {
  var card = btn.closest('.chat-msg');
  if (!card) return;
  var reqId = card.dataset.requestId || '';
  var status = btn.closest('.login-panel').querySelector('.login-status');

  var username = (card.querySelector('.login-username') || {}).value || '';
  var password = (card.querySelector('.login-password') || {}).value || '';
  var cookies = (card.querySelector('.login-cookies') || {}).value || '';
  var token = (card.querySelector('.login-token') || {}).value || '';

  if (!username && !cookies && !token) {
    if (status) status.textContent = '⚠ 请填写账号密码或 Cookie/Token';
    return;
  }

  btn.disabled = true;
  if (status) status.textContent = '提交中...';

  try {
    var r = await api('POST', '/api/portal/submit-login', {
      request_id: reqId, username: username, password: password,
      cookies: cookies, token: token
    });
    if (r && r.ok) {
      card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0">' +
        '<span class="material-symbols-outlined" style="color:#10b981">check_circle</span>' +
        '<span style="color:#10b981;font-weight:600;font-size:13px">✓ 登录信息已提交，Agent 继续执行中...</span></div>';
    } else {
      if (status) status.textContent = '⚠ ' + ((r && r.error) || '提交失败');
      btn.disabled = false;
    }
  } catch(e) {
    if (status) status.textContent = '⚠ ' + (e.message || '请求失败');
    btn.disabled = false;
  }
}

async function _skipLoginRequest(agentId, btn) {
  var card = btn.closest('.chat-msg');
  if (!card) return;
  var reqId = card.dataset.requestId || '';
  try {
    await api('POST', '/api/portal/submit-login', {
      request_id: reqId, username: '', password: '', cookies: '', token: ''
    });
  } catch(e) {}
  _showLoginRetryState(card, '已跳过登录');
}

function _showLoginRetryState(card, reason) {
  var aid = card.dataset.agentId || '';
  var loginUrl = card.dataset.loginUrl || '';
  var siteName = card.dataset.siteName || '';
  card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0">' +
    '<span class="material-symbols-outlined" style="color:var(--text3)">skip_next</span>' +
    '<span style="color:var(--text3);font-size:13px">' + esc(reason) + '</span>' +
    '<button class="btn btn-sm" onclick="_retryLogin(this)" ' +
      'data-agent-id="' + aid + '" data-url="' + esc(loginUrl) + '" data-site="' + esc(siteName) + '" ' +
      'style="margin-left:auto;font-size:11px;padding:4px 10px;color:#3b82f6;border:1px solid #3b82f6;border-radius:6px;background:none">' +
      '<span class="material-symbols-outlined" style="font-size:14px">refresh</span> 重试登录</button>' +
    '</div>';
}

async function _retryLogin(btn) {
  var agentId = btn.dataset.agentId || '';
  var url = btn.dataset.url || '';
  var siteName = btn.dataset.site || '';
  if (!agentId || !url) return;
  btn.disabled = true;
  btn.textContent = '重置中...';
  try {
    var r = await api('POST', '/api/portal/reset-login', {
      agent_id: agentId, url: url
    });
    if (r && r.ok) {
      var card = btn.closest('.chat-msg');
      if (card) {
        card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0">' +
          '<span class="material-symbols-outlined" style="color:#3b82f6">lock_open</span>' +
          '<span style="color:#3b82f6;font-size:13px">已重置 ' + esc(siteName || url) + ' 的登录限制，请重新指示 Agent 执行任务</span></div>';
      }
    } else {
      btn.textContent = '重试失败';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = '重试失败';
    btn.disabled = false;
  }
}

async function chatApprovalAction(agentId, action, btnEl) {
  // 1. Primary: get approval_id directly from the DOM card (most reliable)
  var card = btnEl.closest('.chat-msg');
  var approvalId = card ? (card.dataset.approvalId || '') : '';

  // 2. Fallback: search the client-side approvals array
  if (!approvalId) {
    var agent = agents.find(function(a){ return a.id === agentId; });
    var agentName = agent ? agent.name : null;
    var pending = (approvals||[]).filter(function(a){
      return a.status === 'pending' && a.agent_id === agentId;
    });
    var target = pending.length > 0 ? pending[pending.length-1] : null;
    if (!target && agentName) {
      pending = (approvals||[]).filter(function(a){
        return a.status === 'pending' && a.agent_name === agentName;
      });
      target = pending.length > 0 ? pending[pending.length-1] : null;
    }
    if (target) approvalId = target.approval_id;
  }

  // 3. Last resort: fetch fresh approvals from server
  if (!approvalId) {
    try {
      var freshData = await api('GET', '/api/portal/state');
      if (freshData && freshData.approvals) {
        var rawApprovals = freshData.approvals || {};
        var allPending = [].concat(rawApprovals.pending||[]);
        var found = allPending.filter(function(a){ return a.agent_id === agentId; }).pop();
        if (!found) {
          var agent2 = agents.find(function(a){ return a.id === agentId; });
          var name2 = agent2 ? agent2.name : null;
          if (name2) found = allPending.filter(function(a){ return a.agent_name === name2; }).pop();
        }
        if (found) approvalId = found.approval_id;
      }
    } catch(e) { console.error('Failed to fetch fresh approvals:', e); }
  }

  if (!approvalId) {
    if (card) {
      var _noIdMsg = document.createElement('div');
      _noIdMsg.style.cssText = 'margin-top:8px;font-size:11px;color:var(--warning);';
      _noIdMsg.textContent = '⚠ No pending approval found. It may have timed out.';
      card.querySelector('div:last-child').appendChild(_noIdMsg);
    }
    return;
  }

  // Immediately update card UI (optimistic) — then call API
  if (card) {
    if (action === 'deny') {
      card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0"><span class="material-symbols-outlined" style="color:var(--error)">block</span><span style="color:var(--error);font-weight:600;font-size:13px">Denied</span></div>';
    } else if (action === 'approve') {
      card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0"><span class="material-symbols-outlined" style="color:var(--success)">check_circle</span><span style="color:var(--success);font-weight:600;font-size:13px">✓ Approved</span></div>';
    } else if (action === 'approve_session') {
      card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0"><span class="material-symbols-outlined" style="color:var(--primary)">verified</span><span style="color:var(--primary);font-weight:600;font-size:13px">✓ Approved (session)</span></div>';
    }
  }

  try {
    var apiAction = (action === 'approve_session') ? 'approve' : action;
    var apiBody = {approval_id: approvalId, action: apiAction};
    if (action === 'approve_session') apiBody.scope = 'session';
    var result = await api('POST', '/api/portal/approve', apiBody);
    if (result && result.error) {
      if (card) {
        var errSpan = document.createElement('div');
        errSpan.style.cssText = 'font-size:11px;color:var(--warning);margin-top:4px;';
        errSpan.textContent = '⚠ ' + result.error;
        card.appendChild(errSpan);
      }
    }
    refreshSidebar();
  } catch(e) {
    console.error('Approval action failed:', e);
    if (card) {
      var errDiv = document.createElement('div');
      errDiv.style.cssText = 'font-size:11px;color:var(--error);margin-top:4px;';
      errDiv.textContent = '⚠ ' + (e.message || 'Request failed');
      card.appendChild(errDiv);
    }
  }
}

// ============ Agent Chat Attachments ============
var _agentAttachments = {};

function _agentAttachList(agentId) {
  if (!_agentAttachments[agentId]) _agentAttachments[agentId] = [];
  return _agentAttachments[agentId];
}

function _renderAgentAttachPreview(agentId) {
  var box = document.getElementById('agent-attach-preview-'+agentId);
  if (!box) return;
  var list = _agentAttachList(agentId);
  if (list.length === 0) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = list.map(function(a, idx) {
    var thumb = a.preview_url
      ? '<img src="'+a.preview_url+'" style="width:36px;height:36px;object-fit:cover;border-radius:4px">'
      : '<span class="material-symbols-outlined" style="font-size:20px;color:var(--text3)">draft</span>';
    var sizeKb = Math.max(1, Math.round((a.size||0)/1024));
    return '<div style="display:inline-flex;align-items:center;gap:6px;background:var(--surface);border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:4px 8px;font-size:11px;color:var(--text)">' +
      thumb +
      '<div style="display:flex;flex-direction:column;max-width:140px">' +
        '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(a.name)+'</span>' +
        '<span style="color:var(--text3);font-size:10px">'+sizeKb+' KB</span>' +
      '</div>' +
      '<button onclick="removeAgentAttach(\''+agentId+'\','+idx+')" title="Remove" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:2px"><span class="material-symbols-outlined" style="font-size:14px">close</span></button>' +
    '</div>';
  }).join('');
}

function removeAgentAttach(agentId, idx) {
  var list = _agentAttachList(agentId);
  if (idx >= 0 && idx < list.length) { list.splice(idx, 1); _renderAgentAttachPreview(agentId); }
}

function handleAgentAttach(agentId, fileInput) {
  if (!fileInput || !fileInput.files || !fileInput.files.length) return;
  var list = _agentAttachList(agentId);
  var files = Array.prototype.slice.call(fileInput.files, 0, Math.max(0, 10 - list.length));
  files.forEach(function(f) {
    if (f.size > 10*1024*1024) { alert('File "'+f.name+'" exceeds 10 MB.'); return; }
    var reader = new FileReader();
    reader.onload = function(e) {
      var dataUrl = e.target.result || '';
      var b64 = dataUrl.indexOf(',') >= 0 ? dataUrl.split(',')[1] : dataUrl;
      var isImage = (f.type||'').indexOf('image/') === 0;
      list.push({ name: f.name, mime: f.type||'application/octet-stream', size: f.size, data_base64: b64, preview_url: isImage ? dataUrl : '' });
      _renderAgentAttachPreview(agentId);
    };
    reader.readAsDataURL(f);
  });
  fileInput.value = '';
}

// Active task streams per agent (so we can reconnect)
var _activeTaskStreams = {};  // agentId -> {taskId, abortCtrl}

async function sendAgentMsg(agentId) {
  const inputEl = document.getElementById('chat-input-'+agentId);
  if(!inputEl) return;
  const text = inputEl.value.trim();
  var attachments = _agentAttachList(agentId).slice();
  if(!text && !attachments.length) return;
  inputEl.value = '';
  _agentAttachments[agentId] = [];
  _renderAgentAttachPreview(agentId);

  var displayText = text || '';
  if (attachments.length) {
    var attNames = attachments.map(function(a){ return '📎'+a.name; }).join(' ');
    displayText = displayText ? displayText + '\n' + attNames : attNames;
  }
  addChatBubble(agentId, 'user', displayText);

  const progressBar = _createProgressBar(agentId);
  const thinkDiv = document.createElement('div');
  thinkDiv.style.display = 'none';

  try {
    var chatBody = {message: text || '(attached files)'};
    if (attachments.length) {
      chatBody.attachments = attachments.map(function(a) {
        return { name: a.name, mime: a.mime, data_base64: a.data_base64 };
      });
    }
    const resp = await fetch('/api/portal/agent/'+agentId+'/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify(chatBody)
    });
    if (resp.status === 401) { window.location.href = '/'; return; }
    const result = await resp.json();
    if (!result.task_id) {
      throw new Error(result.error || 'Failed to create task');
    }

    await _streamTaskEvents(agentId, result.task_id, thinkDiv, progressBar);
    refreshSidebar();
  } catch(e) {
    if (thinkDiv.parentNode) thinkDiv.remove();
    _removeProgressBar(agentId);
    var errDiv = addChatBubble(agentId, 'assistant', '');
    errDiv.textContent = '✗ '+e.message;
    errDiv.style.color='var(--error)';
  }
}

function _getAgentRobotSrc(agentId) {
  // Find agent in cached state to get robot avatar
  var agents = window._cachedAgents || [];
  var a = agents.find(function(x){ return x.id === agentId; });
  if (a && a.robot_avatar) return '/static/robots/' + a.robot_avatar + '.svg';
  if (a) return '/static/robots/robot_' + (a.role || 'general') + '.svg';
  return '/static/robots/robot_general.svg';
}

function _createProgressBar(agentId) {
  var el = document.getElementById('chat-msgs-'+agentId);
  if(!el) return null;
  var bar = document.createElement('div');
  bar.id = 'chat-progress-'+agentId;
  bar.className = 'chat-progress-bar';
  var robotSrc = _getAgentRobotSrc(agentId);
  bar.innerHTML = '' +
    '<div style="display:flex;align-items:center;gap:8px;padding:8px 0">' +
      '<div class="robot-working-container" id="robot-anim-'+agentId+'" style="position:relative;display:inline-flex;flex-direction:column;align-items:center">' +
        '<div class="robot-status-bubble" id="robot-bubble-'+agentId+'" style="background:var(--primary);color:#fff;font-size:10px;padding:2px 8px;border-radius:8px;margin-bottom:4px;white-space:nowrap;animation:robotBubblePulse 1.5s infinite">工作中...</div>' +
        '<div style="position:relative">' +
          '<img src="'+robotSrc+'" class="robot-working" style="width:36px;height:36px;animation:robotTyping 0.6s steps(2) infinite">' +
          '<div style="position:absolute;bottom:-2px;right:-6px;font-size:10px;animation:robotTyping 0.4s steps(3) infinite">⌨️</div>' +
        '</div>' +
      '</div>' +
      '<span class="chat-progress-phase" id="chat-progress-phase-'+agentId+'" style="font-size:12px;color:var(--text2)">Preparing...</span>' +
      '<button class="chat-progress-abort" id="chat-progress-abort-'+agentId+'" onclick="_abortTask(\''+agentId+'\')" title="Stop task">✕</button>' +
    '</div>';
  el.appendChild(bar);
  el.scrollTop = el.scrollHeight;
  return bar;
}

function _updateProgress(agentId, progress, phase) {
  var phaseEl = document.getElementById('chat-progress-phase-'+agentId);
  var abortBtn = document.getElementById('chat-progress-abort-'+agentId);
  var bubble = document.getElementById('robot-bubble-'+agentId);
  var robotAnim = document.getElementById('robot-anim-'+agentId);
  if(phaseEl) {
    var isDone = progress >= 100 || phase === 'Done' || phase === 'Aborted' || phase === 'Failed';
    if (isDone) {
      phaseEl.innerHTML = (phase || 'Done');
      if (bubble) {
        bubble.style.background = 'var(--success)';
        bubble.style.animation = 'none';
        bubble.textContent = '✅ 已完成';
      }
      if (robotAnim) {
        var img = robotAnim.querySelector('.robot-working');
        if (img) img.style.animation = 'none';
      }
    } else {
      phaseEl.textContent = phase || 'Working...';
      if (bubble) bubble.textContent = phase || '工作中...';
    }
  }
  if(abortBtn && (progress >= 100 || phase === 'Done' || phase === 'Aborted' || phase === 'Failed')) {
    abortBtn.style.display = 'none';
  }
}

function _removeProgressBar(agentId) {
  var bar = document.getElementById('chat-progress-'+agentId);
  if(bar && bar.parentNode) bar.remove();
}

async function _abortTask(agentId) {
  var stream = _activeTaskStreams[agentId];
  if (!stream) return;
  var taskId = stream.taskId;
  try {
    var resp = await fetch('/api/portal/chat-task/'+taskId+'/abort', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      credentials: 'same-origin',
      body: '{}'
    });
    if (resp.ok) {
      // Abort the SSE stream client-side too
      if (stream.abortCtrl) stream.abortCtrl.abort();
      _updateProgress(agentId, 0, 'Aborted');
      var pb = document.getElementById('chat-progress-'+agentId);
      if (pb) { pb.style.transition = 'opacity 0.6s'; pb.style.opacity = '0'; }
      setTimeout(function(){ _removeProgressBar(agentId); }, 800);
      delete _activeTaskStreams[agentId];
    }
  } catch(e) {
    console.error('Abort failed:', e);
  }
}

async function _saveToFile(btn, agentId) {
  var msgEl = btn.closest('.chat-msg');
  if(!msgEl) return;
  var contentEl = msgEl.querySelector('.chat-msg-content');
  if(!contentEl) return;
  var content = contentEl._rawText || contentEl.textContent || '';
  if(!content.trim()) { alert('No content to save'); return; }
  // Guess filename from content
  var defaultName = 'output.txt';
  if(content.indexOf('<html') !== -1 || content.indexOf('<!DOCTYPE') !== -1) defaultName = 'output.html';
  else if(content.indexOf('def ') !== -1 || content.indexOf('import ') !== -1) defaultName = 'output.py';
  else if(content.indexOf('function ') !== -1 || content.indexOf('const ') !== -1) defaultName = 'output.js';
  else if(content.indexOf('{') !== -1 && content.indexOf('"') !== -1) defaultName = 'output.json';
  var filename = prompt('Save as (relative to agent working directory):', defaultName);
  if(!filename) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">hourglass_top</span> Saving...';
  try {
    var resp = await fetch('/api/portal/agent/'+agentId+'/save-file', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({filename: filename, content: content})
    });
    var result = await resp.json();
    if(result.ok) {
      btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">check_circle</span> Saved';
      btn.style.color = 'var(--success, #22c55e)';
      setTimeout(function(){
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">save</span> Save';
        btn.style.color = '';
      }, 2000);
    } else {
      alert('Save failed: ' + (result.error || 'Unknown error'));
      btn.disabled = false;
      btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">save</span> Save';
    }
  } catch(e) {
    alert('Save error: ' + e.message);
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">save</span> Save';
  }
}

function _copyToClipboard(btn) {
  var msgEl = btn.closest('.chat-msg');
  if(!msgEl) return;
  var contentEl = msgEl.querySelector('.chat-msg-content');
  if(!contentEl) return;
  var copyText = contentEl._rawText || contentEl.textContent || '';
  navigator.clipboard.writeText(copyText).then(function(){
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">check</span>';
    setTimeout(function(){ btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">content_copy</span>'; }, 1500);
  });
}

async function _streamTaskEvents(agentId, taskId, thinkDiv, progressBar) {
  var cursor = 0;
  var msgDiv = null;
  var fullText = '';
  var abortCtrl = new AbortController();
  _activeTaskStreams[agentId] = {taskId: taskId, abortCtrl: abortCtrl};

  // Safety heartbeat: poll task status every 5s as fallback
  // In case SSE misses the done event (connection issues, timeouts, etc.)
  var heartbeatDone = false;
  var heartbeat = setInterval(async function() {
    try {
      var r = await fetch('/api/portal/chat-task/'+taskId+'/status', {credentials:'same-origin'});
      if (r.status === 401 || r.status === 403) { clearInterval(heartbeat); return; }
      if (!r.ok) return;
      var d = await r.json();
      if (d.status === 'completed' || d.status === 'failed' || d.status === 'aborted') {
        heartbeatDone = true;
        // Abort the SSE stream so the main loop picks up the terminal state
        try { abortCtrl.abort(); } catch(e){}
      }
    } catch(e){}
  }, 5000);

  var maxRetries = 60;  // up to 60 reconnections (60 * 120s = 2 hours max)
  for (var retry = 0; retry < maxRetries; retry++) {
    try {
      var resp = await fetch('/api/portal/chat-task/'+taskId+'/stream?cursor='+cursor, {
        credentials: 'same-origin',
        signal: abortCtrl.signal,
      });
      if (resp.status === 401 || resp.status === 403) {
        // Auth failed — stop retrying, user needs to re-login
        clearInterval(heartbeat);
        _updateProgress(agentId, 0, '认证失败，请刷新页面');
        delete _activeTaskStreams[agentId];
        return;
      }
      if (resp.status === 404) {
        throw new Error('Task not found');
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var taskDone = false;

      while(true) {
        var read = await reader.read();
        if(read.done) break;
        buffer += decoder.decode(read.value, {stream:true});
        var lines = buffer.split('\n');
        buffer = lines.pop();
        for(var i=0; i<lines.length; i++) {
          var line = lines[i];
          if(!line.startsWith('data: ')) continue;
          var d = line.slice(6);
          if(d==='[DONE]') { taskDone = true; continue; }
          try {
            var evt = JSON.parse(d);
            if(evt.type==='status') {
              _updateProgress(agentId, evt.progress||0, evt.phase||'');
              cursor = cursor;  // cursor updated by events
            } else if(evt.type==='text_delta') {
              if (!evt.content) continue;  // Skip empty deltas
              if (thinkDiv.parentNode) thinkDiv.remove();
              if (!msgDiv) { msgDiv = addChatBubble(agentId, 'assistant', '', 0); }
              fullText += evt.content;
              // Track the latest timestamp from streaming deltas
              if (evt.timestamp) msgDiv._timestamp = evt.timestamp;
              msgDiv.textContent = fullText;  // Plain text during streaming for speed
            } else if(evt.type==='text') {
              var textContent = evt.content || '';
              if (!textContent.trim()) continue;  // Skip empty text events
              if (thinkDiv.parentNode) thinkDiv.remove();
              if (!msgDiv) { msgDiv = addChatBubble(agentId, 'assistant', '', evt.timestamp||0); }
              fullText = textContent;
              // Non-streaming: render with markdown
              msgDiv.innerHTML = _renderSimpleMarkdown(fullText, agentId);
              msgDiv._rawText = fullText;
              // Update time label with final timestamp
              if (evt.timestamp) msgDiv._timestamp = evt.timestamp;
            } else if(evt.type==='thinking') {
              if (msgDiv) { msgDiv = null; fullText = ''; }
              // Update progress bar phase text instead of separate thinkDiv
              _updateProgress(agentId, 0, evt.content || 'Thinking...');
            } else if(evt.type==='tool_call') {
              // Show tool name in progress bar
              _updateProgress(agentId, 0, '🔧 ' + esc(evt.name||''));
            } else if(evt.type==='tool_result') {
              // Reset progress bar to thinking state
              _updateProgress(agentId, 0, 'Thinking...');
            } else if(evt.type==='approval_request') {
              if (thinkDiv.parentNode) thinkDiv.remove();
              addApprovalBubble(agentId, evt);
            } else if(evt.type==='login_request') {
              if (thinkDiv.parentNode) thinkDiv.remove();
              addLoginRequestBubble(agentId, evt);
            } else if(evt.type==='plan_update') {
              // Real-time execution steps update
              renderExecutionSteps(agentId, evt.plan);
            } else if(evt.type==='artifact_refs') {
              // Phase-2 envelope injection: render FileCard widgets for
              // every artifact produced during this assistant turn.
              if (!msgDiv) { msgDiv = addChatBubble(agentId, 'assistant', ''); }
              try { _appendFileCards(msgDiv, evt.refs || []); } catch(e) {}
            } else if(evt.type==='error') {
              if (thinkDiv.parentNode) thinkDiv.remove();
              if (!msgDiv) { msgDiv = addChatBubble(agentId, 'assistant', ''); }
              msgDiv.textContent = '✗ '+evt.content;
              msgDiv.style.color='var(--error)';
            } else if(evt.type==='done') {
              taskDone = true;
              // Backfill: scan deliverable_dir + match against bubbles
              // for any file the SSE artifact_refs envelope missed
              // (e.g. side-channel tools that didn't go through extractor).
              try { attachHistoricalFileCards(agentId); } catch(e) {}
            }
          } catch(e){}
        }
      }

      if (taskDone || heartbeatDone) {
        clearInterval(heartbeat);
        if (thinkDiv.parentNode) thinkDiv.remove();
        // Apply markdown rendering to final streamed text
        if (msgDiv && fullText) {
          msgDiv.innerHTML = _renderSimpleMarkdown(fullText, agentId);
          msgDiv._rawText = fullText;
        }
        // Update the time label on the assistant bubble after streaming completes
        if (msgDiv && msgDiv.parentNode) {
          var timeEl = msgDiv.parentNode.querySelector('.chat-msg-time');
          if (timeEl) {
            var finalTs = msgDiv._timestamp || (Date.now() / 1000);
            timeEl.textContent = _formatChatTime(finalTs);
          }
        }
        _updateProgress(agentId, 100, 'Done');
        // Auto TTS: read the final response aloud if toggle is on
        _autoSpeak(agentId, fullText);
        // Fade out progress bar
        var pb = document.getElementById('chat-progress-'+agentId);
        if (pb) { pb.style.transition = 'opacity 0.6s'; pb.style.opacity = '0'; }
        setTimeout(function(){ _removeProgressBar(agentId); }, 800);
        delete _activeTaskStreams[agentId];
        // Stop polling execution steps since task is complete
        if (_stepsTimers[agentId]) clearInterval(_stepsTimers[agentId]);
        delete _stepsTimers[agentId];
        // Load final state from backend
        loadAgentEventLog(agentId);
        loadExecutionSteps(agentId);
        return;
      }
      // SSE connection ended but task not done — reconnect automatically
      // Check task status before reconnecting
      try {
        var statusResp = await fetch('/api/portal/chat-task/'+taskId+'/status', {credentials:'same-origin'});
        if (statusResp.ok) {
          var statusData = await statusResp.json();
          if (statusData.status === 'completed' || statusData.status === 'failed' || statusData.status === 'aborted') {
            // Task finished/aborted while SSE was reconnecting
            clearInterval(heartbeat);
            if (thinkDiv.parentNode) thinkDiv.remove();
            if (statusData.status === 'completed' && statusData.result && !fullText) {
              msgDiv = addChatBubble(agentId, 'assistant', statusData.result);
            }
            var endLabel = statusData.status === 'completed' ? 'Done' : statusData.status === 'aborted' ? 'Aborted' : 'Failed';
            _updateProgress(agentId, statusData.status === 'completed' ? 100 : 0, endLabel);
            var pb = document.getElementById('chat-progress-'+agentId);
            if (pb) { pb.style.transition = 'opacity 0.6s'; pb.style.opacity = '0'; }
            setTimeout(function(){ _removeProgressBar(agentId); }, 800);
            delete _activeTaskStreams[agentId];
            // Stop polling execution steps since task is complete
            if (_stepsTimers[agentId]) clearInterval(_stepsTimers[agentId]);
            delete _stepsTimers[agentId];
            loadAgentEventLog(agentId);
            loadExecutionSteps(agentId);
            return;
          }
        }
      } catch(e2) {}
      // Still running — reconnect with current cursor
    } catch(e) {
      if (e.name === 'AbortError') break;
      // Wait a bit before retry
      await new Promise(function(r){ setTimeout(r, 500); });
    }
  }
  // If we get here (max retries exhausted or heartbeat detected done), clean up
  clearInterval(heartbeat);
  if (thinkDiv.parentNode) thinkDiv.remove();
  // If heartbeat detected completion, show result
  if (heartbeatDone && !fullText) {
    try {
      var finalResp = await fetch('/api/portal/chat-task/'+taskId+'/status', {credentials:'same-origin'});
      if (finalResp.ok) {
        var finalData = await finalResp.json();
        if (finalData.status === 'completed' && finalData.result) {
          addChatBubble(agentId, 'assistant', finalData.result);
        } else if (finalData.status === 'failed' && finalData.error) {
          var errDiv = addChatBubble(agentId, 'assistant', '✗ ' + finalData.error);
          if (errDiv) errDiv.style.color = 'var(--error)';
        }
        _updateProgress(agentId, finalData.status === 'completed' ? 100 : 0,
          finalData.status === 'completed' ? 'Done' : finalData.status === 'failed' ? 'Failed' : 'Aborted');
      }
    } catch(e){}
  }
  var pb = document.getElementById('chat-progress-'+agentId);
  if (pb) { pb.style.transition = 'opacity 0.6s'; pb.style.opacity = '0'; }
  setTimeout(function(){ _removeProgressBar(agentId); }, 800);
  delete _activeTaskStreams[agentId];
}

// Reconnect to active task stream when switching back to agent view
async function _reconnectActiveStream(agentId) {
  // Check if there's an active task for this agent
  var stream = _activeTaskStreams[agentId];
  if (stream && stream.taskId) {
    // Task stream exists but DOM was rebuilt — check if task is still running
    try {
      var statusData = await api('GET', '/api/portal/chat-task/'+stream.taskId+'/status');
      if (!statusData) { delete _activeTaskStreams[agentId]; return; }
      if (['queued','thinking','streaming','tool_exec','waiting_approval'].indexOf(statusData.status) !== -1) {
        // Task is still active — recreate progress bar + thinking UI and reconnect
        var progressBar = _createProgressBar(agentId);
        var thinkDiv = addChatBubble(agentId, 'assistant thinking', '');
        thinkDiv.innerHTML = '<span class="thinking-dots"><span>.</span><span>.</span><span>.</span></span>';
        // Abort old stream connection if any
        if (stream.abortCtrl) { try { stream.abortCtrl.abort(); } catch(e){} }
        // Reconnect SSE
        _streamTaskEvents(agentId, stream.taskId, thinkDiv, progressBar);
        return;
      }
      // Task finished — clean up and show result if needed
      delete _activeTaskStreams[agentId];
      if (statusData.status === 'completed' && statusData.result) {
        // Check if the final response is already in chat
        var chatEl = document.getElementById('chat-msgs-'+agentId);
        if (chatEl) {
          var msgs = chatEl.querySelectorAll('.chat-msg.assistant:not(.thinking)');
          var lastMsg = msgs.length > 0 ? msgs[msgs.length-1] : null;
          var lastContent = lastMsg ? (lastMsg.querySelector('.chat-msg-content') || lastMsg) : null;
          var lastText = lastContent ? (lastContent._rawText || lastContent.textContent || '') : '';
          if (!lastContent || lastText !== statusData.result) {
            addChatBubble(agentId, 'assistant', statusData.result);
          }
        }
      }
    } catch(e) {
      delete _activeTaskStreams[agentId];
    }
    return;
  }
  // No tracked stream — check if agent is busy (status from state data)
  var ag = agents.find(function(a){ return a.id === agentId; });
  if (ag && ag.status === 'busy') {
    // Agent is busy but we don't have a tracked stream (page was loaded fresh)
    // Show a working indicator
    var progressBar = _createProgressBar(agentId);
    _updateProgress(agentId, 50, 'Working (reconnecting)...');
    // Try to find the latest active ChatTask (NOT AgentTask)
    try {
      var taskResp = await api('GET', '/api/portal/agent/'+agentId+'/chat-tasks');
      if (taskResp && taskResp.tasks) {
        var runningTask = taskResp.tasks.find(function(t){ return ['queued','thinking','streaming','tool_exec','waiting_approval'].indexOf(t.status) !== -1; });
        if (runningTask) {
          var thinkDiv = addChatBubble(agentId, 'assistant thinking', '');
          thinkDiv.innerHTML = '<span class="thinking-dots"><span>.</span><span>.</span><span>.</span></span>';
          _streamTaskEvents(agentId, runningTask.id, thinkDiv, progressBar);
          return;
        }
      }
    } catch(e) {}
    // No running task found — remove progress bar
    _removeProgressBar(agentId);
  }
}

// ============ Agent events log ============
async function loadAgentEvents(agentId) {
  try {
    const data = await api('GET', `/api/portal/agent/${agentId}/events`);
    if (!data) return;
    const c = document.getElementById('content');
    c.style.padding = '24px';
    c.innerHTML = `
      <button class="btn btn-ghost btn-sm" onclick="renderAgentChat('${agentId}')" style="margin-bottom:12px">← Back to Chat</button>
      <div class="event-log">
        ${(data.events||[]).map(e => `
          <div class="event-item">
            <span class="time">${new Date(e.timestamp*1000).toLocaleTimeString()}</span>
            <span class="kind ${e.kind}">${e.kind}</span>
            <span class="data">${esc(JSON.stringify(e.data).slice(0,200))}</span>
          </div>
        `).join('')}
      </div>
    `;
  } catch(e) {}
}

// ============ Approvals & Policies ============
var _selectedPolicyAgent = '';  // agent id for policy config

function renderApprovals() {
  var c = document.getElementById('content');
  var pending = (approvals||[]).filter(function(a){ return a.status === 'pending'; });
  var history = (approvals||[]).filter(function(a){ return a.status !== 'pending'; }).slice(0, 20);

  // Build agent selector options
  var agentOpts = agents.map(function(a) {
    return '<option value="' + a.id + '"' + (_selectedPolicyAgent === a.id ? ' selected' : '') + '>' + esc((a.role||'general') + '-' + a.name) + '</option>';
  }).join('');

  // Get the currently selected agent's profile
  var selAgent = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  var prof = selAgent ? (selAgent.profile || {}) : {};
  var execPolicy = prof.exec_policy || 'ask';
  var execBlacklist = prof.exec_blacklist || [];
  var execWhitelist = prof.exec_whitelist || [];

  c.innerHTML = '' +
    '<div style="margin-bottom:28px">' +
      '<h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px">Tool Approvals & Policies</h2>' +
      '<p style="color:var(--text2);font-size:14px;margin-top:4px">Configure per-agent execution policies and manage pending tool approval requests.</p>' +
    '</div>' +

    '<!-- Agent Selector -->' +
    '<div style="margin-bottom:20px;display:flex;align-items:center;gap:12px">' +
      '<span class="material-symbols-outlined" style="font-size:20px;color:var(--primary)">smart_toy</span>' +
      '<span style="font-size:13px;font-weight:600">Configure Policy for:</span>' +
      '<select id="policy-agent-select" onchange="switchPolicyAgent(this.value)" style="padding:8px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;min-width:200px">' +
        '<option value="">-- Select Agent --</option>' +
        agentOpts +
      '</select>' +
    '</div>' +

    (selAgent ? (
    '<!-- Per-Agent Policy Config -->' +
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:32px">' +
      '<div style="background:var(--surface);border-radius:12px;padding:24px;border:1px solid var(--border-light)">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px"><span class="material-symbols-outlined" style="font-size:22px;color:var(--primary)">security</span><span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:15px;font-weight:700">' + esc(selAgent.name) + ' — Execution Policy</span></div>' +
        '<p style="font-size:12px;color:var(--text3);margin-bottom:16px">Control how this agent handles tool execution requests.</p>' +
        '<div style="display:flex;flex-direction:column;gap:8px">' +
          _policyRadio('full', execPolicy, 'Full Access', 'Execute all allowed tools automatically', 'var(--success)') +
          _policyRadio('ask', execPolicy, 'Ask Before Execute', 'Require human approval for each tool invocation', 'var(--warning)') +
          _policyRadio('deny', execPolicy, 'Deny All', 'Block all tool execution requests', 'var(--error)') +
        '</div>' +
      '</div>' +

      '<div style="background:var(--surface);border-radius:12px;padding:24px;border:1px solid var(--border-light)">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px"><span class="material-symbols-outlined" style="font-size:22px;color:var(--primary)">terminal</span><span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:15px;font-weight:700">' + esc(selAgent.name) + ' — Command Rules</span></div>' +
        '<div style="margin-bottom:16px">' +
          '<div style="margin-bottom:8px"><span style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700">Blacklist (Blocked Commands)</span></div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">' +
            execBlacklist.map(function(cmd){ return '<span style="background:rgba(248,81,73,0.1);color:var(--error);padding:4px 10px;border-radius:6px;font-size:11px;font-family:monospace;display:flex;align-items:center;gap:4px">' + esc(cmd) + ' <span style="cursor:pointer;font-size:14px" onclick="removeAgentBlacklist(\'' + esc(cmd) + '\')">&times;</span></span>'; }).join('') +
          '</div>' +
          '<div style="display:flex;gap:6px"><input id="new-blacklist" placeholder="Add blocked command..." style="flex:1;padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;font-family:monospace" onkeydown="if(event.key===\'Enter\'&&!event.isComposing)addAgentBlacklist()"><button class="btn btn-sm btn-danger" onclick="addAgentBlacklist()">+ Add</button></div>' +
        '</div>' +
        '<div>' +
          '<div style="margin-bottom:8px"><span style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700">Whitelist (Always Allowed)</span></div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">' +
            execWhitelist.map(function(cmd){ return '<span style="background:rgba(63,185,80,0.1);color:var(--success);padding:4px 10px;border-radius:6px;font-size:11px;font-family:monospace;display:flex;align-items:center;gap:4px">' + esc(cmd) + ' <span style="cursor:pointer;font-size:14px" onclick="removeAgentWhitelist(\'' + esc(cmd) + '\')">&times;</span></span>'; }).join('') +
          '</div>' +
          '<div style="display:flex;gap:6px"><input id="new-whitelist" placeholder="Add allowed command..." style="flex:1;padding:6px 10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;font-family:monospace" onkeydown="if(event.key===\'Enter\'&&!event.isComposing)addAgentWhitelist()"><button class="btn btn-sm btn-success" onclick="addAgentWhitelist()">+ Add</button></div>' +
        '</div>' +
      '</div>' +
    '</div>' +
    '<!-- Workspace Access -->' +
    '<div style="background:var(--surface);border-radius:12px;padding:24px;border:1px solid var(--border-light);margin-bottom:32px">' +
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px"><span class="material-symbols-outlined" style="font-size:22px;color:var(--primary)">folder_shared</span><span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:15px;font-weight:700">' + esc(selAgent.name) + ' — Workspace Access</span></div>' +
      '<div style="font-size:12px;color:var(--text3);margin-bottom:12px">Working Directory: <code style="color:var(--primary)">' + esc((selAgent.profile||{}).working_dir || selAgent.working_dir || 'Not set') + '</code></div>' +
      '<div id="authorized-ws-list-policy"></div>' +
      '<div style="display:flex;gap:8px;margin-top:10px">' +
        '<select id="ws-auth-select-policy" class="input" style="flex:1;padding:8px 12px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px">' +
          '<option value="">-- Select Agent --</option>' +
        '</select>' +
        '<button class="btn btn-primary" style="padding:8px 16px;cursor:pointer" onclick="authorizeWorkspacePolicy()">Authorize</button>' +
      '</div>' +
    '</div>'
    ) : '<div style="padding:32px;background:var(--surface);border-radius:12px;border:1px solid var(--border-light);text-align:center;color:var(--text3);margin-bottom:32px"><span class="material-symbols-outlined" style="font-size:36px;display:block;margin-bottom:8px">arrow_upward</span>Select an agent above to configure its execution policy.</div>') +

    '<!-- Pending Approvals -->' +
    '<div style="margin-bottom:24px">' +
      '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:15px;font-weight:700;margin-bottom:14px">Pending Approvals (' + pending.length + ')</h3>' +
      (pending.length === 0 ? '<div style="color:var(--text3);padding:16px;background:var(--surface);border-radius:8px;border:1px solid var(--border-light)">No pending approvals.</div>' : '') +
      pending.map(function(a){
        var ts = (a.created_at || a.timestamp || 0) * 1000;
        var tsText = ts ? new Date(ts).toLocaleString() : '—';
        var args = a.arguments || {};
        var argsStr = JSON.stringify(args);
        // Build human-readable action description
        var actionDesc = '';
        if (a.tool_name === 'bash' || a.tool_name === 'exec') {
          actionDesc = '<span style="color:var(--text2)">Command:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc((args.command||'').substring(0,300)) + '</code>';
        } else if (a.tool_name === 'write_file') {
          actionDesc = '<span style="color:var(--text2)">Write to:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc(args.path||'') + '</code>' + (args.content ? ' <span style="color:var(--text3)">(' + args.content.length + ' chars)</span>' : '');
        } else if (a.tool_name === 'edit_file') {
          actionDesc = '<span style="color:var(--text2)">Edit:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc(args.path||'') + '</code>';
        } else if (a.tool_name === 'read_file') {
          actionDesc = '<span style="color:var(--text2)">Read:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc(args.path||'') + '</code>';
        } else if (a.tool_name === 'web_search') {
          actionDesc = '<span style="color:var(--text2)">Search:</span> ' + esc((args.query||'').substring(0,200));
        } else if (a.tool_name === 'web_fetch') {
          actionDesc = '<span style="color:var(--text2)">Fetch URL:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc((args.url||'').substring(0,200)) + '</code>';
        } else if (a.tool_name === 'web_screenshot') {
          actionDesc = '<span style="color:var(--text2)">Screenshot:</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc((args.url||'').substring(0,200)) + '</code>';
        } else if (a.tool_name === 'http_request') {
          actionDesc = '<span style="color:var(--text2)">' + esc(args.method||'GET') + ':</span> <code style="background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px">' + esc((args.url||'').substring(0,200)) + '</code>';
        } else if (a.tool_name === 'mcp_call') {
          actionDesc = '<span style="color:var(--text2)">MCP:</span> ' + esc(args.mcp_id||'') + ' → <strong>' + esc(args.tool||'') + '</strong>';
          if (args.arguments) actionDesc += ' <span style="color:var(--text3);font-size:10px">' + esc(JSON.stringify(args.arguments).substring(0,150)) + '</span>';
        } else if (a.tool_name === 'send_message') {
          actionDesc = '<span style="color:var(--text2)">To:</span> ' + esc(agentName(args.to_agent||'')) + ' <span style="color:var(--text3)">— ' + esc((args.content||'').substring(0,120)) + '</span>';
        } else if (a.tool_name === 'team_create') {
          actionDesc = '<span style="color:var(--text2)">Create Agent:</span> ' + esc(args.name||'') + ' <span style="color:var(--text3)">— ' + esc((args.task||'').substring(0,120)) + '</span>';
        } else if (a.tool_name === 'task_update') {
          actionDesc = '<span style="color:var(--text2)">Task:</span> ' + esc(args.action||'') + (args.title ? ' — ' + esc(args.title.substring(0,100)) : '');
        } else if (argsStr !== '{}') {
          actionDesc = '<span style="color:var(--text3);font-size:10px;font-family:monospace">' + esc(argsStr.substring(0,250)) + '</span>';
        }
        var reasonHtml = a.reason ? '<div class="approval-detail" style="color:var(--warning);font-size:11px">' + esc(a.reason) + '</div>' : '';
        var actionHtml = actionDesc ? '<div style="margin-top:6px;font-size:12px;line-height:1.5;word-break:break-all">' + actionDesc + '</div>' : '';
        return '<div class="approval-card">' +
          '<div class="approval-header">' +
            '<div style="width:36px;height:36px;border-radius:8px;background:rgba(210,153,34,0.1);display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="material-symbols-outlined" style="font-size:18px;color:var(--warning)">lock</span></div>' +
            '<div class="approval-info">' +
              '<div class="approval-title">' + esc(a.agent_name) + ' requests to use <strong>' + esc(a.tool_name) + '</strong></div>' +
              reasonHtml +
              actionHtml +
              '<div class="approval-detail" style="margin-top:4px;font-size:10px;color:var(--text3)">' + tsText + '</div>' +
            '</div>' +
          '</div>' +
          '<div class="approval-actions">' +
            '<button class="btn btn-sm btn-success" onclick="approveRequest(\'' + a.approval_id + '\')"><span class="material-symbols-outlined" style="font-size:14px">check</span> Approve</button>' +
            '<button class="btn btn-sm" onclick="approveRequest(\'' + a.approval_id + '\',\'session\')" style="background:var(--primary);color:#0e141b"><span class="material-symbols-outlined" style="font-size:14px">verified</span> Session</button>' +
            '<button class="btn btn-sm btn-danger" onclick="denyRequest(\'' + a.approval_id + '\')"><span class="material-symbols-outlined" style="font-size:14px">close</span> Deny</button>' +
          '</div>' +
        '</div>';
      }).join('') +
    '</div>' +

    '<!-- History -->' +
    '<div>' +
      '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:15px;font-weight:700;margin-bottom:14px">Recent Decisions</h3>' +
      (history.length === 0 ? '<div style="color:var(--text3);padding:16px">No history yet.</div>' : '') +
      history.map(function(a){
        var ts = (a.decided_at || a.created_at || a.timestamp || 0) * 1000;
        var tsText = ts ? new Date(ts).toLocaleTimeString() : '—';
        var args = a.arguments || {};
        // Brief action summary for history
        var brief = '';
        if (a.tool_name === 'bash' && args.command) brief = ': ' + (args.command||'').substring(0,80);
        else if ((a.tool_name === 'write_file' || a.tool_name === 'edit_file' || a.tool_name === 'read_file') && args.path) brief = ': ' + args.path;
        else if (a.tool_name === 'web_search' && args.query) brief = ': ' + (args.query||'').substring(0,60);
        else if (a.tool_name === 'web_fetch' && args.url) brief = ': ' + (args.url||'').substring(0,60);
        else if (a.tool_name === 'mcp_call') brief = ': ' + (args.mcp_id||'') + '→' + (args.tool||'');
        else if (a.tool_name === 'http_request') brief = ': ' + (args.method||'GET') + ' ' + (args.url||'').substring(0,60);
        else if (a.tool_name === 'send_message') brief = ': → ' + agentName(args.to_agent||'');
        return '<div class="event-item"><span class="time">' + tsText + '</span><span class="kind" style="color:' + (a.status==='approved'?'var(--success)':'var(--error)') + '">' + a.status + '</span><span class="data">' + esc(a.agent_name) + ' → ' + esc(a.tool_name) + '<span style="color:var(--text3);font-size:11px">' + esc(brief) + '</span></span></div>';
      }).join('') +
    '</div>';
}

// Helper to build policy radio buttons
function _policyRadio(value, current, label, desc, color) {
  var active = current === value;
  var borderC = active ? color : 'var(--border-light)';
  var bgC = active ? color.replace('var(--', 'rgba(').replace(')', ',0.08)') : 'transparent';
  return '<label style="display:flex;align-items:center;gap:10px;padding:12px;border-radius:8px;cursor:pointer;border:1px solid ' + borderC + ';background:' + bgC + '" onclick="setAgentExecPolicy(\'' + value + '\')">' +
    '<input type="radio" name="exec-policy" value="' + value + '" ' + (active?'checked':'') + ' style="accent-color:' + color + '">' +
    '<div><div style="font-size:13px;font-weight:600;color:' + (active?color:'var(--text)') + '">' + label + '</div><div style="font-size:11px;color:var(--text3)">' + desc + '</div></div>' +
  '</label>';
}

function switchPolicyAgent(agentId) {
  _selectedPolicyAgent = agentId;
  renderApprovals();
  if (agentId) loadWorkspaceAccessPolicy(agentId);
}

async function loadWorkspaceAccessPolicy(agentId) {
  try {
    var data = await api('POST', '/api/portal/agent/workspace/list', { agent_id: agentId });
    if (!data) return;
    var allAgentsList = agents || [];
    var selectEl = document.getElementById('ws-auth-select-policy');
    if (selectEl) {
      var otherAgents = allAgentsList.filter(function(a) { return a.id !== agentId; });
      selectEl.innerHTML = '<option value="">-- Select Agent --</option>' +
        otherAgents.map(function(a) {
          return '<option value="' + esc(a.id) + '">' + esc(a.name) + '</option>';
        }).join('');
    }
    var listEl = document.getElementById('authorized-ws-list-policy');
    if (!listEl) return;
    var auth = data.authorized_workspaces || [];
    if (auth.length === 0) {
      listEl.innerHTML = '<div style="color:var(--text3);font-size:12px">No authorized workspaces</div>';
    } else {
      listEl.innerHTML = auth.map(function(otherId) {
        var otherAgent = allAgentsList.find(function(a) { return a.id === otherId; });
        var name = otherAgent ? otherAgent.name : otherId;
        return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg);border-radius:8px;margin-bottom:4px;border:1px solid var(--border-light)">' +
          '<span style="font-size:12px;color:var(--text)">' + esc(name) + '</span>' +
          '<button class="btn btn-sm" style="padding:4px 10px;font-size:10px;background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.3);color:#ff6b6b;border-radius:6px;cursor:pointer" onclick="revokeWorkspacePolicy(\'' + esc(otherId) + '\')">' +
            'Revoke' +
          '</button>' +
        '</div>';
      }).join('');
    }
  } catch(e) { console.error('Error loading workspace access:', e); }
}

async function authorizeWorkspacePolicy() {
  if (!_selectedPolicyAgent) return;
  var sel = document.getElementById('ws-auth-select-policy');
  var targetId = sel ? sel.value : '';
  if (!targetId) return;
  try {
    await api('POST', '/api/portal/agent/workspace/authorize', {
      agent_id: _selectedPolicyAgent,
      target_agent_id: targetId
    });
    loadWorkspaceAccessPolicy(_selectedPolicyAgent);
  } catch(e) { console.error('Error authorizing workspace:', e); }
}

async function revokeWorkspacePolicy(targetId) {
  if (!_selectedPolicyAgent) return;
  try {
    await api('POST', '/api/portal/agent/workspace/revoke', {
      agent_id: _selectedPolicyAgent,
      target_agent_id: targetId
    });
    loadWorkspaceAccessPolicy(_selectedPolicyAgent);
  } catch(e) { console.error('Error revoking workspace:', e); }
}

function setAgentExecPolicy(policy) {
  if (!_selectedPolicyAgent) return;
  var ag = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  if (!ag) return;
  if (!ag.profile) ag.profile = {};
  ag.profile.exec_policy = policy;
  api('POST', '/api/portal/agent/' + _selectedPolicyAgent + '/profile', { exec_policy: policy });
  renderApprovals();
}
function addAgentBlacklist() {
  if (!_selectedPolicyAgent) return;
  var el = document.getElementById('new-blacklist');
  var val = (el.value||'').trim();
  if (!val) return;
  var ag = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  if (!ag) return;
  if (!ag.profile) ag.profile = {};
  var bl = ag.profile.exec_blacklist || [];
  if (bl.indexOf(val) === -1) { bl.push(val); ag.profile.exec_blacklist = bl; }
  el.value = '';
  api('POST', '/api/portal/agent/' + _selectedPolicyAgent + '/profile', { exec_blacklist: bl });
  renderApprovals();
}
function removeAgentBlacklist(cmd) {
  if (!_selectedPolicyAgent) return;
  var ag = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  if (!ag || !ag.profile) return;
  ag.profile.exec_blacklist = (ag.profile.exec_blacklist||[]).filter(function(c){ return c !== cmd; });
  api('POST', '/api/portal/agent/' + _selectedPolicyAgent + '/profile', { exec_blacklist: ag.profile.exec_blacklist });
  renderApprovals();
}
function addAgentWhitelist() {
  if (!_selectedPolicyAgent) return;
  var el = document.getElementById('new-whitelist');
  var val = (el.value||'').trim();
  if (!val) return;
  var ag = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  if (!ag) return;
  if (!ag.profile) ag.profile = {};
  var wl = ag.profile.exec_whitelist || [];
  if (wl.indexOf(val) === -1) { wl.push(val); ag.profile.exec_whitelist = wl; }
  el.value = '';
  api('POST', '/api/portal/agent/' + _selectedPolicyAgent + '/profile', { exec_whitelist: wl });
  renderApprovals();
}
function removeAgentWhitelist(cmd) {
  if (!_selectedPolicyAgent) return;
  var ag = agents.find(function(a){ return a.id === _selectedPolicyAgent; });
  if (!ag || !ag.profile) return;
  ag.profile.exec_whitelist = (ag.profile.exec_whitelist||[]).filter(function(c){ return c !== cmd; });
  api('POST', '/api/portal/agent/' + _selectedPolicyAgent + '/profile', { exec_whitelist: ag.profile.exec_whitelist });
  renderApprovals();
}

async function approveRequest(approvalId, scope) {
  var payload = {approval_id: approvalId, action: 'approve'};
  if (scope === 'session') payload.scope = 'session';
  await api('POST', '/api/portal/approve', payload);
  refresh();
}

async function denyRequest(approvalId) {
  await api('POST', '/api/portal/approve', {approval_id: approvalId, action: 'deny'});
  refresh();
}

// ============ Audit Log ============
function renderAudit() {
  const c = document.getElementById('content');
  c.innerHTML = `
    <div style="margin-bottom:12px">
      <select id="audit-filter" onchange="renderAudit()" style="padding:6px 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);color:var(--text)">
        <option value="">All Actions</option>
        <option value="login">Login</option>
        <option value="create_agent">Create Agent</option>
        <option value="chat">Chat</option>
        <option value="tool_call">Tool Call</option>
        <option value="approval">Approval</option>
      </select>
    </div>
    <table class="audit-table">
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>Action</th>
          <th>Actor</th>
          <th>Role</th>
          <th>Target</th>
          <th>Details</th>
          <th>IP</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        ${(auditLog||[]).slice().reverse().map(row => `
          <tr>
            <td>${new Date(row.timestamp*1000).toLocaleString()}</td>
            <td>${esc(row.action)}</td>
            <td>${esc(row.actor)}</td>
            <td>${esc(row.role)}</td>
            <td>${esc(row.target || '-')}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc((row.detail||'').slice(0,100))}</td>
            <td style="font-size:11px;color:var(--text3)">${esc(row.ip)}</td>
            <td><span class="tag ${row.success?'tag-green':'tag-red'}">${row.success?'OK':'Failed'}</span></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
  loadAuditLog();
}

async function loadAuditLog() {
  try {
    const filterEl = document.getElementById('audit-filter');
    const filter = filterEl ? filterEl.value : '';
    const data = await api('GET', '/api/portal/audit' + (filter ? '?action=' + filter : ''));
    if (!data) return;
    auditLog = (data.entries || []).reverse();
    const tbody = document.querySelector('.audit-table tbody');
    if (!tbody) return;
    tbody.innerHTML = (auditLog||[]).map(row => `
      <tr>
        <td>${new Date(row.timestamp*1000).toLocaleString()}</td>
        <td>${esc(row.action)}</td>
        <td>${esc(row.actor)}</td>
        <td>${esc(row.role)}</td>
        <td>${esc(row.target || '-')}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${esc((row.detail||'').slice(0,100))}</td>
        <td style="font-size:11px;color:var(--text3)">${esc(row.ip)}</td>
        <td><span class="tag ${row.success?'tag-green':'tag-red'}">${row.success?'OK':'Failed'}</span></td>
      </tr>
    `).join('');
  } catch(e) {}
}

// ============ Tokens ============
function renderTokens(container) {
  const c = container || document.getElementById('content');
  c.innerHTML = `
    <div style="margin-bottom:20px">
      <button class="btn btn-primary" onclick="showModal('create-token')">+ Create Token</button>
    </div>
    <div id="tokens-list"></div>
  `;
  loadTokens();
}

async function loadTokens() {
  try {
    const data = await api('GET', '/api/auth/tokens');
    if (!data) return;
    tokens = data.tokens || [];
    const container = document.getElementById('tokens-list');
    if (!container) return;
    container.innerHTML = tokens.map(t => {
      const adminLabel = t.admin_user_id ?
        `<span class="tag tag-blue" style="margin-left:6px" title="Bound to admin: ${esc(t.admin_user_id)}">` +
        `<span class="material-symbols-outlined" style="font-size:12px;vertical-align:middle;margin-right:2px">person</span>` +
        `${esc(t.admin_display_name || t.admin_user_id)}</span>` :
        '<span class="tag" style="margin-left:6px;opacity:0.5">Unbound</span>';
      return `
      <div class="token-item">
        <div class="token-info">
          <div style="font-weight:600;margin-bottom:4px">${esc(t.name)} ${adminLabel}</div>
          <div class="token-id">ID: ${esc(t.token_id)}</div>
          <div style="font-size:11px;color:var(--text3);margin-top:4px">
            Role: ${esc(t.role)} · Created: ${new Date(t.created_at*1000).toLocaleDateString()} ·
            Last used: ${t.last_used ? new Date(t.last_used*1000).toLocaleDateString() : 'Never'} ·
            ${t.active ? '<span class="tag tag-green">Active</span>' : '<span class="tag tag-red">Revoked</span>'}
          </div>
        </div>
        <div class="token-actions">
          ${t.active ? '<button class="btn btn-sm btn-danger" onclick="revokeToken(\''+t.token_id+'\')">Revoke</button>' : ''}
        </div>
      </div>`;
    }).join('');
  } catch(e) {}
}

async function createToken() {
  const name = document.getElementById('ct-name').value.trim();
  const role = document.getElementById('ct-role').value;
  const adminBind = document.getElementById('ct-admin-bind').value;
  if (!name) { alert('Please enter a token name'); return; }

  try {
    const body = {name, role};
    if (adminBind) body.admin_user_id = adminBind;
    const data = await api('POST', '/api/auth/tokens', body);
    if (!data) return;
    document.getElementById('ct-form').classList.add('hidden');
    document.getElementById('ct-result').classList.remove('hidden');
    document.getElementById('ct-token-display').textContent = data.raw_token || data.token;
  } catch(e) {
    alert('Error creating token: ' + e.message);
  }
}

function copyToken() {
  const text = document.getElementById('ct-token-display').textContent;
  navigator.clipboard.writeText(text).then(() => alert('Token copied to clipboard'));
}

async function revokeToken(tokenId) {
  if (!confirm('Revoke this token?')) return;
  await api('DELETE', '/api/auth/tokens/' + tokenId);
  loadTokens();
}

// ============ Nodes ============
function renderNodes(container) {
  var c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  var epoch = _renderEpoch;

  // Cache local agents for the node card render (before rendering)
  window._localAgentsForRender = {};
  agents.forEach(function(a) { window._localAgentsForRender[a.id] = a; });

  // Header
  var onlineCount = nodes.filter(function(n){ return n.status === 'online'; }).length;
  var remoteCount = nodes.filter(function(n){ return !n.is_self; }).length;
  c.innerHTML = '' +
    '<div style="margin-bottom:28px;display:flex;justify-content:space-between;align-items:flex-start">' +
      '<div>' +
        '<h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px">Network Nodes</h2>' +
        '<p style="color:var(--text2);font-size:14px;margin-top:4px">Manage connected machines and multi-node agent orchestration.</p>' +
      '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-node\')" style="flex-shrink:0"><span class="material-symbols-outlined" style="font-size:16px">add</span> Connect Node</button>' +
    '</div>' +
    '<div id="nodes-grid" style="display:grid;grid-template-columns:repeat(2,1fr);gap:20px"></div>';

  var grid = document.getElementById('nodes-grid');
  if (!grid) return;

  grid.innerHTML = nodes.map(function(n) {
    var isOnline = n.status === 'online';
    var isSelf = n.is_self;
    var nodeIcon = isSelf ? 'dns' : 'computer';
    var nodeLabel = isSelf ? 'LOCAL' : 'REMOTE';
    var agentCount = n.agent_count || 0;
    var agentList = (n.agents || []);

    return '<div style="background:var(--surface);border-radius:14px;padding:24px;border:1px solid var(--border-light);transition:all 0.2s" onmouseenter="this.style.background=\'var(--surface2)\'" onmouseleave="this.style.background=\'var(--surface)\'">' +
      // Top: icon + name + status
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">' +
        '<div style="display:flex;align-items:center;gap:14px">' +
          '<div style="width:48px;height:48px;background:var(--surface3);border-radius:10px;display:flex;align-items:center;justify-content:center"><span class="material-symbols-outlined" style="font-size:28px;color:var(--primary)">' + nodeIcon + '</span></div>' +
          '<div>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
              '<span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:16px;font-weight:700">' + esc(n.name) + '</span>' +
              '<span style="font-size:10px;background:' + (isSelf ? 'rgba(76,175,80,0.1)' : 'rgba(203,201,255,0.1)') + ';color:' + (isSelf ? 'var(--success)' : 'var(--primary)') + ';padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:0.5px;font-weight:700">' + nodeLabel + '</span>' +
            '</div>' +
            '<div style="font-size:12px;color:var(--text3);margin-top:2px">' + esc(n.url === 'local' ? 'localhost' : n.url) + ' · Node ID: ' + esc(n.node_id) + '</div>' +
          '</div>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:' + (isOnline ? 'var(--success)' : 'var(--error)') + '"><span style="width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;display:inline-block"></span>' + (isOnline ? 'ONLINE' : 'OFFLINE') + '</div>' +
      '</div>' +
      // Agents section
      '<div style="margin-bottom:20px">' +
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700;margin-bottom:8px">Agents (' + agentCount + ')</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
          (agentCount > 0 ?
            (isSelf ? Object.values(window._localAgentsForRender || {}).map(function(a) { return '<span style="background:var(--surface3);padding:5px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px">' + esc(a.name || 'Agent') + ' <span class="material-symbols-outlined" style="font-size:14px;color:' + (a.status === 'idle' ? 'var(--success)' : a.status === 'busy' ? 'var(--warning)' : 'var(--text3)') + '">' + (a.status === 'idle' ? 'check_circle' : a.status === 'busy' ? 'sync' : 'radio_button_unchecked') + '</span></span>'; }).join('') :
             agentList.map(function(a) { return '<span style="background:var(--surface3);padding:5px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px">' + esc(a.name || 'Remote Agent') + ' <span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">cloud</span></span>'; }).join('')) :
            '<span style="font-size:12px;color:var(--text3)">No agents on this node</span>') +
        '</div>' +
      '</div>' +
      // Stats row
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px">' +
        '<div style="background:var(--bg);border-radius:8px;padding:10px 12px;text-align:center"><div style="font-size:18px;font-weight:800;font-family:\'Plus Jakarta Sans\',sans-serif">' + agentCount + '</div><div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--text3);margin-top:2px">Agents</div></div>' +
        '<div style="background:var(--bg);border-radius:8px;padding:10px 12px;text-align:center"><div style="font-size:18px;font-weight:800;font-family:\'Plus Jakarta Sans\',sans-serif;color:' + (isOnline ? 'var(--success)' : 'var(--error)') + '">' + (isOnline ? '●' : '○') + '</div><div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--text3);margin-top:2px">Status</div></div>' +
        '<div style="background:var(--bg);border-radius:8px;padding:10px 12px;text-align:center"><div style="font-size:18px;font-weight:800;font-family:\'Plus Jakarta Sans\',sans-serif">' + (n.last_seen ? new Date(n.last_seen * 1000).toLocaleTimeString() : '-') + '</div><div style="font-size:9px;text-transform:uppercase;letter-spacing:0.6px;color:var(--text3);margin-top:2px">Last Seen</div></div>' +
      '</div>' +
      // Config deploy status bar
      '<div id="node-config-' + esc(n.node_id) + '" style="margin-bottom:16px;display:none"></div>' +
      // Actions row
      '<div style="display:flex;align-items:center;justify-content:space-between;padding-top:16px;border-top:1px solid var(--border-light)">' +
        '<div style="display:flex;gap:12px">' +
          (!isSelf ? '<button style="background:none;border:none;color:var(--primary);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0" onclick="refreshNode(\'' + esc(n.node_id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">refresh</span> Refresh</button>' : '<button style="background:none;border:none;color:var(--primary);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0" onclick="showView(\'dashboard\',null)"><span class="material-symbols-outlined" style="font-size:16px">dashboard</span> Dashboard</button>') +
          '<button style="background:none;border:none;color:var(--warning);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0" onclick="showPushConfig(\'' + esc(n.node_id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">settings_suggest</span> Push Config</button>' +
          '<button style="background:none;border:none;color:var(--text3);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0" onclick="loadNodeConfigStatus(\'' + esc(n.node_id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">verified</span> Config Status</button>' +
        '</div>' +
        (!isSelf ? '<button style="background:none;border:none;color:var(--error);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0;opacity:0.7" onclick="removeNode(\'' + esc(n.node_id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">link_off</span> Disconnect</button>' : '') +
      '</div>' +
    '</div>';
  }).join('') +
  (nodes.length === 0 ? '<div style="color:var(--text3);padding:20px;grid-column:1/-1">No nodes connected. Click "+ Connect Node" to add a remote machine.</div>' : '');
}

async function refreshNode(nodeId) { await api('POST','/api/hub/refresh',{node_id:nodeId}); refresh(); }
async function removeNode(nodeId) {
  if (!confirm('Disconnect this node?')) return;
  await api('DELETE','/api/hub/node/'+nodeId);
  refresh();
}
async function addNode() {
  var name = document.getElementById('an-name').value.trim();
  var url = document.getElementById('an-url').value.trim();
  if(!name||!url) { alert('Please fill in all fields'); return; }
  await api('POST', '/api/hub/register', {name: name, url: url, node_id: 'remote-' + Date.now()});
  hideModal('add-node');
  refresh();
}

// ============ Config Deployment ============

function showPushConfig(nodeId) {
  // Build agent selector from node's agents
  var node = nodes.find(function(n) { return n.node_id === nodeId; });
  if (!node) return;
  var agentOpts = '<option value="">-- Select Agent --</option>';
  var agentList = node.is_self
    ? Object.values(window._localAgentsForRender || {})
    : (node.agents || []);
  agentList.forEach(function(a) {
    agentOpts += '<option value="' + esc(a.id) + '">' + esc(a.name || a.id) + '</option>';
  });

  var html = '<div style="position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center" id="config-modal" onclick="if(event.target===this)this.remove()">' +
    '<div style="background:var(--surface);border-radius:16px;padding:32px;width:520px;max-height:80vh;overflow:auto;border:1px solid var(--border-light)">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">' +
        '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:20px;font-weight:800;margin:0">Push Config to Node</h3>' +
        '<button onclick="document.getElementById(\'config-modal\').remove()" style="background:none;border:none;cursor:pointer;color:var(--text3)"><span class="material-symbols-outlined">close</span></button>' +
      '</div>' +
      '<div style="font-size:12px;color:var(--text3);margin-bottom:16px">Node: <b>' + esc(node.name) + '</b> (' + esc(nodeId) + ')</div>' +
      '<div style="display:flex;flex-direction:column;gap:14px">' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Target Agent</label>' +
          '<select id="pc-agent" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px">' + agentOpts + '</select></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Model</label>' +
          '<input id="pc-model" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px;box-sizing:border-box" placeholder="e.g. qwen3-235b-a22b"></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Provider</label>' +
          '<input id="pc-provider" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px;box-sizing:border-box" placeholder="e.g. ollama, openai"></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Role</label>' +
          '<input id="pc-role" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px;box-sizing:border-box" placeholder="e.g. coder, reviewer"></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">System Prompt</label>' +
          '<textarea id="pc-sysprompt" rows="3" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:13px;margin-top:4px;box-sizing:border-box;resize:vertical" placeholder="Optional system prompt override"></textarea></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Working Directory</label>' +
          '<input id="pc-workdir" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px;box-sizing:border-box" placeholder="/path/to/project"></div>' +
        '<div><label style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px">Temperature</label>' +
          '<input id="pc-temp" type="number" step="0.1" min="0" max="2" style="width:100%;padding:10px 12px;border:1px solid var(--border-light);border-radius:8px;background:var(--bg);color:var(--text1);font-size:14px;margin-top:4px;box-sizing:border-box" placeholder="0.7"></div>' +
      '</div>' +
      '<div style="display:flex;gap:12px;justify-content:flex-end;margin-top:24px">' +
        '<button onclick="document.getElementById(\'config-modal\').remove()" style="padding:10px 20px;border:1px solid var(--border-light);border-radius:8px;background:none;color:var(--text2);font-size:13px;cursor:pointer">Cancel</button>' +
        '<button onclick="doPushConfig(\'' + esc(nodeId) + '\')" style="padding:10px 24px;border:none;border-radius:8px;background:var(--primary);color:#fff;font-size:13px;font-weight:700;cursor:pointer">Deploy Config</button>' +
      '</div>' +
      '<div id="pc-result" style="margin-top:16px;display:none"></div>' +
    '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

async function doPushConfig(nodeId) {
  var agentId = document.getElementById('pc-agent').value;
  if (!agentId) { alert('Please select an agent'); return; }
  var config = {};
  var model = document.getElementById('pc-model').value.trim();
  var provider = document.getElementById('pc-provider').value.trim();
  var role = document.getElementById('pc-role').value.trim();
  var sysprompt = document.getElementById('pc-sysprompt').value.trim();
  var workdir = document.getElementById('pc-workdir').value.trim();
  var temp = document.getElementById('pc-temp').value.trim();
  if (model) config.model = model;
  if (provider) config.provider = provider;
  if (role) config.role = role;
  if (sysprompt) config.system_prompt = sysprompt;
  if (workdir) config.working_dir = workdir;
  if (temp) config.profile = {temperature: parseFloat(temp)};
  config.partial = true;

  var resultDiv = document.getElementById('pc-result');
  resultDiv.style.display = 'block';
  resultDiv.innerHTML = '<div style="color:var(--text3);font-size:12px"><span class="material-symbols-outlined" style="font-size:14px;animation:spin 1s linear infinite">sync</span> Deploying...</div>';

  try {
    var res = await api('POST', '/api/hub/dispatch-config', {
      node_id: nodeId, agent_id: agentId, config: config
    });
    var statusColor = res.status === 'applied' ? 'var(--success)' : res.status === 'failed' ? 'var(--error)' : 'var(--warning)';
    var statusIcon = res.status === 'applied' ? 'check_circle' : res.status === 'failed' ? 'error' : 'schedule';
    resultDiv.innerHTML = '<div style="background:var(--bg);border-radius:8px;padding:12px;border:1px solid ' + statusColor + '">' +
      '<div style="display:flex;align-items:center;gap:8px;color:' + statusColor + ';font-weight:700;font-size:13px"><span class="material-symbols-outlined" style="font-size:18px">' + statusIcon + '</span>' + res.status.toUpperCase() + '</div>' +
      '<div style="font-size:11px;color:var(--text3);margin-top:4px">Deploy ID: ' + (res.deploy_id || '-') + (res.error ? ' · Error: ' + res.error : '') + '</div>' +
      (res.duration ? '<div style="font-size:11px;color:var(--text3)">Duration: ' + res.duration.toFixed(2) + 's</div>' : '') +
    '</div>';
    // Auto-refresh after successful apply
    if (res.status === 'applied') {
      setTimeout(function() { refresh(); }, 1000);
    }
  } catch(e) {
    resultDiv.innerHTML = '<div style="color:var(--error);font-size:12px">Error: ' + e.message + '</div>';
  }
}

async function loadNodeConfigStatus(nodeId) {
  var el = document.getElementById('node-config-' + nodeId);
  if (!el) return;
  // Toggle visibility
  if (el.style.display === 'block') { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = '<div style="color:var(--text3);font-size:12px">Loading config status...</div>';
  try {
    var data = await api('GET', '/api/portal/node/' + nodeId + '/config-status');
    if (data.total === 0) {
      el.innerHTML = '<div style="background:var(--bg);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--text3)">No config deployments for this node.</div>';
      return;
    }
    var html = '<div style="background:var(--bg);border-radius:8px;padding:12px">' +
      '<div style="display:flex;gap:16px;margin-bottom:10px;font-size:11px">' +
        '<span style="color:var(--success);font-weight:700">' + data.applied + ' Applied</span>' +
        '<span style="color:var(--warning);font-weight:700">' + data.pending + ' Pending</span>' +
        '<span style="color:var(--error);font-weight:700">' + data.failed + ' Failed</span>' +
      '</div>' +
      '<div style="display:flex;flex-direction:column;gap:6px;max-height:200px;overflow:auto">';
    (data.deployments || []).slice(0, 10).forEach(function(d) {
      var sc = d.status === 'applied' ? 'var(--success)' : d.status === 'failed' ? 'var(--error)' : 'var(--warning)';
      var si = d.status === 'applied' ? 'check_circle' : d.status === 'failed' ? 'error' : 'schedule';
      html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;background:var(--surface);font-size:11px">' +
        '<span class="material-symbols-outlined" style="font-size:14px;color:' + sc + '">' + si + '</span>' +
        '<span style="color:var(--text2);font-weight:600">' + esc(d.agent_id || '-').substr(0, 8) + '</span>' +
        '<span style="color:' + sc + ';font-weight:700">' + d.status + '</span>' +
        '<span style="color:var(--text3);margin-left:auto">' + new Date(d.created_at * 1000).toLocaleTimeString() + '</span>' +
        (d.error ? '<span style="color:var(--error)" title="' + esc(d.error) + '">⚠</span>' : '') +
      '</div>';
    });
    html += '</div></div>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:var(--error);font-size:12px">Error loading config status</div>';
  }
}

// ============ AI Agent Office ============
var _officeAnimFrame = null;
var _officeRobots = [];
var _officeCurrentNode = 'all';

// 像素机器人颜色方案
var _robotColors = {
  coder:    {body:'#4CAF50', eye:'#fff', accent:'#81C784'},
  pm:       {body:'#FF9800', eye:'#fff', accent:'#FFB74D'},
  architect:{body:'#2196F3', eye:'#fff', accent:'#64B5F6'},
  tester:   {body:'#9C27B0', eye:'#fff', accent:'#CE93D8'},
  devops:   {body:'#F44336', eye:'#fff', accent:'#E57373'},
  writer:   {body:'#00BCD4', eye:'#fff', accent:'#4DD0E1'},
  security: {body:'#607D8B', eye:'#fff', accent:'#90A4AE'},
  general:  {body:'#CBC1FF', eye:'#fff', accent:'#9B8FFF'},
  designer: {body:'#E91E63', eye:'#fff', accent:'#F48FB1'},
  hr:       {body:'#CDDC39', eye:'#fff', accent:'#DCE775'},
  marketing:{body:'#FF5722', eye:'#fff', accent:'#FF8A65'},
  data_analyst:{body:'#3F51B5', eye:'#fff', accent:'#7986CB'},
  support:  {body:'#009688', eye:'#fff', accent:'#4DB6AC'},
};

function _getRobotColor(role) {
  return _robotColors[role] || _robotColors['general'];
}

/* Resolve robot role key from agent profile — use robot_avatar config first */
function _resolveRobotRole(a) {
  // robot_avatar field: "robot_coder" → "coder", "robot_cto" → "cto"
  if (a.robot_avatar) {
    var r = a.robot_avatar.replace(/^robot_/, '');
    if (_robotColors[r]) return r;
  }
  // Fallback to agent role
  return a.role || 'general';
}

/* Generate a tiny pixel-robot avatar as a data-URL (32×36 canvas) */
function _miniRobotDataURL(role) {
  var c = document.createElement('canvas');
  c.width = 32; c.height = 36;
  var ctx = c.getContext('2d');
  var col = _getRobotColor(role);
  var s = 2, cx = 16, cy = 14;
  // head
  ctx.fillStyle = col.body;
  ctx.fillRect(cx-4*s, cy-5*s, 8*s, 7*s);
  // antenna
  ctx.fillStyle = col.accent;
  ctx.fillRect(cx-s, cy-7*s, 2*s, 2*s);
  // eyes
  ctx.fillStyle = col.eye;
  ctx.fillRect(cx+s, cy-3*s, 2*s, 2*s);
  ctx.fillRect(cx-3*s, cy-3*s, 2*s, 2*s);
  ctx.fillStyle = '#333';
  ctx.fillRect(cx+s, cy-3*s, s, 2*s);
  ctx.fillRect(cx-3*s, cy-3*s, s, 2*s);
  // mouth
  ctx.fillStyle = '#333';
  ctx.fillRect(cx-s, cy, 2*s, s);
  // body
  ctx.fillStyle = col.body;
  ctx.fillRect(cx-3*s, cy+2*s, 6*s, 6*s);
  // chest emblem
  ctx.fillStyle = col.accent;
  ctx.fillRect(cx-s, cy+3*s, 2*s, 2*s);
  // arms
  ctx.fillStyle = col.body;
  ctx.fillRect(cx-5*s, cy+3*s, 2*s, 3*s);
  ctx.fillRect(cx+3*s, cy+3*s, 2*s, 3*s);
  // legs
  ctx.fillStyle = col.accent;
  ctx.fillRect(cx-2*s, cy+8*s, 2*s, 2*s);
  ctx.fillRect(cx, cy+8*s, 2*s, 2*s);
  return c.toDataURL();
}

function renderMessages() {
  var c = document.getElementById('content');
  c.style.padding = '0';

  // 上下分栏：上=办公室场景，下=消息列表
  c.innerHTML = '' +
    '<div style="display:flex;flex-direction:column;height:100%;overflow:hidden">' +
      '<!-- 上半：办公室像素场景 -->' +
      '<div style="flex:0 0 320px;position:relative;background:#1a1a2e;border-bottom:3px solid var(--primary);overflow:hidden">' +
        '<canvas id="office-canvas" style="width:100%;height:100%;display:block"></canvas>' +
        '<!-- Node tabs bar -->' +
        '<div style="position:absolute;top:8px;left:16px;right:16px;display:flex;align-items:center;gap:6px;z-index:10">' +
          '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">apartment</span>' +
          '<span style="font-family:monospace;font-size:11px;color:var(--primary);font-weight:800">AI AGENT OFFICE</span>' +
          '<div id="office-node-tabs" style="display:flex;gap:4px;margin-left:12px"></div>' +
          '<span id="office-agent-count" style="font-size:10px;color:var(--text3);margin-left:auto;background:rgba(255,255,255,0.1);padding:2px 8px;border-radius:10px"></span>' +
        '</div>' +
      '</div>' +
      '<!-- 下半：消息列表 -->' +
      '<div style="flex:1;overflow-y:auto;padding:16px 20px;background:var(--bg)" id="office-messages">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">' +
          '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">forum</span>' +
          '<span style="font-size:14px;font-weight:700">Agent Messages</span>' +
          '<span style="font-size:11px;color:var(--text3)" id="office-msg-count"></span>' +
        '</div>' +
        '<div id="office-msg-list" style="display:flex;flex-direction:column;gap:8px"></div>' +
      '</div>' +
    '</div>';

  _initOfficeScene();
  _renderOfficeMessages();
}

function _renderOfficeMessages() {
  var list = document.getElementById('office-msg-list');
  var countEl = document.getElementById('office-msg-count');
  if (!list) return;
  if (countEl) countEl.textContent = messages.length ? '(' + messages.length + ')' : '';

  if (messages.length === 0) {
    list.innerHTML = '<div style="text-align:center;padding:30px;color:var(--text3);font-size:12px"><span class="material-symbols-outlined" style="font-size:32px;display:block;margin-bottom:8px;opacity:0.4">forum</span>暂无 Agent 间消息<br>当 Agent 之间互相通信时，消息将显示在这里</div>';
    return;
  }

  list.innerHTML = messages.slice().reverse().map(function(m) {
    var statusColor = m.status === 'completed' ? '#3fb950' : m.status === 'pending' ? '#f0883e' : 'var(--primary)';
    var statusIcon = m.status === 'completed' ? 'check_circle' : m.status === 'pending' ? 'schedule' : 'sync';
    var typeIcon = m.msg_type === 'task' ? 'assignment' : m.msg_type === 'response' ? 'reply' : 'chat';
    var fromName = m.from_agent_name || agentName(m.from_agent);
    var toName = m.to_agent_name || agentName(m.to_agent);
    return '<div style="background:var(--surface);border-radius:10px;padding:12px 14px;border:1px solid rgba(255,255,255,0.05);font-size:12px">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
        '<span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">' + typeIcon + '</span>' +
        '<span style="font-weight:700;color:var(--text)">' + esc(fromName) + '</span>' +
        '<span class="material-symbols-outlined" style="font-size:12px;color:var(--text3)">arrow_forward</span>' +
        '<span style="font-weight:700;color:var(--text)">' + esc(toName) + '</span>' +
        '<span style="margin-left:auto;font-size:10px;color:var(--text3)">' + new Date(m.timestamp * 1000).toLocaleTimeString() + '</span>' +
        '<span style="font-size:10px;color:' + statusColor + ';font-weight:700">' + esc(m.status) + '</span>' +
      '</div>' +
      '<div style="color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc((m.content || '').slice(0, 200)) + '</div>' +
    '</div>';
  }).join('');
}

// ── roundRect polyfill (Safari < 16, older browsers) ──
if (typeof CanvasRenderingContext2D !== 'undefined' &&
    !CanvasRenderingContext2D.prototype.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function(x, y, w, h, r) {
    if (typeof r === 'number') r = [r, r, r, r];
    var tl = r[0] || 0, tr = r[1] || tl, br = r[2] || tl, bl = r[3] || tl;
    this.moveTo(x + tl, y);
    this.lineTo(x + w - tr, y);
    this.quadraticCurveTo(x + w, y, x + w, y + tr);
    this.lineTo(x + w, y + h - br);
    this.quadraticCurveTo(x + w, y + h, x + w - br, y + h);
    this.lineTo(x + bl, y + h);
    this.quadraticCurveTo(x, y + h, x, y + h - bl);
    this.lineTo(x, y + tl);
    this.quadraticCurveTo(x, y, x + tl, y);
    this.closePath();
    return this;
  };
}

// ── Node tabs rendering for office ──

function _renderNodeTabs() {
  var tabsEl = document.getElementById('office-node-tabs');
  if (!tabsEl) return;

  // Extract unique nodes from agents
  var nodeMap = {};
  agents.forEach(function(a) {
    var nid = a.node_id || 'local';
    if (!nodeMap[nid]) {
      nodeMap[nid] = {
        id: nid,
        name: nid === 'local' ? 'Local' : nid,
        count: 0,
        busy: 0
      };
    }
    nodeMap[nid].count++;
    if (a.status === 'busy') nodeMap[nid].busy++;
  });

  var nodes = Object.values(nodeMap).sort(function(a, b) {
    if (a.id === 'local') return -1;
    if (b.id === 'local') return 1;
    return a.id.localeCompare(b.id);
  });

  var html = '<div onclick="_switchOfficeNode(\'all\')" class="office-tab" ' +
    'style="cursor:pointer;padding:2px 8px;border-radius:8px;font-size:10px;' +
    (_officeCurrentNode === 'all' ? 'background:var(--primary);color:#fff' : 'background:rgba(255,255,255,0.08);color:var(--text3)') + '">' +
    'All (' + agents.length + ')</div>';

  nodes.forEach(function(n) {
    var isActive = _officeCurrentNode === n.node_id;
    var statusDot = '🟢';
    html += '<div onclick="_switchOfficeNode(\'' + esc(n.node_id) + '\')" class="office-tab" ' +
      'style="cursor:pointer;padding:2px 8px;border-radius:8px;font-size:10px;' +
      (isActive ? 'background:var(--primary);color:#fff' : 'background:rgba(255,255,255,0.08);color:var(--text3)') + '">' +
      statusDot + ' ' + esc(n.name) + ' (' + n.count + ')</div>';
  });

  tabsEl.innerHTML = html;
}

function _switchOfficeNode(nodeId) {
  _officeCurrentNode = nodeId;
  _initOfficeScene();
  _renderNodeTabs();
}

// ── 像素办公室场景 Canvas 引擎 ──

function _initOfficeScene() {
  var canvas = document.getElementById('office-canvas');
  if (!canvas) return;
  var parent = canvas.parentElement;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = parent.clientWidth * dpr;
  canvas.height = parent.clientHeight * dpr;
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var W = parent.clientWidth;
  var H = parent.clientHeight;

  // Filter agents by current node selection
  var filteredAgents = _officeCurrentNode === 'all' ?
    agents :
    agents.filter(function(a) { return (a.node_id || 'local') === _officeCurrentNode; });

  // Render node tabs
  _renderNodeTabs();

  // 生成办公桌位置（最多 8 张桌子）
  var deskCount = Math.min(filteredAgents.length, 8);
  var deskSpacing = W / (deskCount + 1);
  var deskY = H * 0.42; // 桌子 Y 位置
  var floorY = H * 0.75; // 地板走动 Y 位置

  // 初始化机器人状态
  _officeRobots = filteredAgents.slice(0, 12).map(function(a, i) {
    var deskIdx = i < deskCount ? i : -1;
    var robotRole = _resolveRobotRole(a);
    var col = _getRobotColor(robotRole);
    // Detect learning state from self_improvement stats
    var si = a.self_improvement || {};
    var isLearning = si.is_learning || false;
    var isBusyOrLearning = a.status === 'busy' || isLearning;
    // Determine bubble text
    var bubble = '';
    if (a.status === 'busy') bubble = '工作中...';
    else if (isLearning) bubble = '📖 学习中...';
    else if (si.learning_queue_count > 0) bubble = '📋 待学习: ' + si.learning_queue_count;
    return {
      id: a.id,
      name: a.name || 'Agent',
      role: robotRole,
      status: a.status || 'idle',
      isLearning: isLearning,
      color: col,
      // 桌子位置
      deskX: deskIdx >= 0 ? deskSpacing * (deskIdx + 1) : 0,
      deskY: deskY,
      // 当前位置（动画用）
      x: deskIdx >= 0 ? deskSpacing * (deskIdx + 1) : 60 + Math.random() * (W - 120),
      y: isBusyOrLearning ? deskY - 16 : floorY,
      // 走路方向和速度
      vx: (Math.random() > 0.5 ? 1 : -1) * (0.3 + Math.random() * 0.5),
      walkFrame: Math.floor(Math.random() * 4),
      walkTimer: 0,
      // 气泡
      bubbleText: bubble,
      bubbleTimer: 0,
      facingRight: Math.random() > 0.5,
    };
  });

  var agentCountEl = document.getElementById('office-agent-count');
  if (agentCountEl) {
    var busyCount = filteredAgents.filter(function(a){return a.status==='busy'}).length;
    agentCountEl.textContent = filteredAgents.length + ' agents · ' + busyCount + ' working';
  }

  // 动画循环
  if (_officeAnimFrame) cancelAnimationFrame(_officeAnimFrame);

  function animate() {
    // 检查是否还在当前视图
    if (!document.getElementById('office-canvas')) {
      _officeAnimFrame = null;
      return;
    }

    ctx.clearRect(0, 0, W, H);

    // ── 背景：夜间办公室 ──
    // 天花板
    ctx.fillStyle = '#12122a';
    ctx.fillRect(0, 0, W, 30);

    // 墙壁
    var wallGrad = ctx.createLinearGradient(0, 30, 0, H * 0.65);
    wallGrad.addColorStop(0, '#1a1a3e');
    wallGrad.addColorStop(1, '#16213e');
    ctx.fillStyle = wallGrad;
    ctx.fillRect(0, 30, W, H * 0.65 - 30);

    // 窗户（像素风格）
    var winCount = Math.floor(W / 200);
    for (var wi = 0; wi < winCount; wi++) {
      var wx = 100 + wi * 200;
      // 窗框
      ctx.fillStyle = '#2a2a5a';
      ctx.fillRect(wx - 32, 50, 64, 50);
      // 窗户玻璃（蓝紫色夜空）
      ctx.fillStyle = '#1e3a5f';
      ctx.fillRect(wx - 28, 54, 25, 42);
      ctx.fillRect(wx + 3, 54, 25, 42);
      // 星星
      ctx.fillStyle = '#fff';
      ctx.fillRect(wx - 20, 60, 2, 2);
      ctx.fillRect(wx + 10, 68, 2, 2);
      ctx.fillRect(wx - 10, 75, 2, 2);
    }

    // 地板
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(0, H * 0.65, W, H * 0.35);
    // 地板网格线
    ctx.strokeStyle = 'rgba(255,255,255,0.03)';
    ctx.lineWidth = 1;
    for (var gy = H * 0.65; gy < H; gy += 20) {
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
    }

    // ── 绘制办公桌 ──
    for (var di = 0; di < deskCount; di++) {
      var dx = deskSpacing * (di + 1);
      var dy = deskY;
      // 桌面
      ctx.fillStyle = '#3d2b1f';
      ctx.fillRect(dx - 28, dy + 10, 56, 8);
      // 桌腿
      ctx.fillStyle = '#2a1f15';
      ctx.fillRect(dx - 24, dy + 18, 4, 22);
      ctx.fillRect(dx + 20, dy + 18, 4, 22);
      // 电脑显示器
      ctx.fillStyle = '#333';
      ctx.fillRect(dx - 12, dy - 14, 24, 20);
      // 屏幕（蓝光 = 有人在用，灰色 = 空闲）
      var deskRobot = _officeRobots.find(function(r){return r.status === 'busy' && r.deskX === dx;});
      ctx.fillStyle = deskRobot ? '#4a90d9' : '#222';
      ctx.fillRect(dx - 10, dy - 12, 20, 16);
      // 显示器支架
      ctx.fillStyle = '#555';
      ctx.fillRect(dx - 2, dy + 6, 4, 4);
      // 键盘
      ctx.fillStyle = '#444';
      ctx.fillRect(dx - 10, dy + 12, 20, 4);
    }

    // ── 更新和绘制机器人 ──
    _officeRobots.forEach(function(r) {
      // 从 agents 数据同步状态
      var agentData = agents.find(function(a){return a.id === r.id;});
      if (agentData) {
        r.status = agentData.status || 'idle';
        // Sync learning state
        var si = agentData.self_improvement || {};
        r.isLearning = si.is_learning || false;
        // Update color from profile (in case avatar changed)
        var robotRole = _resolveRobotRole(agentData);
        r.role = robotRole;
        r.color = _getRobotColor(robotRole);
      }

      var isBusyOrLearning = r.status === 'busy' || r.isLearning;

      if (isBusyOrLearning) {
        // BUSY or LEARNING: 坐在桌子前
        if (r.deskX > 0) {
          r.x += (r.deskX - r.x) * 0.08;
          r.y += ((r.deskY - 16) - r.y) * 0.08;
        }
        if (r.status === 'busy') {
          r.bubbleText = '工作中...';
        } else if (r.isLearning) {
          r.bubbleText = '📖 学习中...';
        }
      } else {
        // IDLE: 在地板上来回走
        r.y += (floorY - r.y) * 0.05;
        r.x += r.vx;
        r.facingRight = r.vx > 0;
        // 碰到边界反弹
        if (r.x < 30 || r.x > W - 30) {
          r.vx = -r.vx;
          r.facingRight = r.vx > 0;
        }
        // 随机转向
        if (Math.random() < 0.003) r.vx = -r.vx;
        // Show queued learning count if any
        var si2 = agentData ? (agentData.self_improvement || {}) : {};
        if (si2.learning_queue_count > 0) {
          r.bubbleText = '📋 待学习';
        } else {
          r.bubbleText = '';
        }
      }

      // 走路动画帧
      r.walkTimer++;
      if (r.walkTimer > 10) {
        r.walkTimer = 0;
        r.walkFrame = (r.walkFrame + 1) % 4;
      }

      _drawPixelRobot(ctx, r);
    });

    _officeAnimFrame = requestAnimationFrame(animate);
  }

  animate();
}

function _drawPixelRobot(ctx, r) {
  var x = Math.round(r.x);
  var y = Math.round(r.y);
  var s = 3; // 像素尺寸
  var col = r.color;
  var isBusy = r.status === 'busy' || r.isLearning;
  var frame = r.walkFrame;

  ctx.save();

  // ── 气泡（工作中/学习中...）──
  if (r.bubbleText) {
    ctx.font = '10px "Plus Jakarta Sans", sans-serif';
    var tw = ctx.measureText(r.bubbleText).width;
    var bx = x - tw / 2 - 6;
    var by = y - 36;
    // 气泡背景 — 学习用绿色底，工作用深色底
    var bubbleBg = r.isLearning ? 'rgba(76,175,80,0.85)' : 'rgba(0,0,0,0.7)';
    ctx.fillStyle = bubbleBg;
    ctx.beginPath();
    ctx.roundRect(bx, by, tw + 12, 18, 4);
    ctx.fill();
    // 气泡文字 — 学习用白色，工作用金色
    ctx.fillStyle = r.isLearning ? '#fff' : '#FFD700';
    ctx.fillText(r.bubbleText, bx + 6, by + 13);
    // 小三角
    ctx.fillStyle = bubbleBg;
    ctx.beginPath();
    ctx.moveTo(x - 3, by + 18);
    ctx.lineTo(x + 3, by + 18);
    ctx.lineTo(x, by + 23);
    ctx.fill();
  }

  // ── 头部 ──
  ctx.fillStyle = col.body;
  ctx.fillRect(x - 4*s, y - 5*s, 8*s, 7*s);

  // 天线
  ctx.fillStyle = col.accent;
  ctx.fillRect(x - s, y - 7*s, 2*s, 2*s);
  // 天线灯（busy 时闪烁）
  if (isBusy && Math.floor(Date.now() / 400) % 2 === 0) {
    ctx.fillStyle = '#FF0';
    ctx.fillRect(x - s, y - 8*s, 2*s, s);
  }

  // 眼睛
  ctx.fillStyle = col.eye;
  if (r.facingRight) {
    ctx.fillRect(x + s, y - 3*s, 2*s, 2*s);
    ctx.fillRect(x - 2*s, y - 3*s, 2*s, 2*s);
    // 瞳孔
    ctx.fillStyle = '#333';
    ctx.fillRect(x + 2*s, y - 3*s, s, 2*s);
    ctx.fillRect(x - s, y - 3*s, s, 2*s);
  } else {
    ctx.fillRect(x + s, y - 3*s, 2*s, 2*s);
    ctx.fillRect(x - 3*s, y - 3*s, 2*s, 2*s);
    ctx.fillStyle = '#333';
    ctx.fillRect(x + s, y - 3*s, s, 2*s);
    ctx.fillRect(x - 3*s, y - 3*s, s, 2*s);
  }

  // 嘴巴
  ctx.fillStyle = '#333';
  ctx.fillRect(x - s, y, 2*s, s);

  // ── 身体 ──
  ctx.fillStyle = col.body;
  ctx.fillRect(x - 3*s, y + 2*s, 6*s, 6*s);

  // 胸前标志
  ctx.fillStyle = col.accent;
  ctx.fillRect(x - s, y + 3*s, 2*s, 2*s);

  // ── 手臂 ──
  if (isBusy) {
    // busy: 手臂伸向前方（敲键盘）
    var armBob = Math.floor(Date.now() / 200) % 2;
    ctx.fillStyle = col.body;
    ctx.fillRect(x - 5*s, y + 3*s, 2*s, 3*s + armBob*s);
    ctx.fillRect(x + 3*s, y + 3*s, 2*s, 3*s + (1-armBob)*s);
  } else {
    // idle: 手臂随走路摆动
    var armSwing = (frame % 2 === 0) ? 1 : -1;
    ctx.fillStyle = col.body;
    ctx.fillRect(x - 5*s, y + (3 + armSwing)*s, 2*s, 3*s);
    ctx.fillRect(x + 3*s, y + (3 - armSwing)*s, 2*s, 3*s);
  }

  // ── 腿 ──
  ctx.fillStyle = col.accent;
  if (isBusy) {
    // 坐着
    ctx.fillRect(x - 2*s, y + 8*s, 2*s, 2*s);
    ctx.fillRect(x, y + 8*s, 2*s, 2*s);
  } else {
    // 走路
    var legOffset = (frame < 2) ? 1 : -1;
    ctx.fillRect(x - 2*s, y + 8*s + legOffset*s, 2*s, 3*s);
    ctx.fillRect(x, y + 8*s - legOffset*s, 2*s, 3*s);
  }

  // ── 名牌 ──
  ctx.font = 'bold 9px "Plus Jakarta Sans", sans-serif';
  var nameW = ctx.measureText(r.name).width;
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  ctx.beginPath();
  ctx.roundRect(x - nameW/2 - 4, y + 12*s, nameW + 8, 14, 3);
  ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.fillText(r.name, x - nameW/2, y + 12*s + 10);

  ctx.restore();
}

// ============ System Prompts ============
var _scenePrompts = [];
var _availableRoles = []; // populated from role_presets keys

function _spScopeLabel(sp) {
  var scope = sp.scope || 'all';
  if (scope === 'all') return '<span style="background:var(--primary);color:#fff;padding:1px 6px;border-radius:4px;font-size:10px">All Agents</span>';
  var roles = sp.roles || [];
  if (roles.length === 0) return '<span style="background:var(--warning,#f0ad4e);color:#fff;padding:1px 6px;border-radius:4px;font-size:10px">No roles</span>';
  return roles.map(function(r) {
    return '<span style="background:var(--surface3);padding:1px 6px;border-radius:4px;font-size:10px;color:var(--text2)">' + esc(r) + '</span>';
  }).join(' ');
}

function _spRender(prompts) {
  _scenePrompts = prompts || [];
  var el = document.getElementById('scene-prompts-list');
  if (!el) return;
  if (_scenePrompts.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);text-align:center;padding:16px 0;font-size:13px">No system prompts yet. Click "Add Prompt" to create one.</div>';
    return;
  }
  var html = '';
  _scenePrompts.forEach(function(sp, i) {
    var enabled = sp.enabled !== false;
    var preview = (sp.prompt || '').slice(0, 100) + ((sp.prompt || '').length > 100 ? '...' : '');
    html += '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:8px;border:1px solid ' + (enabled ? 'var(--border-light)' : 'var(--border)') + ';opacity:' + (enabled ? '1' : '0.6') + '">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">'
        + '<div style="display:flex;align-items:center;gap:8px">'
          + '<label style="cursor:pointer;display:flex;align-items:center"><input type="checkbox" ' + (enabled ? 'checked' : '') + ' onchange="_spToggle(' + i + ',this.checked)" style="margin:0"></label>'
          + '<span style="font-weight:600;font-size:13px;color:var(--text)">' + esc(sp.name || 'Unnamed') + '</span>'
          + _spScopeLabel(sp)
        + '</div>'
        + '<div style="display:flex;gap:4px">'
          + '<button class="btn btn-sm" onclick="_spEdit(' + i + ')" style="padding:3px 8px;font-size:11px"><span class="material-symbols-outlined" style="font-size:14px">edit</span></button>'
          + '<button class="btn btn-sm" onclick="_spDelete(' + i + ')" style="padding:3px 8px;font-size:11px;color:var(--error)"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button>'
        + '</div>'
      + '</div>'
      + '<div style="font-size:12px;color:var(--text3);line-height:1.4;white-space:pre-wrap">' + esc(preview) + '</div>'
    + '</div>';
  });
  el.innerHTML = html;
}

function _spAdd() {
  _spShowEditor(-1, {name: '', prompt: '', enabled: true, scope: 'all', roles: []});
}

function _spEdit(idx) {
  var sp = _scenePrompts[idx];
  if (!sp) return;
  _spShowEditor(idx, sp);
}

function _spShowEditor(idx, sp) {
  var existing = document.getElementById('sp-edit-modal');
  if (existing) existing.remove();

  var scope = sp.scope || 'all';
  var roles = sp.roles || [];

  // Build role checkboxes
  var roleChecks = _availableRoles.map(function(r) {
    var checked = roles.indexOf(r) >= 0 ? ' checked' : '';
    return '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:12px;padding:2px 0">'
      + '<input type="checkbox" class="sp-role-cb" value="' + esc(r) + '"' + checked + ' style="margin:0">'
      + esc(r) + '</label>';
  }).join('');

  var modal = document.createElement('div');
  modal.id = 'sp-edit-modal';
  modal.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';
  modal.innerHTML = '<div style="background:var(--surface);border-radius:12px;padding:24px;width:90%;max-width:600px;max-height:80vh;overflow-y:auto">'
    + '<div style="font-size:16px;font-weight:700;margin-bottom:16px">' + (idx >= 0 ? 'Edit System Prompt' : 'New System Prompt') + '</div>'
    + '<div class="form-group" style="margin-bottom:12px"><label style="font-size:12px">Name · 名称</label>'
    + '<input id="sp-edit-name" value="' + esc(sp.name || '').replace(/"/g,'&quot;') + '" style="width:100%;padding:8px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px" placeholder="e.g. 全局规则 / 编码规范 / 运维操作"></div>'
    // Scope selector
    + '<div class="form-group" style="margin-bottom:12px"><label style="font-size:12px">Scope · 作用范围</label>'
    + '<div style="display:flex;gap:12px;margin-top:4px">'
      + '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:13px"><input type="radio" name="sp-scope" value="all"' + (scope === 'all' ? ' checked' : '') + ' onchange="_spScopeChanged()"> All Agents · 所有智能体</label>'
      + '<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:13px"><input type="radio" name="sp-scope" value="roles"' + (scope === 'roles' ? ' checked' : '') + ' onchange="_spScopeChanged()"> Specific Roles · 指定角色</label>'
    + '</div></div>'
    // Role checkboxes (shown when scope=roles)
    + '<div id="sp-roles-panel" style="margin-bottom:12px;padding:10px;background:var(--surface2);border-radius:6px;display:' + (scope === 'roles' ? 'block' : 'none') + '">'
      + '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">选择需要遵从此提示词的角色：</div>'
      + '<div style="display:flex;flex-wrap:wrap;gap:8px 16px">' + roleChecks + '</div>'
    + '</div>'
    // Prompt content
    + '<div class="form-group" style="margin-bottom:12px"><label style="font-size:12px">Prompt Content · 提示词内容</label>'
    + '<textarea id="sp-edit-prompt" rows="10" style="width:100%;padding:10px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;font-family:\'Fira Code\',\'Cascadia Code\',monospace;resize:vertical;line-height:1.6" placeholder="e.g. 编写代码时遵循以下规范...">' + esc(sp.prompt || '') + '</textarea></div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px">'
    + '<button class="btn btn-sm" onclick="document.getElementById(\'sp-edit-modal\').remove()" style="padding:6px 16px">Cancel</button>'
    + '<button class="btn btn-sm btn-primary" onclick="_spSaveEditor(' + idx + ')" style="padding:6px 16px">Save</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  modal.addEventListener('click', function(e) { if (e.target === modal) modal.remove(); });
}

function _spScopeChanged() {
  var panel = document.getElementById('sp-roles-panel');
  if (!panel) return;
  var radios = document.querySelectorAll('input[name="sp-scope"]');
  var scope = 'all';
  radios.forEach(function(r) { if (r.checked) scope = r.value; });
  panel.style.display = scope === 'roles' ? 'block' : 'none';
}

function _spSaveEditor(idx) {
  var nameEl = document.getElementById('sp-edit-name');
  var promptEl = document.getElementById('sp-edit-prompt');
  if (!nameEl || !promptEl) return;
  var name = nameEl.value.trim();
  var promptText = promptEl.value.trim();
  if (!name) { alert('Name is required'); return; }
  if (!promptText) { alert('Prompt content is required'); return; }

  // Read scope
  var radios = document.querySelectorAll('input[name="sp-scope"]');
  var scope = 'all';
  radios.forEach(function(r) { if (r.checked) scope = r.value; });

  // Read selected roles
  var roles = [];
  if (scope === 'roles') {
    document.querySelectorAll('.sp-role-cb').forEach(function(cb) {
      if (cb.checked) roles.push(cb.value);
    });
  }

  if (idx >= 0) {
    _scenePrompts[idx].name = name;
    _scenePrompts[idx].prompt = promptText;
    _scenePrompts[idx].scope = scope;
    _scenePrompts[idx].roles = roles;
  } else {
    _scenePrompts.push({
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      name: name,
      prompt: promptText,
      enabled: true,
      scope: scope,
      roles: roles
    });
  }

  document.getElementById('sp-edit-modal').remove();
  _spRender(_scenePrompts);
  _spSave();
}

function _spToggle(idx, checked) {
  if (_scenePrompts[idx]) {
    _scenePrompts[idx].enabled = checked;
    _spRender(_scenePrompts);
    _spSave();
  }
}

function _spDelete(idx) {
  var sp = _scenePrompts[idx];
  if (!sp) return;
  var el = document.getElementById('scene-prompts-list');
  if (!el) return;
  var cards = el.children;
  if (!cards[idx]) return;
  var card = cards[idx];
  var orig = card.innerHTML;
  card.innerHTML = '<div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0">'
    + '<span style="color:var(--error);font-size:13px">Delete "' + esc(sp.name || 'Unnamed') + '"?</span>'
    + '<div style="display:flex;gap:6px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style]\').parentElement.innerHTML=window._spDeleteOrig;window._spDeleteOrig=null" style="padding:4px 12px;font-size:12px">Cancel</button>'
    + '<button class="btn btn-sm" style="padding:4px 12px;font-size:12px;background:var(--error);color:#fff" onclick="_spConfirmDelete(' + idx + ')">Delete</button>'
    + '</div></div>';
  window._spDeleteOrig = orig;
}

function _spConfirmDelete(idx) {
  _scenePrompts.splice(idx, 1);
  _spRender(_scenePrompts);
  _spSave();
}

async function _spSave() {
  try {
    await api('POST', '/api/portal/config', { scene_prompts: _scenePrompts, global_system_prompt: '' });
  } catch(e) {
    alert('Save failed: ' + e.message);
  }
}

// ============ Config ============
function renderConfig(container) {
  const c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  const epoch = _renderEpoch;
  api('GET', '/api/portal/config').then(cfg => {
    if (!cfg || epoch !== _renderEpoch) return;
    // Store presets for editing
    window._rolePresets = cfg.role_presets || {};
    _availableRoles = Object.keys(cfg.role_presets || {}).sort();

    // Auto-migrate legacy global_system_prompt into scene_prompts
    var _mergedPrompts = cfg.scene_prompts || [];
    if (cfg.global_system_prompt && cfg.global_system_prompt.trim()) {
      var hasLegacy = _mergedPrompts.some(function(p) { return p._is_global; });
      if (!hasLegacy) {
        _mergedPrompts.unshift({
          id: 'global_default',
          name: 'Global Rules · 全局规则',
          prompt: cfg.global_system_prompt.trim(),
          enabled: true,
          _is_global: true
        });
      }
    }

    c.innerHTML = `
      <!-- System Prompts (unified) -->
      <div class="card" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div>
            <div class="card-title" style="margin:0">
              <span class="material-symbols-outlined" style="font-size:18px;vertical-align:middle;margin-right:4px;color:var(--primary)">tune</span>
              System Prompts · 系统提示词
            </div>
            <div style="font-size:12px;color:var(--text3);margin-top:4px">
              为所有 Agent 定义系统提示词。可按场景分条管理（如「全局规则」「复杂工程任务」「编码规范」等），启用的条目会注入到每个 Agent 的系统提示中。
            </div>
          </div>
          <button class="btn btn-sm btn-primary" onclick="_spAdd()" style="padding:6px 14px">
            <span class="material-symbols-outlined" style="font-size:15px;vertical-align:middle;margin-right:2px">add</span> Add Prompt
          </button>
        </div>
        <div id="scene-prompts-list"></div>
      </div>

      <!-- Row 2: Role Presets (editable) -->
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div class="card-title" style="margin:0">
              <span class="material-symbols-outlined" style="font-size:18px;vertical-align:middle;margin-right:4px;color:var(--primary)">person</span>
              Role Presets · 角色预设
            </div>
            <div style="font-size:12px;color:var(--text3);margin-top:4px">
              创建智能体时可选角色预设。编辑 system_prompt 和 profile 属性，点击保存后立即生效。
            </div>
          </div>
          <button class="btn btn-sm btn-primary" onclick="_rpShowCreate()" style="padding:6px 14px">
            <span class="material-symbols-outlined" style="font-size:15px;vertical-align:middle;margin-right:2px">add</span> 新建角色
          </button>
        </div>
        <div id="role-presets-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px"></div>
      </div>
    `;
    // Populate role presets grid after innerHTML is set
    _rpRenderCards(cfg.role_presets || {});
    _spRender(_mergedPrompts);
  });
}

function _rpRenderCards(presets) {
  var grid = document.getElementById('role-presets-list');
  if (!grid) return;
  var html = '';
  Object.keys(presets).forEach(function(k) {
    var v = presets[k];
    var p = v.profile || {};
    var expertiseStr = (p.expertise||[]).slice(0,4).join(', ');
    var skillsStr = (p.skills||[]).slice(0,4).join(', ');
    html += '<div style="background:var(--surface);border-radius:10px;padding:14px;border:1px solid var(--border-light);transition:all 0.15s" onmouseenter="this.style.borderColor=\'var(--primary)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\'">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">'
        + '<div style="font-weight:700;font-size:14px;color:var(--primary)">' + esc(k) + '</div>'
        + '<div style="display:flex;gap:4px">'
          + '<button class="btn btn-sm" onclick="_rpShowEdit(\'' + esc(k) + '\')" style="padding:3px 8px;font-size:11px" title="编辑"><span class="material-symbols-outlined" style="font-size:14px">edit</span></button>'
          + '<button class="btn btn-sm" onclick="_rpDelete(\'' + esc(k) + '\')" style="padding:3px 8px;font-size:11px;color:var(--error)" title="删除"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button>'
        + '</div>'
      + '</div>'
      + '<div style="font-size:12px;color:var(--text2);margin-bottom:6px;line-height:1.5;max-height:48px;overflow:hidden">' + esc((v.system_prompt||'').slice(0,120)) + (v.system_prompt && v.system_prompt.length > 120 ? '...' : '') + '</div>'
      + (p.personality ? '<div style="font-size:11px;color:var(--text3)"><b>性格:</b> ' + esc(p.personality) + '</div>' : '')
      + (expertiseStr ? '<div style="font-size:11px;color:var(--text3)"><b>专长:</b> ' + esc(expertiseStr) + '</div>' : '')
      + (skillsStr ? '<div style="font-size:11px;color:var(--text3)"><b>技能:</b> ' + esc(skillsStr) + '</div>' : '')
    + '</div>';
  });
  grid.innerHTML = html;
}

// ── Knowledge Wiki Management ──
var _kbAllEntries = [];   // full list from server
var _kbActiveTag = '';     // currently selected tag filter

function _kbRenderCard(e) {
  var tags = (e.tags||[]).map(function(t){ return '<span style="background:var(--surface3);padding:1px 6px;border-radius:4px;font-size:11px;color:var(--text2)">'+esc(t)+'</span>'; }).join(' ');
  var preview = esc((e.content||'').slice(0, 120)) + (e.content && e.content.length > 120 ? '...' : '');
  return '<div class="kb-card" style="border:1px solid var(--border-light);border-radius:8px;padding:12px">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">' +
      '<div style="font-weight:600;font-size:14px;color:var(--text)">' + esc(e.title) + '</div>' +
      '<div style="display:flex;gap:4px">' +
        '<button class="btn btn-sm" onclick="_kbShowEdit(\''+e.id+'\')" style="padding:3px 8px;font-size:11px"><span class="material-symbols-outlined" style="font-size:13px">edit</span></button>' +
        '<button class="btn btn-sm" onclick="_kbDelete(\''+e.id+'\',\''+esc(e.title).replace(/'/g,"\\'")+'\')" style="padding:3px 8px;font-size:11px;color:var(--error)"><span class="material-symbols-outlined" style="font-size:13px">delete</span></button>' +
      '</div>' +
    '</div>' +
    (tags ? '<div style="margin-bottom:6px">'+tags+'</div>' : '') +
    '<div style="color:var(--text3);font-size:12px;line-height:1.4;white-space:pre-wrap">'+preview+'</div>' +
  '</div>';
}

function _kbRenderTagBar(entries) {
  var bar = document.getElementById('kb-tag-bar');
  if (!bar) return;
  var tagSet = {};
  entries.forEach(function(e) { (e.tags||[]).forEach(function(t){ tagSet[t] = (tagSet[t]||0) + 1; }); });
  var allTags = Object.keys(tagSet).sort();
  if (allTags.length === 0) { bar.innerHTML = ''; return; }
  var html = '<button class="btn btn-sm" style="padding:2px 10px;font-size:11px;border-radius:12px;' +
    (!_kbActiveTag ? 'background:var(--accent);color:#fff' : '') +
    '" onclick="_kbSetTag(&quot;&quot;)">All</button>';
  allTags.forEach(function(t) {
    var active = (_kbActiveTag === t);
    var safe = esc(t).replace(/"/g, '&quot;');
    html += ' <button class="btn btn-sm" style="padding:2px 10px;font-size:11px;border-radius:12px;' +
      (active ? 'background:var(--accent);color:#fff' : '') +
      '" onclick="_kbSetTag(&quot;'+safe+'&quot;)">' + esc(t) + ' <span style="opacity:0.6">(' + tagSet[t] + ')</span></button>';
  });
  bar.innerHTML = html;
}

function _kbSetTag(tag) {
  _kbActiveTag = tag;
  _kbFilter();
}

function _kbFilter() {
  var searchEl = document.getElementById('kb-search');
  var q = (searchEl ? searchEl.value : '').toLowerCase().trim();
  var filtered = _kbAllEntries.filter(function(e) {
    // tag filter
    if (_kbActiveTag && (e.tags||[]).indexOf(_kbActiveTag) === -1) return false;
    // text search
    if (!q) return true;
    var title = (e.title||'').toLowerCase();
    var content = (e.content||'').toLowerCase();
    var tags = (e.tags||[]).join(' ').toLowerCase();
    return title.indexOf(q) !== -1 || content.indexOf(q) !== -1 || tags.indexOf(q) !== -1;
  });
  var el = document.getElementById('kb-entries-list');
  if (!el) return;
  if (filtered.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);text-align:center;padding:20px 0;grid-column:1/-1">' +
      (q || _kbActiveTag ? 'No matching entries.' : 'No entries yet. Click "Add Entry" to create your first knowledge entry.') + '</div>';
  } else {
    el.innerHTML = filtered.map(_kbRenderCard).join('');
  }
  _kbRenderTagBar(_kbAllEntries);
}

async function _kbLoadEntries() {
  var el = document.getElementById('kb-entries-list');
  if (!el) return;
  try {
    var data = await api('GET', '/api/portal/knowledge');
    _kbAllEntries = (data && data.entries) || [];
    _kbFilter();
  } catch(e) {
    el.innerHTML = '<div style="color:var(--error)">Failed to load: '+esc(e.message)+'</div>';
  }
}

var _kbCache = {};  // id → entry for edit

function _kbShowAdd() {
  document.getElementById('kb-edit-id').value = '';
  document.getElementById('kb-edit-title').value = '';
  document.getElementById('kb-edit-tags').value = '';
  document.getElementById('kb-edit-content').value = '';
  document.getElementById('kb-modal-title').textContent = 'Add Knowledge Entry';
  document.getElementById('kb-modal').style.display = 'flex';
}

async function _kbShowEdit(id) {
  try {
    var data = await api('GET', '/api/portal/knowledge');
    var entry = ((data && data.entries) || []).find(function(e){ return e.id === id; });
    if (!entry) { alert('Entry not found'); return; }
    document.getElementById('kb-edit-id').value = entry.id;
    document.getElementById('kb-edit-title').value = entry.title || '';
    document.getElementById('kb-edit-tags').value = (entry.tags || []).join(', ');
    document.getElementById('kb-edit-content').value = entry.content || '';
    document.getElementById('kb-modal-title').textContent = 'Edit: ' + entry.title;
    document.getElementById('kb-modal').style.display = 'flex';
  } catch(e) { alert('Error: ' + e.message); }
}

function _kbCloseModal() {
  document.getElementById('kb-modal').style.display = 'none';
}

async function _kbSave() {
  var id = document.getElementById('kb-edit-id').value;
  var title = document.getElementById('kb-edit-title').value.trim();
  var content = document.getElementById('kb-edit-content').value.trim();
  var tagsRaw = document.getElementById('kb-edit-tags').value.trim();
  var tags = tagsRaw ? tagsRaw.split(',').map(function(t){ return t.trim(); }).filter(Boolean) : [];
  if (!title) { alert('Title is required'); return; }
  if (!content) { alert('Content is required'); return; }
  try {
    if (id) {
      await api('POST', '/api/portal/knowledge/' + id, { title: title, content: content, tags: tags });
    } else {
      await api('POST', '/api/portal/knowledge', { title: title, content: content, tags: tags });
    }
    _kbCloseModal();
    _kbLoadEntries();
  } catch(e) { alert('Save failed: ' + e.message); }
}

async function _kbDelete(id, title) {
  if (!confirm('Delete "' + title + '"?')) return;
  try {
    await api('POST', '/api/portal/knowledge/' + id + '/delete');
    _kbLoadEntries();
  } catch(e) { alert('Delete failed: ' + e.message); }
}

function _cfgProviderChanged() {
  var provEl = document.getElementById('cfg-provider');
  var modelEl = document.getElementById('cfg-model');
  if (!provEl || !modelEl) return;
  var pid = provEl.value;
  var models = _availableModels[pid] || [];
  modelEl.innerHTML = models.length
    ? models.map(m => '<option value="'+esc(m)+'">'+esc(m)+'</option>').join('')
    : '<option value="">-- no models detected --</option>';
  saveConfig();
}

async function _cfgDetectModels() {
  var provEl = document.getElementById('cfg-provider');
  if (!provEl) return;
  var pid = provEl.value;
  var btn = event.currentTarget;
  btn.disabled = true;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:16px;animation:spin 1s linear infinite">refresh</span> Detecting...';
  try {
    var data = await api('POST', '/api/portal/providers/'+pid+'/detect');
    if (data && data.models) {
      _availableModels[pid] = data.models;
      var modelEl = document.getElementById('cfg-model');
      if (modelEl) {
        modelEl.innerHTML = data.models.map(m =>
          '<option value="'+esc(m)+'">'+esc(m)+'</option>'
        ).join('');
      }
    }
  } catch(e) {
    alert('Detection failed: ' + e.message);
  }
  btn.disabled = false;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:16px">refresh</span> Detect';
}

async function saveConfig() {
  var provEl = document.getElementById('cfg-provider');
  var modelEl = document.getElementById('cfg-model');
  if (!provEl || !modelEl) return;
  const body = {
    provider: provEl.value,
    model: modelEl.value,
  };
  await api('POST', '/api/portal/config', body);
  loadAvailableModels();
}

// _cfgSaveGlobalPrompt removed — merged into unified System Prompts (scene_prompts)

// ============ Role Presets CRUD ============
function _rpShowEdit(roleKey) {
  var preset = (window._rolePresets||{})[roleKey];
  if (!preset) { alert('Preset not found'); return; }
  var p = preset.profile || {};
  var html = '<div class="modal-overlay" id="rp-edit-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:600px">'
    + '<h3>编辑角色预设: ' + esc(roleKey) + '</h3>'
    + '<div class="form-group"><label>角色名 Role Key</label><input id="rp-edit-key" value="'+esc(roleKey)+'" readonly style="opacity:0.6"></div>'
    + '<div class="form-group"><label>显示名 Name</label><input id="rp-edit-name" value="'+esc(preset.name||'')+'"></div>'
    + '<div class="form-group"><label>System Prompt *</label><textarea id="rp-edit-prompt" rows="5" style="font-family:monospace;font-size:12px">'+esc(preset.system_prompt||'')+'</textarea></div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
      + '<div class="form-group"><label>性格 Personality</label><input id="rp-edit-personality" value="'+esc(p.personality||'')+'"></div>'
      + '<div class="form-group"><label>沟通风格 Communication</label><input id="rp-edit-comm" value="'+esc(p.communication_style||'')+'"></div>'
    + '</div>'
    + '<div class="form-group"><label>专长 Expertise (逗号分隔)</label><input id="rp-edit-expertise" value="'+esc((p.expertise||[]).join(', '))+'"></div>'
    + '<div class="form-group"><label>技能 Skills (逗号分隔)</label><input id="rp-edit-skills" value="'+esc((p.skills||[]).join(', '))+'"></div>'
    + '<div class="form-group"><label>允许的工具 Allowed Tools (逗号分隔，留空=全部)</label><input id="rp-edit-allowed" value="'+esc((p.allowed_tools||[]).join(', '))+'"></div>'
    + '<div class="form-group"><label>禁用的工具 Denied Tools (逗号分隔)</label><input id="rp-edit-denied" value="'+esc((p.denied_tools||[]).join(', '))+'"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'rp-edit-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" onclick="_rpSaveEdit(\''+esc(roleKey)+'\')">保存</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

function _rpShowCreate() {
  var html = '<div class="modal-overlay" id="rp-create-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:600px">'
    + '<h3>新建角色预设</h3>'
    + '<div class="form-group"><label>角色名 Role Key * (英文，如 analyst)</label><input id="rp-new-key" placeholder="e.g. analyst, translator, tester"></div>'
    + '<div class="form-group"><label>显示名 Name</label><input id="rp-new-name" placeholder="e.g. 数据分析师"></div>'
    + '<div class="form-group"><label>System Prompt *</label><textarea id="rp-new-prompt" rows="5" style="font-family:monospace;font-size:12px" placeholder="You are a data analyst..."></textarea></div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
      + '<div class="form-group"><label>性格 Personality</label><input id="rp-new-personality" placeholder="e.g. analytical, precise"></div>'
      + '<div class="form-group"><label>沟通风格 Communication</label><input id="rp-new-comm" placeholder="e.g. technical, brief"></div>'
    + '</div>'
    + '<div class="form-group"><label>专长 Expertise (逗号分隔)</label><input id="rp-new-expertise" placeholder="data_analysis, statistics"></div>'
    + '<div class="form-group"><label>技能 Skills (逗号分隔)</label><input id="rp-new-skills" placeholder="data_analysis, visualization"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'rp-create-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" onclick="_rpSaveNew()">创建</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

function _rpParseList(val) {
  return (val||'').split(',').map(function(s){return s.trim()}).filter(Boolean);
}

async function _rpSaveEdit(roleKey) {
  var name = (document.getElementById('rp-edit-name')||{}).value||'';
  var prompt = (document.getElementById('rp-edit-prompt')||{}).value||'';
  if (!prompt.trim()) { alert('System Prompt 不能为空'); return; }
  var profile = {
    personality: (document.getElementById('rp-edit-personality')||{}).value||'',
    communication_style: (document.getElementById('rp-edit-comm')||{}).value||'',
    expertise: _rpParseList((document.getElementById('rp-edit-expertise')||{}).value),
    skills: _rpParseList((document.getElementById('rp-edit-skills')||{}).value),
    allowed_tools: _rpParseList((document.getElementById('rp-edit-allowed')||{}).value),
    denied_tools: _rpParseList((document.getElementById('rp-edit-denied')||{}).value),
  };
  try {
    await api('POST', '/api/portal/role-presets/update', {
      key: roleKey, name: name, system_prompt: prompt, profile: profile
    });
    var m = document.getElementById('rp-edit-modal'); if(m) m.remove();
    renderConfig(document.getElementById('settings-content'));
  } catch(e) { alert('保存失败: '+e); }
}

async function _rpSaveNew() {
  var key = (document.getElementById('rp-new-key')||{}).value||'';
  var name = (document.getElementById('rp-new-name')||{}).value||'';
  var prompt = (document.getElementById('rp-new-prompt')||{}).value||'';
  if (!key.trim()) { alert('角色名不能为空'); return; }
  if (!prompt.trim()) { alert('System Prompt 不能为空'); return; }
  if (!/^[a-z][a-z0-9_]*$/.test(key)) { alert('角色名只能使用小写字母、数字和下划线'); return; }
  var profile = {
    personality: (document.getElementById('rp-new-personality')||{}).value||'',
    communication_style: (document.getElementById('rp-new-comm')||{}).value||'',
    expertise: _rpParseList((document.getElementById('rp-new-expertise')||{}).value),
    skills: _rpParseList((document.getElementById('rp-new-skills')||{}).value),
  };
  try {
    await api('POST', '/api/portal/role-presets/update', {
      key: key, name: name, system_prompt: prompt, profile: profile
    });
    var m = document.getElementById('rp-create-modal'); if(m) m.remove();
    renderConfig(document.getElementById('settings-content'));
  } catch(e) { alert('创建失败: '+e); }
}

async function _rpDelete(roleKey) {
  if (!confirm('确定删除角色预设 "'+roleKey+'" 吗？')) return;
  try {
    await api('POST', '/api/portal/role-presets/delete', { key: roleKey });
    renderConfig(document.getElementById('settings-content'));
  } catch(e) { alert('删除失败: '+e); }
}

// ============ Node Configuration ============
function renderNodeConfig(container) {
  const c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  const epoch = _renderEpoch;
  c.innerHTML = '<div style="color:var(--text3);padding:20px">Loading node configurations...</div>';
  api('GET', '/api/portal/node-configs').then(data => {
    if (!data || epoch !== _renderEpoch) return;
    const configs = data.configs || [];
    const isHub = portalMode === 'hub';
    c.innerHTML = `
      <div style="margin-bottom:20px">
        <h3 style="font-family:'Plus Jakarta Sans',sans-serif;font-size:20px;font-weight:700;margin-bottom:4px">Node Configuration</h3>
        <p style="color:var(--text2);font-size:13px">Per-node secrets, tokens, and environment variables. ${isHub ? 'Admin can manage all nodes.' : 'You can only edit local node config.'}</p>
      </div>
      <div id="node-config-list"></div>
    `;
    const listEl = document.getElementById('node-config-list');
    if (configs.length === 0) {
      listEl.innerHTML = '<div style="color:var(--text3);padding:16px">No nodes found.</div>';
      return;
    }
    listEl.innerHTML = configs.map(nc => {
      const nid = nc.node_id;
      const nname = nc.node_name || nid;
      const items = Object.values(nc.items || {});
      const canEdit = isHub || nid === 'local';
      const unsyncedCount = items.filter(i => !i.synced).length;
      return `
        <div class="card" style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div>
              <span style="font-weight:700;font-size:15px">${esc(nname)}</span>
              <span style="font-size:11px;color:var(--text3);margin-left:8px">${esc(nid)}</span>
              ${nid === 'local' ? '<span style="font-size:10px;background:var(--primary);color:white;padding:1px 6px;border-radius:4px;margin-left:6px">LOCAL</span>' : '<span style="font-size:10px;background:var(--warning);color:#333;padding:1px 6px;border-radius:4px;margin-left:6px">REMOTE</span>'}
            </div>
            <div style="display:flex;gap:8px">
              ${isHub && nid !== 'local' ? `<button class="btn btn-sm" style="font-size:11px" onclick="syncNodeConfig('${esc(nid)}')" title="Push config to node">
                <span class="material-symbols-outlined" style="font-size:14px">sync</span> Sync ${unsyncedCount > 0 ? '('+unsyncedCount+' pending)' : ''}
              </button>` : ''}
              ${canEdit ? `<button class="btn btn-primary btn-sm" style="font-size:11px" onclick="showAddNodeConfigItem('${esc(nid)}')">
                <span class="material-symbols-outlined" style="font-size:14px">add</span> Add
              </button>` : ''}
            </div>
          </div>
          <div id="ncfg-items-${esc(nid)}">
            ${items.length === 0 ? '<div style="color:var(--text3);font-size:13px;padding:8px 0">No config items yet.</div>' :
              `<table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="border-bottom:1px solid var(--border-light);color:var(--text3);font-size:11px;text-transform:uppercase;letter-spacing:0.5px">
                  <th style="text-align:left;padding:6px 8px">Key</th>
                  <th style="text-align:left;padding:6px 8px">Value</th>
                  <th style="text-align:left;padding:6px 8px">Category</th>
                  <th style="text-align:left;padding:6px 8px">Synced</th>
                  ${canEdit ? '<th style="text-align:right;padding:6px 8px">Actions</th>' : ''}
                </tr>
                ${items.map(item => `
                  <tr style="border-bottom:1px solid var(--border-light)">
                    <td style="padding:8px;font-family:monospace;font-weight:600">${esc(item.key)}</td>
                    <td style="padding:8px;font-family:monospace;color:var(--text2)">${item.is_secret ? '<span style="color:var(--text3)">••••••••</span>' : esc(item.value)}</td>
                    <td style="padding:8px"><span style="font-size:11px;background:var(--surface3);padding:2px 8px;border-radius:4px">${esc(item.category)}</span></td>
                    <td style="padding:8px">${item.synced ? '<span style="color:var(--success);font-size:12px">&#10003; synced</span>' : '<span style="color:var(--warning);font-size:12px">pending</span>'}</td>
                    ${canEdit ? `<td style="padding:8px;text-align:right">
                      <button style="background:none;border:none;color:var(--primary);cursor:pointer;font-size:11px;font-weight:700" onclick="editNodeConfigItem('${esc(nid)}','${esc(item.key)}')">Edit</button>
                      <button style="background:none;border:none;color:var(--error);cursor:pointer;font-size:11px;font-weight:700;margin-left:8px" onclick="deleteNodeConfigItem('${esc(nid)}','${esc(item.key)}')">Delete</button>
                    </td>` : ''}
                  </tr>
                `).join('')}
              </table>`
            }
          </div>
        </div>
      `;
    }).join('');
  });
}

function showAddNodeConfigItem(nodeId) {
  const html = `
    <div style="padding:16px">
      <h3 style="margin-bottom:16px">Add Config Item — ${esc(nodeId)}</h3>
      <div class="form-group"><label>Key</label><input id="ncfg-key" placeholder="e.g. market_api_token"></div>
      <div class="form-group"><label>Value</label><input id="ncfg-value" placeholder="Enter value"></div>
      <div class="form-group"><label>Description</label><input id="ncfg-desc" placeholder="What is this for?"></div>
      <div class="form-group"><label>Category</label>
        <select id="ncfg-category">
          <option value="general">General</option>
          <option value="credentials">Credentials</option>
          <option value="integration">Integration</option>
          <option value="custom">Custom</option>
        </select>
      </div>
      <div class="form-group" style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" id="ncfg-secret"> <label for="ncfg-secret" style="margin:0">Secret (masked in UI)</label>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
        <button class="btn btn-sm" onclick="closeModal()">Cancel</button>
        <button class="btn btn-primary btn-sm" onclick="saveNodeConfigItem('${esc(nodeId)}')">Save</button>
      </div>
    </div>
  `;
  showModalHTML(html);
}

function editNodeConfigItem(nodeId, key) {
  // Fetch current value
  api('GET', '/api/portal/node/' + nodeId + '/config?mask=0').then(data => {
    const item = (data.items || {})[key];
    if (!item) return;
    const html = `
      <div style="padding:16px">
        <h3 style="margin-bottom:16px">Edit Config — ${esc(key)}</h3>
        <div class="form-group"><label>Key</label><input id="ncfg-key" value="${esc(key)}" readonly style="opacity:0.6"></div>
        <div class="form-group"><label>Value</label><input id="ncfg-value" value="${esc(item.value)}"></div>
        <div class="form-group"><label>Description</label><input id="ncfg-desc" value="${esc(item.description)}"></div>
        <div class="form-group"><label>Category</label>
          <select id="ncfg-category">
            <option value="general" ${item.category==='general'?'selected':''}>General</option>
            <option value="credentials" ${item.category==='credentials'?'selected':''}>Credentials</option>
            <option value="integration" ${item.category==='integration'?'selected':''}>Integration</option>
            <option value="custom" ${item.category==='custom'?'selected':''}>Custom</option>
          </select>
        </div>
        <div class="form-group" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="ncfg-secret" ${item.is_secret?'checked':''}> <label for="ncfg-secret" style="margin:0">Secret</label>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-sm" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary btn-sm" onclick="saveNodeConfigItem('${esc(nodeId)}')">Save</button>
        </div>
      </div>
    `;
    showModalHTML(html);
  });
}

async function saveNodeConfigItem(nodeId) {
  const key = document.getElementById('ncfg-key').value.trim();
  const value = document.getElementById('ncfg-value').value;
  const desc = document.getElementById('ncfg-desc').value;
  const cat = document.getElementById('ncfg-category').value;
  const secret = document.getElementById('ncfg-secret').checked;
  if (!key) return alert('Key is required');
  await api('POST', '/api/portal/node/' + nodeId + '/config', {
    key, value, description: desc, category: cat, is_secret: secret
  });
  closeModal();
  renderNodeConfig();
}

async function deleteNodeConfigItem(nodeId, key) {
  if (!confirm('Delete config "' + key + '" from node ' + nodeId + '?')) return;
  await api('POST', '/api/portal/node/' + nodeId + '/config', {
    action: 'delete', key: key
  });
  renderNodeConfig();
}

async function syncNodeConfig(nodeId) {
  const result = await api('POST', '/api/portal/node/' + nodeId + '/config/sync', {});
  if (result && result.ok) {
    alert('Synced ' + (result.synced||0) + ' config items to node ' + nodeId);
  } else {
    alert('Sync failed: ' + (result && result.error || 'Unknown error'));
  }
  renderNodeConfig();
}

function showModalHTML(html) {
  // Remove any existing overlays first to prevent duplicate stacking
  document.querySelectorAll('#modal-overlay').forEach(function(o){ o.remove(); });
  var overlay = document.createElement('div');
  overlay.id = 'modal-overlay';
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:1000';
  document.body.appendChild(overlay);
  overlay.innerHTML = '<div style="background:var(--surface);border-radius:16px;min-width:400px;max-width:550px;max-height:85vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.3);border:1px solid var(--border-light)">' + html + '</div>';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
}

// ============ Template Library ============
function renderTemplateLibrary(container) {
  const c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  const epoch = _renderEpoch;
  c.innerHTML = '<div style="color:var(--text3);padding:20px">Loading templates...</div>';
  api('GET', '/api/portal/templates').then(data => {
    if (!data || epoch !== _renderEpoch) return;
    const templates = data.templates || [];

    // Group by category
    const cats = {};
    templates.forEach(t => {
      const cat = t.category || 'General';
      if (!cats[cat]) cats[cat] = [];
      cats[cat].push(t);
    });

    let html = '<div style="margin-bottom:20px">' +
      '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:20px;font-weight:700;margin-bottom:4px">专业领域</h3>' +
      '<p style="color:var(--text2);font-size:13px">Agent 执行任务时自动匹配对应专业领域，注入领域方法论和检查清单。共 ' + templates.length + ' 个领域。</p>' +
      '<button class="btn btn-primary btn-sm" style="margin-top:10px" onclick="showCreateTemplate()">' +
        '<span class="material-symbols-outlined" style="font-size:14px">add</span> 新建专业领域</button>' +
    '</div>';

    Object.keys(cats).sort().forEach(cat => {
      html += '<div style="margin-bottom:24px">';
      html += '<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--text3);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border-light)">' + esc(cat) + ' (' + cats[cat].length + ')</div>';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">';
      cats[cat].forEach(t => {
        html += '<div class="card" style="padding:16px;cursor:pointer;transition:all 0.2s;border:1px solid var(--border-light)" ' +
          'onmouseenter="this.style.borderColor=\'var(--primary)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\'" ' +
          'onclick="viewTemplate(\'' + esc(t.id) + '\')">' +
          '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">' +
            '<span style="font-weight:700;font-size:14px;line-height:1.3">' + esc(t.name) + '</span>' +
            '<span style="font-size:18px;flex-shrink:0;margin-left:8px">' + getCategoryIcon(cat) + '</span>' +
          '</div>' +
          '<div style="font-size:12px;color:var(--text2);line-height:1.4;margin-bottom:8px">' + esc(t.description||'').slice(0,100) + '</div>' +
          '<div style="display:flex;flex-wrap:wrap;gap:4px">' +
            (t.tags||[]).slice(0,4).map(tag => '<span style="font-size:10px;background:var(--surface3);padding:2px 6px;border-radius:4px;color:var(--text3)">' + esc(tag) + '</span>').join('') +
          '</div>' +
          '<div style="font-size:10px;color:var(--text3);margin-top:8px">' +
            'Roles: ' + (t.roles||[]).slice(0,3).map(r => esc(r)).join(', ') +
          '</div>' +
        '</div>';
      });
      html += '</div></div>';
    });
    c.innerHTML = html;
  });
}

function getCategoryIcon(cat) {
  const map = {
    'Product Management': '📋', 'Business': '📊', 'Development': '💻',
    'Quality Assurance': '✅', 'Security': '🛡️', 'Operations': '🚀',
    'Documentation': '📝', 'Data': '📈', 'Design': '🎨',
    'Project Management': '📅', 'General': '📦',
  };
  for (const [k,v] of Object.entries(map)) {
    if (cat.toLowerCase().includes(k.toLowerCase().split(' ')[0])) return v;
  }
  return '📄';
}

async function viewTemplate(templateId) {
  const data = await api('GET', '/api/portal/templates/' + templateId);
  if (!data) return;
  const t = data;
  // Render content as HTML (basic markdown conversion)
  const contentHtml = (t.content||'')
    .replace(/^### (.*$)/gm, '<h4 style="color:var(--primary);margin:12px 0 6px;font-size:14px">$1</h4>')
    .replace(/^## (.*$)/gm, '<h3 style="color:var(--text);margin:16px 0 8px;font-size:16px;font-weight:700">$1</h3>')
    .replace(/^# (.*$)/gm, '<h2 style="color:var(--text);margin:20px 0 10px;font-size:18px;font-weight:800">$1</h2>')
    .replace(/^- \[x\] (.*$)/gm, '<div style="display:flex;align-items:flex-start;gap:6px;margin:3px 0"><span style="color:var(--success);font-size:14px">☑</span><span style="font-size:13px;color:var(--text2)">$1</span></div>')
    .replace(/^- \[ \] (.*$)/gm, '<div style="display:flex;align-items:flex-start;gap:6px;margin:3px 0"><span style="color:var(--text3);font-size:14px">☐</span><span style="font-size:13px;color:var(--text2)">$1</span></div>')
    .replace(/^- (.*$)/gm, '<div style="margin:2px 0 2px 12px;font-size:13px;color:var(--text2)">• $1</div>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.*?)`/g, '<code style="background:var(--surface3);padding:1px 4px;border-radius:3px;font-size:12px">$1</code>')
    .replace(/\n\n/g, '<br><br>')
    .replace(/\n/g, '<br>');

  const html = '<div style="padding:20px;max-height:80vh;overflow-y:auto;max-width:700px">' +
    '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">' +
      '<div>' +
        '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;margin-bottom:4px">' + esc(t.name) + '</h3>' +
        '<p style="font-size:12px;color:var(--text2)">' + esc(t.description||'') + '</p>' +
      '</div>' +
      '<div style="display:flex;gap:6px">' +
        '<button class="btn btn-sm" style="font-size:11px" onclick="editTemplateContent(\'' + esc(t.id) + '\')"><span class="material-symbols-outlined" style="font-size:14px">edit</span> Edit</button>' +
        '<button class="btn btn-sm" style="font-size:11px;color:var(--error)" onclick="deleteTemplate(\'' + esc(t.id) + '\')"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button>' +
      '</div>' +
    '</div>' +
    '<div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap">' +
      (t.roles||[]).map(r => '<span style="font-size:10px;background:rgba(203,201,255,0.15);color:var(--primary);padding:2px 8px;border-radius:4px;font-weight:600">' + esc(r) + '</span>').join('') +
      (t.tags||[]).map(tg => '<span style="font-size:10px;background:var(--surface3);padding:2px 6px;border-radius:4px;color:var(--text3)">' + esc(tg) + '</span>').join('') +
    '</div>' +
    '<div style="border:1px solid var(--border-light);border-radius:8px;padding:16px;background:var(--bg);font-size:13px;line-height:1.6;max-height:50vh;overflow-y:auto">' + contentHtml + '</div>' +
    '<div style="text-align:right;margin-top:12px"><button class="btn btn-sm" onclick="closeModal()">Close</button></div>' +
  '</div>';
  showModalHTML(html);
}

function showCreateTemplate() {
  const html = '<div style="padding:16px">' +
    '<h3 style="margin-bottom:16px">Create Template</h3>' +
    '<div class="form-group"><label>Name</label><input id="tpl-name" placeholder="e.g. 性能优化技能"></div>' +
    '<div class="form-group"><label>Description</label><input id="tpl-desc" placeholder="Brief description"></div>' +
    '<div class="form-group"><label>Category</label><input id="tpl-cat" value="General" placeholder="e.g. Development, Security"></div>' +
    '<div class="form-group"><label>Roles (comma separated)</label><input id="tpl-roles" placeholder="e.g. Developer, Architect"></div>' +
    '<div class="form-group"><label>Tags (comma separated)</label><input id="tpl-tags" placeholder="e.g. performance, optimization"></div>' +
    '<div class="form-group"><label>Content (Markdown)</label>' +
      '<textarea id="tpl-content" rows="12" style="width:100%;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:10px;font-family:monospace;font-size:12px;line-height:1.5" placeholder="# Template Title\n\ntags: tag1, tag2\n\n## Section 1\n\n- [ ] Checklist item..."></textarea>' +
    '</div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">' +
      '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary btn-sm" onclick="saveNewTemplate()">Create</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function saveNewTemplate() {
  const name = document.getElementById('tpl-name').value.trim();
  const content = document.getElementById('tpl-content').value;
  if (!name || !content) { alert('Name and content required'); return; }
  await api('POST', '/api/portal/templates', {
    action: 'create',
    name: name,
    description: document.getElementById('tpl-desc').value,
    category: document.getElementById('tpl-cat').value || 'General',
    roles: document.getElementById('tpl-roles').value.split(',').map(s=>s.trim()).filter(Boolean),
    tags: document.getElementById('tpl-tags').value.split(',').map(s=>s.trim()).filter(Boolean),
    content: content,
  });
  closeModal();
  renderTemplateLibrary();
}

function editTemplateContent(templateId) {
  api('GET', '/api/portal/templates/' + templateId).then(t => {
    if (!t) return;
    const html = '<div style="padding:16px">' +
      '<h3 style="margin-bottom:16px">Edit: ' + esc(t.name) + '</h3>' +
      '<div class="form-group"><label>Name</label><input id="tpl-edit-name" value="' + esc(t.name) + '"></div>' +
      '<div class="form-group"><label>Description</label><input id="tpl-edit-desc" value="' + esc(t.description||'') + '"></div>' +
      '<div class="form-group"><label>Content (Markdown)</label>' +
        '<textarea id="tpl-edit-content" rows="15" style="width:100%;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:10px;font-family:monospace;font-size:12px;line-height:1.5">' + esc(t.content||'') + '</textarea>' +
      '</div>' +
      '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">' +
        '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
        '<button class="btn btn-primary btn-sm" onclick="saveEditTemplate(\'' + esc(templateId) + '\')">Save</button>' +
      '</div></div>';
    showModalHTML(html);
  });
}

async function saveEditTemplate(templateId) {
  await api('POST', '/api/portal/templates', {
    action: 'update',
    template_id: templateId,
    name: document.getElementById('tpl-edit-name').value,
    description: document.getElementById('tpl-edit-desc').value,
    content: document.getElementById('tpl-edit-content').value,
  });
  closeModal();
  renderTemplateLibrary();
}

async function deleteTemplate(templateId) {
  if (!confirm('确定要删除这个技能吗?')) return;
  await api('POST', '/api/portal/templates', { action: 'delete', template_id: templateId });
  closeModal();
  renderTemplateLibrary();
}

// ============ Scheduler Panel ============
async function renderScheduler() {
  const content = document.getElementById('content');
  content.style.padding = '24px';
  // Inline header with title + action button (top-right) — matches other tabs
  const schedHeader = ''
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">'
    + '  <div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:22px;font-weight:800;margin:0">定时任务</h2>'
    + '    <p style="font-size:12px;color:var(--text3);margin-top:4px">Scheduled Jobs · 按 cron 或间隔自动触发 agent</p></div>'
    + '  <button class="btn btn-primary btn-sm" onclick="showCreateJob()"><span class="material-symbols-outlined" style="font-size:14px">add</span> New Job</button>'
    + '</div>';
  content.innerHTML = schedHeader + '<div style="color:var(--text3);padding:20px">Loading scheduled tasks...</div>';
  let jobsData, presetsData, agentsData;
  try {
    [jobsData, presetsData, agentsData] = await Promise.all([
      api('GET', '/api/portal/scheduler/jobs'),
      api('GET', '/api/portal/scheduler/presets'),
      api('GET', '/api/portal/agents'),
    ]);
  } catch(e) {
    content.innerHTML = schedHeader + '<div style="color:var(--error);padding:20px">Failed to load scheduler: '+(e && e.message || e)+'</div>';
    return;
  }
  const jobs = (jobsData && jobsData.jobs) || [];
  const presets = (presetsData && presetsData.presets) || {};
  const agents = (agentsData && agentsData.agents) || [];

  let html = schedHeader + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px">';

  if (jobs.length === 0) {
    html += '<div class="card" style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted)">';
    html += '<span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:8px">schedule</span>';
    html += '<p>No scheduled jobs yet</p>';
    html += '<p style="font-size:12px">Create one from presets or manually</p>';
    html += '<div style="margin-top:16px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">';
    Object.entries(presets).forEach(([k,v]) => {
      html += '<button class="btn btn-sm" onclick="createFromPreset(\''+k+'\')">'+v.name+'</button>';
    });
    html += '</div></div>';
  }

  jobs.forEach(job => {
    const agentName = (agents.find(a=>a.id===job.agent_id)||{}).name||job.agent_id;
    const statusIcon = job.enabled ? (job.last_status==='success'?'check_circle':job.last_status==='failed'?'error':'pending') : 'pause_circle';
    const statusColor = job.enabled ? (job.last_status==='success'?'var(--success)':job.last_status==='failed'?'var(--danger)':'var(--muted)') : 'var(--muted)';
    const nextRun = job.next_run_at ? new Date(job.next_run_at*1000).toLocaleString() : '-';
    const lastRun = job.last_run_at ? new Date(job.last_run_at*1000).toLocaleString() : 'Never';

    html += '<div class="card" style="position:relative">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">';
    html += '<span class="material-symbols-outlined" style="color:'+statusColor+'">'+statusIcon+'</span>';
    html += '<strong style="flex:1">'+job.name+'</strong>';
    html += '<span class="badge badge-primary" style="font-size:11px">'+job.job_type+'</span>';
    html += '</div>';
    html += '<div style="font-size:12px;color:var(--muted);margin-bottom:8px">'+(job.description||'No description')+'</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px">';
    html += '<div><strong>Agent:</strong> '+agentName+'</div>';
    html += '<div><strong>Cron:</strong> <code>'+(job.cron_expr||'one-time')+'</code></div>';
    html += '<div><strong>Next:</strong> '+nextRun+'</div>';
    html += '<div><strong>Last:</strong> '+lastRun+'</div>';
    html += '<div><strong>Runs:</strong> '+job.run_count+(job.max_runs?' / '+job.max_runs:'')+'</div>';
    html += '<div><strong>Timeout:</strong> '+job.timeout+'s</div>';
    html += '</div>';
    if (job.tags && job.tags.length) {
      html += '<div style="margin-top:6px">';
      job.tags.forEach(t => html += '<span class="badge" style="font-size:10px;margin-right:4px">'+t+'</span>');
      html += '</div>';
    }
    html += '<div style="display:flex;gap:6px;margin-top:12px;border-top:1px solid var(--border);padding-top:10px">';
    html += '<button class="btn btn-sm btn-primary" onclick="triggerJob(\''+job.id+'\')"><span class="material-symbols-outlined" style="font-size:14px">play_arrow</span> Run Now</button>';
    html += '<button class="btn btn-sm" onclick="toggleJob(\''+job.id+'\','+(!job.enabled)+')">'+(job.enabled?'Pause':'Resume')+'</button>';
    html += '<button class="btn btn-sm" onclick="viewJobHistory(\''+job.id+'\')">History</button>';
    html += '<button class="btn btn-sm" style="margin-left:auto;color:var(--danger)" onclick="deleteJob(\''+job.id+'\')">Delete</button>';
    html += '</div></div>';
  });
  html += '</div>';
  content.innerHTML = html;
}

async function showCreateJob() {
  const [agentsData, presetsData, workflowsData] = await Promise.all([
    api('GET', '/api/portal/agents'),
    api('GET', '/api/portal/scheduler/presets'),
    api('GET', '/api/portal/workflows').catch(function(){ return {workflows:[]}; }),
  ]);
  const agents = agentsData.agents || [];
  const presets = presetsData.presets || {};
  // /api/portal/workflows returns mixed templates + instances. Templates
  // have a "steps" array but no "instance_id" / no terminal status; we
  // include anything with an id + name and let the user pick.
  const allWfs = (workflowsData && workflowsData.workflows) || [];
  const templates = allWfs.filter(function(w){
    // Heuristic: skip running/completed instances; keep entries with no
    // instance-only fields ("status" == "draft" or absent)
    var st = (w.status || '').toLowerCase();
    return !st || st === 'draft' || st === 'template';
  });

  let presetOpts = '<option value="">-- Custom --</option>';
  Object.entries(presets).forEach(([k,v]) => {
    presetOpts += '<option value="'+k+'">'+v.name+'</option>';
  });
  let agentOpts = agents.map(a => '<option value="'+a.id+'">'+a.name+' ('+a.role+')</option>').join('');
  let wfOpts = '<option value="">-- select workflow template --</option>' +
    templates.map(function(w){
      return '<option value="'+esc(w.id||w.template_id||'')+'">'+esc(w.name||'(unnamed)')+'</option>';
    }).join('');

  const html = '<div style="padding:20px;max-width:600px">' +
    '<h3 style="margin-bottom:16px">Create Scheduled Job</h3>' +
    '<label>Preset</label><select id="job-preset" onchange="applyJobPreset(this.value)" class="input" style="margin-bottom:8px">'+presetOpts+'</select>' +
    '<label>Target Type</label><select id="job-target-type" class="input" style="margin-bottom:8px" onchange="_jobTargetTypeChanged()">' +
      '<option value="chat">Agent Chat — call agent.chat with prompt</option>' +
      '<option value="workflow">Workflow — run a workflow template</option>' +
    '</select>' +
    '<div id="job-workflow-row" style="display:none;margin-bottom:8px">' +
      '<label>Workflow Template</label>' +
      '<select id="job-workflow-id" class="input" onchange="_jobWorkflowChanged()">'+wfOpts+'</select>' +
      '<div style="font-size:11px;color:var(--muted);margin-top:4px">' +
        'If step assignments are not provided below, every step will run as the selected default agent.' +
      '</div>' +
      '<div id="job-step-assignments" style="margin-top:8px"></div>' +
    '</div>' +
    '<label id="job-agent-label">Agent</label><select id="job-agent" class="input" style="margin-bottom:8px"><option value="">-- none --</option>'+agentOpts+'</select>' +
    '<label>Name</label><input id="job-name" class="input" style="margin-bottom:8px" placeholder="Daily AIGC Digest">' +
    '<label>Type</label><select id="job-type" class="input" style="margin-bottom:8px"><option value="recurring">Recurring (Cron)</option><option value="one_time">One Time</option></select>' +
    '<label>Cron Expression</label><input id="job-cron" class="input" style="margin-bottom:8px" placeholder="0 9 * * *">' +
    '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">minute hour day month weekday (e.g. 0 9 * * 1-5 = weekdays 9am)</div>' +
    '<label id="job-prompt-label">Prompt Template</label><textarea id="job-prompt" class="input" rows="4" style="margin-bottom:8px" placeholder="Use {date}, {time}, {weekday} variables..."></textarea>' +
    '<label>Notify Channels (comma-separated IDs)</label><input id="job-channels" class="input" style="margin-bottom:8px">' +
    '<label>Tags (comma-separated)</label><input id="job-tags" class="input" style="margin-bottom:16px">' +
    '<div style="display:flex;gap:8px"><button class="btn btn-primary" onclick="saveNewJob()">Create</button>' +
    '<button class="btn" onclick="closeModal()">Cancel</button></div></div>';
  showModalHTML(html);
  // Store templates + agent options for step assignment rendering
  window._jobWfTemplates = templates;
  window._jobAgentOpts = agentOpts;
}

function _jobTargetTypeChanged() {
  var t = document.getElementById('job-target-type').value;
  var row = document.getElementById('job-workflow-row');
  var label = document.getElementById('job-agent-label');
  var promptLabel = document.getElementById('job-prompt-label');
  var promptEl = document.getElementById('job-prompt');
  if (t === 'workflow') {
    if (row) row.style.display = 'block';
    if (label) label.textContent = 'Default Agent (used for unassigned workflow steps)';
    if (promptLabel) promptLabel.textContent = 'Workflow Input (passed as input_data)';
    if (promptEl) promptEl.placeholder = 'Initial input passed to the workflow. Optional — leave empty to use job name.';
  } else {
    if (row) row.style.display = 'none';
    if (label) label.textContent = 'Agent';
    if (promptLabel) promptLabel.textContent = 'Prompt Template';
    if (promptEl) promptEl.placeholder = 'Use {date}, {time}, {weekday} variables...';
  }
}

function _jobWorkflowChanged() {
  var container = document.getElementById('job-step-assignments');
  if (!container) return;
  var wfId = document.getElementById('job-workflow-id').value;
  if (!wfId) { container.innerHTML = ''; return; }
  var templates = window._jobWfTemplates || [];
  var tmpl = templates.find(function(t){ return (t.id||t.template_id) === wfId; });
  if (!tmpl || !tmpl.steps || !tmpl.steps.length) {
    container.innerHTML = '<div style="font-size:11px;color:var(--muted)">该模板没有定义步骤</div>';
    return;
  }
  var agentOpts = window._jobAgentOpts || '';
  var stepRows = tmpl.steps.map(function(s, i) {
    return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
      '<span style="font-size:11px;color:var(--text);min-width:140px;font-weight:600">Step '+(i+1)+': '+esc(s.name||s.title||'')+'</span>' +
      '<select class="job-step-agent input" data-step="'+i+'" style="flex:1;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 8px;color:var(--text);font-size:12px">' +
        '<option value="">— 使用默认 Agent —</option>' + agentOpts +
      '</select>' +
      '<label title="启动前需人工批准" style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text3);cursor:pointer;white-space:nowrap">' +
        '<input type="checkbox" class="job-step-approval" data-step="'+i+'" style="cursor:pointer">审核' +
      '</label>' +
    '</div>';
  }).join('');
  container.innerHTML =
    '<div style="font-size:12px;color:var(--text3);margin-bottom:6px;font-weight:600">步骤 → Agent 分配（未分配的步骤将使用默认 Agent）</div>' +
    stepRows;
}

function applyJobPreset(presetId) {
  if (!presetId) return;
  api('GET', '/api/portal/scheduler/presets').then(data => {
    const p = (data.presets||{})[presetId];
    if (!p) return;
    document.getElementById('job-name').value = p.name || '';
    document.getElementById('job-cron').value = p.cron_expr || '';
    document.getElementById('job-prompt').value = p.prompt_template || '';
    document.getElementById('job-type').value = p.job_type || 'recurring';
    document.getElementById('job-tags').value = (p.tags||[]).join(', ');
  });
}

async function saveNewJob() {
  const preset = document.getElementById('job-preset').value;
  const targetTypeEl = document.getElementById('job-target-type');
  const targetType = targetTypeEl ? targetTypeEl.value : 'chat';
  const body = {
    action: 'create',
    agent_id: document.getElementById('job-agent').value,
    name: document.getElementById('job-name').value,
    job_type: document.getElementById('job-type').value,
    cron_expr: document.getElementById('job-cron').value,
    prompt_template: document.getElementById('job-prompt').value,
    notify_channels: document.getElementById('job-channels').value.split(',').map(s=>s.trim()).filter(Boolean),
    tags: document.getElementById('job-tags').value.split(',').map(s=>s.trim()).filter(Boolean),
    target_type: targetType,
  };
  if (targetType === 'workflow') {
    var wfEl = document.getElementById('job-workflow-id');
    body.workflow_id = wfEl ? wfEl.value : '';
    if (!body.workflow_id) {
      alert('Please select a workflow template.');
      return;
    }
    // The prompt textarea is repurposed as workflow input in workflow mode.
    body.workflow_input = body.prompt_template;
    body.prompt_template = '';
    // Collect per-step agent assignments
    var stepAssignments = [];
    var approvalMap = {};
    document.querySelectorAll('.job-step-approval').forEach(function(chk){
      approvalMap[parseInt(chk.getAttribute('data-step'))] = chk.checked;
    });
    document.querySelectorAll('.job-step-agent').forEach(function(sel){
      var idx = parseInt(sel.getAttribute('data-step'));
      var aid = sel.value;
      if (aid) stepAssignments.push({
        step_index: idx, agent_id: aid,
        require_approval: !!approvalMap[idx],
      });
    });
    if (stepAssignments.length > 0) {
      body.workflow_step_assignments = stepAssignments;
    }
  }
  if (preset) body.preset_id = preset;
  await api('POST', '/api/portal/scheduler/jobs', body);
  closeModal();
  _refreshSchedulerView();
}

async function createFromPreset(presetId) {
  await showCreateJob();
  document.getElementById('job-preset').value = presetId;
  applyJobPreset(presetId);
}

async function triggerJob(jobId) {
  try {
    var res = await api('POST', '/api/portal/scheduler/jobs', {action:'trigger', job_id:jobId});
    if (res.ok) {
      alert('✅ Job triggered — agent正在后台执行...');
      setTimeout(function(){ _refreshSchedulerView(); }, 3000);
      setTimeout(function(){ _refreshSchedulerView(); }, 8000);
      setTimeout(function(){ _refreshSchedulerView(); }, 15000);
    } else {
      alert('❌ Trigger failed: job not found');
    }
  } catch(e) {
    alert('❌ Trigger error: '+e.message);
  }
  _refreshSchedulerView();
}

// Refresh whichever view is currently showing scheduled jobs.
// Task Center now hosts the canonical view; renderScheduler() is kept
// only for any direct deep-link callers (it falls back to Task Center).
function _refreshSchedulerView() {
  if (document.getElementById('tc-content')) {
    loadTaskCenter();
  } else if (typeof renderScheduler === 'function') {
    try { renderScheduler(); } catch(e) {}
  }
}

async function toggleJob(jobId, enabled) {
  await api('POST', '/api/portal/scheduler/jobs', {action:'toggle', job_id:jobId, enabled});
  _refreshSchedulerView();
}

async function deleteJob(jobId) {
  if (!confirm('Delete this scheduled job?')) return;
  await api('POST', '/api/portal/scheduler/jobs', {action:'delete', job_id:jobId});
  renderScheduler();
}

async function viewJobHistory(jobId) {
  const data = await api('GET', '/api/portal/scheduler/jobs/'+jobId+'/history');
  const records = data.history || [];
  let html = '<div style="padding:20px;max-width:700px"><h3>Execution History</h3>';
  if (records.length === 0) {
    html += '<p style="color:var(--muted)">No executions yet</p>';
  } else {
    html += '<table class="table" style="font-size:12px"><thead><tr><th>Time</th><th>Status</th><th>Duration</th><th>Result</th></tr></thead><tbody>';
    records.forEach(r => {
      const started = new Date(r.started_at*1000).toLocaleString();
      const dur = r.completed_at ? ((r.completed_at - r.started_at).toFixed(1)+'s') : 'running';
      const statusBadge = r.status==='success'?'badge-primary':r.status==='failed'?'badge badge-danger':'badge';
      html += '<tr><td>'+started+'</td><td><span class="badge '+statusBadge+'">'+r.status+'</span></td><td>'+dur+'</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">'+(r.result||r.error||'-').substring(0,200)+'</td></tr>';
    });
    html += '</tbody></table>';
  }
  html += '</div>';
  showModalHTML(html);
}

// ============ MCP Config Panel ============
async function renderMCPConfig(container) {
  const content = container || document.getElementById('content');
  if (!container) content.style.padding = '24px';
  const epoch = _renderEpoch;
  content.innerHTML = '<div style="color:var(--text3);padding:20px">Loading MCP configuration...</div>';
  let nodesData, catalogData, agentsData;
  try {
    [nodesData, catalogData, agentsData] = await Promise.all([
      api('GET', '/api/portal/mcp/nodes'),
      api('GET', '/api/portal/mcp/catalog'),
      api('GET', '/api/portal/agents'),
    ]);
  } catch(e) {
    if (epoch !== _renderEpoch) return;
    content.innerHTML = '<div style="color:var(--error);padding:20px">Failed to load MCP config: '+(e && e.message || e)+'</div>';
    return;
  }
  if (epoch !== _renderEpoch) return;
  const nodes = (nodesData && nodesData.nodes) || {};
  const catalog = (catalogData && catalogData.catalog) || {};
  const agents = (agentsData && agentsData.agents) || [];

  let html = '';

  // Page header with Add button
  html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:28px">';
  html += '<div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px">MCP Configuration</h2>';
  html += '<p style="color:var(--text2);font-size:14px;margin-top:4px">Manage Model Context Protocol server bindings and catalog.</p></div>';
  html += '<button class="btn btn-primary" onclick="showAddMCP()" style="flex-shrink:0"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;margin-right:4px">add</span>Add MCP</button>';
  html += '</div>';

  // MCP Catalog — 分 Global / Node 两组
  var globalCaps = Object.entries(catalog).filter(([_,c]) => c.scope === 'global');
  var nodeCaps = Object.entries(catalog).filter(([_,c]) => c.scope !== 'global');

  function _renderCapCards(caps) {
    var h = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px">';
    caps.forEach(([id, cap]) => {
      var typeIcon = {filesystem:'folder',database:'storage',api:'api',search:'search',communication:'chat',custom:'extension'}[cap.server_type]||'extension';
      var scopeBadge = cap.scope==='global'
        ? '<span style="font-size:9px;background:#dbeafe;color:#1d4ed8;padding:1px 5px;border-radius:4px;margin-left:6px">Global</span>'
        : '<span style="font-size:9px;background:#fef3c7;color:#92400e;padding:1px 5px;border-radius:4px;margin-left:6px">Node</span>';
      h += '<div style="border:1px solid var(--border-light);border-radius:12px;padding:14px;cursor:pointer;background:var(--surface);transition:all 0.2s" onmouseover="this.style.borderColor=\'var(--border)\';this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.2)\'" onmouseout="this.style.borderColor=\'var(--border-light)\';this.style.boxShadow=\'none\'" onclick="showCatalogDetail(\''+id+'\')">';
      h += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">';
      h += '<div style="width:32px;height:32px;border-radius:8px;background:var(--bg);display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">'+typeIcon+'</span></div>';
      h += '<strong style="font-size:13px">'+cap.name+'</strong>'+scopeBadge+'</div>';
      h += '<div style="font-size:11px;color:var(--text3);line-height:1.4;margin-bottom:8px">'+cap.description.substring(0,80)+'</div>';
      h += '<div style="font-size:10px;display:flex;flex-wrap:wrap;gap:4px">';
      (cap.tools_provided||[]).slice(0,3).forEach(t => h += '<span class="badge" style="font-size:9px;padding:2px 6px;border-radius:5px">'+t+'</span>');
      if ((cap.tools_provided||[]).length > 3) h += '<span style="color:var(--text3);font-size:9px">+'+((cap.tools_provided||[]).length-3)+'</span>';
      h += '</div></div>';
    });
    h += '</div>';
    return h;
  }

  html += '<div class="card" style="margin-bottom:16px">';
  html += '<h3 style="margin-bottom:12px"><span class="material-symbols-outlined" style="vertical-align:middle">inventory_2</span> MCP Catalog</h3>';
  // scope tabs
  html += '<div style="display:flex;gap:0;margin-bottom:14px;border-bottom:1px solid var(--border-light)">';
  html += '<button class="btn btn-sm" id="mcp-tab-global" onclick="_switchMCPCatalogTab(\'global\')" style="border-radius:8px 8px 0 0;border-bottom:2px solid var(--primary);font-weight:700;color:var(--primary)"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">public</span> Global ('+globalCaps.length+')</button>';
  html += '<button class="btn btn-sm" id="mcp-tab-node" onclick="_switchMCPCatalogTab(\'node\')" style="border-radius:8px 8px 0 0;border-bottom:2px solid transparent;color:var(--text3)"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">dns</span> Node ('+nodeCaps.length+')</button>';
  html += '</div>';
  html += '<div style="font-size:11px;color:var(--text3);margin-bottom:10px" id="mcp-catalog-hint">Global MCP: 调用外部 API / 云服务，配置一次可同步到所有 Node</div>';
  html += '<div id="mcp-catalog-global">' + _renderCapCards(globalCaps) + '</div>';
  html += '<div id="mcp-catalog-node" style="display:none">' + _renderCapCards(nodeCaps) + '</div>';
  html += '</div>';

  // Per-Node MCP configs
  Object.entries(nodes).forEach(([nodeId, nodeCfg]) => {
    const mcps = nodeCfg.available_mcps || {};
    const bindings = nodeCfg.agent_bindings || {};

    html += '<div class="card" style="margin-bottom:16px">';
    html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">';
    html += '<h3 style="margin:0"><span class="material-symbols-outlined" style="vertical-align:middle">dns</span> Node: '+nodeId+'</h3>';
    html += '<button class="btn btn-sm" onclick="syncGlobalToNode(\''+nodeId+'\')"><span class="material-symbols-outlined" style="font-size:14px">sync</span> 同步 Global MCP</button>';
    html += '</div>';

    if (Object.keys(mcps).length === 0) {
      html += '<p style="color:var(--muted);font-size:13px">No MCP servers configured on this node</p>';
    } else {
      html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:12px">';
      Object.entries(mcps).forEach(([mcpId, mcp]) => {
        const boundAgents = Object.entries(bindings).filter(([aid,mids])=>mids.includes(mcpId)).map(([aid])=>{
          const a = agents.find(x=>x.id===aid);
          return a ? a.name : aid;
        });
        const typeIcon = {filesystem:'folder',database:'storage',api:'api',search:'search',communication:'chat',custom:'extension'}[mcp.server_type]||'extension';
        const tools = mcp.tools_provided || [];
        html += '<div style="border:1px solid var(--border-light);border-radius:14px;padding:20px;background:var(--surface);transition:all 0.2s">';
        // Header
        html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">';
        html += '<div style="display:flex;align-items:center;gap:10px;min-width:0;flex:1">';
        html += '<div style="width:40px;height:40px;border-radius:10px;background:var(--bg);display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid var(--border-light)"><span class="material-symbols-outlined" style="font-size:20px;color:var(--primary)">'+typeIcon+'</span></div>';
        html += '<div style="min-width:0;flex:1">';
        html += '<div style="font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(mcp.name)+'</div>';
        html += '<div style="font-size:11px;color:var(--text3);font-family:monospace;margin-top:2px">'+esc(mcpId)+'</div>';
        html += '</div></div>';
        html += '<span style="font-size:12px;font-weight:600;color:'+(mcp.enabled?'var(--success)':'var(--text3)')+'"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:'+(mcp.enabled?'var(--success)':'var(--text3)')+';margin-right:5px;vertical-align:middle"></span>'+(mcp.enabled?'Active':'Disabled')+'</span>';
        html += '</div>';
        // ── Install status badge ──
        var ist = mcp.install_status || 'unknown';
        if (ist === 'installing') {
          html += '<div style="background:#fff3cd;color:#856404;font-size:11px;padding:4px 10px;border-radius:6px;margin-bottom:10px;display:flex;align-items:center;gap:6px"><span class="material-symbols-outlined" style="font-size:14px;animation:spin 1s linear infinite">progress_activity</span> 正在安装...</div>';
        } else if (ist === 'failed') {
          html += '<div style="background:#f8d7da;color:#721c24;font-size:11px;padding:4px 10px;border-radius:6px;margin-bottom:10px">';
          html += '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">error</span> 安装失败';
          if (mcp.install_error) html += ': ' + esc(String(mcp.install_error).substring(0,80));
          html += ' <a href="#" onclick="retryMCPInstall(\''+nodeId+'\',\''+mcpId+'\');return false" style="color:#721c24;font-weight:700;text-decoration:underline;margin-left:6px">重试</a>';
          html += '</div>';
        } else if (ist === 'installed') {
          html += '<div style="background:#d4edda;color:#155724;font-size:11px;padding:4px 10px;border-radius:6px;margin-bottom:10px"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">check_circle</span> 已安装</div>';
        }
        // Description
        if (mcp.description) {
          html += '<div style="font-size:12px;color:var(--text3);margin-bottom:10px;line-height:1.5">'+esc(String(mcp.description).substring(0,100))+'</div>';
        }
        // Meta row
        html += '<div style="display:flex;gap:6px;font-size:10px;margin-bottom:10px;flex-wrap:wrap">';
        if (mcp.scope === 'global') {
          html += '<span style="font-size:9px;background:#dbeafe;color:#1d4ed8;padding:2px 6px;border-radius:5px;font-weight:600">Global</span>';
        } else {
          html += '<span style="font-size:9px;background:#fef3c7;color:#92400e;padding:2px 6px;border-radius:5px;font-weight:600">Node</span>';
        }
        html += '<span class="badge" style="font-size:10px;padding:3px 8px;border-radius:6px">'+esc(mcp.transport)+'</span>';
        tools.slice(0,3).forEach(t => html += '<span class="badge" style="font-size:10px;padding:3px 8px;border-radius:6px">'+esc(t)+'</span>');
        if (tools.length > 3) html += '<span style="color:var(--text3);font-size:10px">+'+(tools.length-3)+'</span>';
        html += '</div>';
        // Config details (command / URL / env)
        html += '<div style="background:var(--bg);border-radius:8px;padding:10px 12px;margin-bottom:10px;font-size:11px;font-family:monospace;line-height:1.6">';
        if (mcp.command) {
          html += '<div style="margin-bottom:4px"><span style="color:var(--text3);font-family:sans-serif;font-size:10px">Command:</span><br><span style="color:var(--text2);word-break:break-all">'+esc(mcp.command)+'</span></div>';
        }
        if (mcp.url) {
          html += '<div style="margin-bottom:4px"><span style="color:var(--text3);font-family:sans-serif;font-size:10px">URL:</span><br><span style="color:var(--text2);word-break:break-all">'+esc(mcp.url)+'</span></div>';
        }
        var envOverridesForCard = (nodeCfg.env_overrides || {})[mcpId] || {};
        var mergedEnv = Object.assign({}, mcp.env || {}, envOverridesForCard);
        var envKeys = Object.keys(mergedEnv);
        if (envKeys.length) {
          html += '<div><span style="color:var(--text3);font-family:sans-serif;font-size:10px">Env (' + envKeys.length + '):</span><br>';
          envKeys.forEach(function(k) {
            var v = mergedEnv[k];
            var masked = v ? (v.length > 6 ? v.substring(0,3) + '***' + v.substring(v.length-2) : '***') : '<empty>';
            html += '<span style="color:var(--primary)">'+esc(k)+'</span>=<span style="color:var(--text3)">'+esc(masked)+'</span><br>';
          });
          html += '</div>';
        } else {
          html += '<div style="color:var(--text3);font-family:sans-serif;font-size:10px">⚠️ No env vars configured</div>';
        }
        html += '</div>';
        // Bound agents
        html += '<div style="font-size:12px;margin-bottom:14px"><span style="color:var(--text3)">Bound:</span> ';
        html += boundAgents.length?('<span style="font-weight:600;color:var(--text)">'+boundAgents.map(esc).join(', ')+'</span>'):'<span style="color:var(--text3)">None</span>';
        html += '</div>';
        // Separator + Actions
        html += '<div style="border-top:1px solid var(--border-light);padding-top:12px;display:flex;gap:8px">';
        html += '<button class="btn btn-sm" style="flex:1" onclick="showBindMCP(\''+nodeId+'\',\''+mcpId+'\')"><span class="material-symbols-outlined" style="font-size:14px">link</span> Bind</button>';
        html += '<button class="btn btn-sm" style="flex:1" onclick="showEditMCPEnv(\''+nodeId+'\',\''+mcpId+'\')"><span class="material-symbols-outlined" style="font-size:14px">settings</span> Env</button>';
        html += '<button class="btn btn-sm" title="Change scope" onclick="showChangeMCPScope(\''+mcpId+'\',\''+(mcp.scope||'node')+'\',\''+nodeId+'\')"><span class="material-symbols-outlined" style="font-size:14px">swap_horiz</span> Scope</button>';
        html += '<button class="btn btn-sm btn-danger" onclick="removeMCPFromNode(\''+nodeId+'\',\''+mcpId+'\')"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button>';
        html += '</div>';
        html += '</div>';
      });
      html += '</div>';
    }
    html += '</div>';
  });

  // If no nodes at all, show empty state
  if (Object.keys(nodes).length === 0) {
    html += '<div class="card" style="text-align:center;padding:40px;color:var(--muted)">';
    html += '<span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:8px">hub</span>';
    html += '<p>No MCP configurations yet</p><p style="font-size:12px">Add an MCP server from the catalog above</p></div>';
  }

  content.innerHTML = html;
}

async function showAddMCP(preSelectCapId) {
  const catalogData = await api('GET', '/api/portal/mcp/catalog');
  const catalog = catalogData.catalog || {};

  let opts = '<option value="">-- Select from Catalog --</option>';
  opts += Object.entries(catalog).map(([id,cap]) => '<option value="'+id+'"'+(id===preSelectCapId?' selected':'')+'>'+cap.name+' ('+cap.server_type+')</option>').join('');

  const html = '<div style="padding:20px;max-width:550px">' +
    '<h3 style="margin-bottom:16px">Add MCP Server</h3>' +
    '<label>Select from Catalog</label><select id="mcp-catalog" class="input" style="margin-bottom:8px" onchange="onMCPCatalogSelect(this.value)">'+opts+'</select>' +
    '<div id="mcp-env-fields" style="margin-bottom:8px"></div>' +
    '<label>Or Custom: Name</label><input id="mcp-name" class="input" style="margin-bottom:8px">' +
    '<label>Transport</label><select id="mcp-transport" class="input" style="margin-bottom:8px"><option value="stdio">stdio</option><option value="sse">sse</option><option value="streamable-http">streamable-http</option></select>' +
    '<label>Command (stdio)</label><input id="mcp-command" class="input" style="margin-bottom:8px">' +
    '<label>URL (sse/http)</label><input id="mcp-url" class="input" style="margin-bottom:12px">' +
    // ── Scope selector (Invariant D: Node / Global / multi-Node) ──
    // Global MCPs are eagerly copied into every node at save time, so
    // every agent on every node can see them. Node-scope restricts the
    // MCP to one node. multi_node restricts it to an explicit list.
    '<label>Scope</label>' +
    '<select id="mcp-scope" class="input" style="margin-bottom:8px" onchange="onMCPScopeChange()">' +
      '<option value="node">Node — only this node (' + esc(window._currentNodeId || 'local') + ')</option>' +
      '<option value="global">Global — visible to every node</option>' +
      '<option value="multi_node">Multi-Node — pick specific nodes</option>' +
    '</select>' +
    '<div id="mcp-scope-nodes" style="display:none;margin-bottom:8px">' +
      '<label style="font-size:11px;color:var(--text3)">Target nodes (comma-separated)</label>' +
      '<input id="mcp-scope-nodes-input" class="input" placeholder="nodeA,nodeB" style="font-size:12px">' +
    '</div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:12px;line-height:1.5">' +
      'Scope 决定 MCP 的可见范围。<b>Global</b> 会在每个 Node 的列表里都出现；' +
      '<b>Node</b> 只绑定到当前 Node；<b>Multi-Node</b> 指定一组 Node 白名单。' +
    '</div>' +
    '<div id="mcp-test-result" style="margin-bottom:12px;font-size:12px"></div>' +
    '<div style="display:flex;gap:8px"><button class="btn btn-primary" onclick="saveMCP()">Add</button>' +
    '<button class="btn" onclick="testMCP()">Test</button>' +
    '<button class="btn" onclick="closeModal()">Cancel</button></div></div>';
  showModalHTML(html);
  // If pre-selected, auto-populate the form
  if (preSelectCapId && catalog[preSelectCapId]) {
    onMCPCatalogSelect(preSelectCapId);
  }
}

function onMCPScopeChange() {
  var sel = document.getElementById('mcp-scope');
  var nodesBox = document.getElementById('mcp-scope-nodes');
  if (!sel || !nodesBox) return;
  nodesBox.style.display = (sel.value === 'multi_node') ? 'block' : 'none';
}

// ── Rescope existing MCP (Invariant D: admin-driven scope change) ──
// Calls POST /api/portal/mcp/manage { action: 'change_scope', ... }. The
// backend handles eager propagation (global → all nodes) and whitelist
// enforcement (multi_node → target_nodes list) in manager.change_mcp_scope.
function showChangeMCPScope(mcpId, currentScope, currentNodeId) {
  var cs = currentScope || 'node';
  var html = '<div style="padding:20px;max-width:520px">' +
    '<h3 style="margin-bottom:6px">Change MCP Scope</h3>' +
    '<div style="font-size:12px;color:var(--text3);margin-bottom:14px">' +
      'MCP: <code style="background:var(--bg);padding:2px 6px;border-radius:4px">'+esc(mcpId)+'</code>' +
      ' &nbsp; Current: <b>'+esc(cs)+'</b>' +
    '</div>' +
    '<label>New Scope</label>' +
    '<select id="rescope-scope" class="input" style="margin-bottom:10px" onchange="onRescopeChange()">' +
      '<option value="node"'+(cs==='node'?' selected':'')+'>Node — exactly one node</option>' +
      '<option value="global"'+(cs==='global'?' selected':'')+'>Global — every node (eager copy)</option>' +
      '<option value="multi_node"'+(cs==='multi_node'?' selected':'')+'>Multi-Node — whitelist</option>' +
    '</select>' +
    '<div id="rescope-node-box" style="display:none;margin-bottom:10px">' +
      '<label style="font-size:11px;color:var(--text3)">Target node id</label>' +
      '<input id="rescope-node-input" class="input" value="'+esc(currentNodeId||'')+'" style="font-size:12px">' +
    '</div>' +
    '<div id="rescope-multi-box" style="display:none;margin-bottom:10px">' +
      '<label style="font-size:11px;color:var(--text3)">Target nodes (comma-separated)</label>' +
      '<input id="rescope-multi-input" class="input" placeholder="nodeA,nodeB" style="font-size:12px">' +
    '</div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:14px;line-height:1.5">' +
      '切换到 <b>Global</b> 会把这个 MCP 物理复制到所有 Node；切换到 <b>Node</b> 只保留指定一个 Node；<b>Multi-Node</b> 按白名单保留。' +
    '</div>' +
    '<div style="display:flex;gap:8px">' +
      '<button class="btn btn-primary" onclick="submitChangeMCPScope(\''+mcpId+'\')">Apply</button>' +
      '<button class="btn" onclick="closeModal()">Cancel</button>' +
    '</div></div>';
  showModalHTML(html);
  onRescopeChange();
}

function onRescopeChange() {
  var sel = document.getElementById('rescope-scope');
  if (!sel) return;
  var nb = document.getElementById('rescope-node-box');
  var mb = document.getElementById('rescope-multi-box');
  if (nb) nb.style.display = (sel.value === 'node') ? 'block' : 'none';
  if (mb) mb.style.display = (sel.value === 'multi_node') ? 'block' : 'none';
}

async function submitChangeMCPScope(mcpId) {
  var scope = document.getElementById('rescope-scope').value;
  var payload = { action: 'change_scope', mcp_id: mcpId, scope: scope };
  if (scope === 'node') {
    var one = (document.getElementById('rescope-node-input').value || '').trim();
    if (!one) { alert('Node scope 需要指定 target node id'); return; }
    payload.target_nodes = [one];
  } else if (scope === 'multi_node') {
    var raw = (document.getElementById('rescope-multi-input').value || '').trim();
    var list = raw.split(',').map(function(s){return s.trim();}).filter(Boolean);
    if (!list.length) { alert('Multi-Node scope 需要至少一个 target node id'); return; }
    payload.target_nodes = list;
  }
  try {
    var r = await api('POST', '/api/portal/mcp/manage', payload);
    if (!r || r.ok === false) {
      alert('Change scope failed: ' + ((r && r.error) || 'unknown'));
      return;
    }
    closeModal();
    renderMCPConfig();
  } catch (e) {
    alert('Change scope failed: ' + (e && e.message || e));
  }
}

function onMCPCatalogSelect(capId) {
  api('GET', '/api/portal/mcp/catalog').then(data => {
    const cap = (data.catalog||{})[capId];
    if (!cap) return;
    document.getElementById('mcp-name').value = cap.name;
    document.getElementById('mcp-transport').value = cap.transport;
    document.getElementById('mcp-command').value = cap.command_template || '';
    document.getElementById('mcp-url').value = cap.url_template || '';
    // Catalog entries carry a preferred scope (e.g. jimeng_video is
    // scope="global"). Pre-select it so the user doesn't have to guess,
    // but leave the selector enabled so admins can override.
    var scopeSel = document.getElementById('mcp-scope');
    if (scopeSel && cap.scope) {
      scopeSel.value = cap.scope;
      onMCPScopeChange();
    }
    let envHtml = '';
    if (cap.notes) {
      envHtml += '<div style="background:var(--surface3);border-left:3px solid var(--warning,#d29922);padding:8px 10px;margin:4px 0 10px;font-size:11px;color:var(--text2);border-radius:4px">'
                 + esc(cap.notes) + '</div>';
    }
    if (cap.install_command) {
      envHtml += '<div style="font-size:11px;color:var(--muted);margin-bottom:8px">安装: <code style="background:var(--surface3);padding:2px 6px;border-radius:3px;font-size:10px">'
                 + esc(cap.install_command) + '</code></div>';
    }
    (cap.required_env||[]).forEach(e => {
      envHtml += '<label style="font-size:12px">'+e+' (required)</label><input id="mcp-env-'+e+'" class="input" style="margin-bottom:4px" placeholder="'+e+'">';
    });
    (cap.optional_env||[]).forEach(e => {
      envHtml += '<label style="font-size:12px">'+e+' (optional)</label><input id="mcp-env-'+e+'" class="input" style="margin-bottom:4px" placeholder="'+e+'">';
    });
    document.getElementById('mcp-env-fields').innerHTML = envHtml;
  });
}

async function testMCP() {
  const box = document.getElementById('mcp-test-result');
  if (box) box.innerHTML = '<span style="color:var(--muted)">⏳ 正在测试连接... (最多 15s)</span>';
  try {
    const capIdEl = document.getElementById('mcp-catalog');
    const capId = capIdEl ? capIdEl.value : '';
    const envFields = document.getElementById('mcp-env-fields').querySelectorAll('input');
    const envValues = {};
    envFields.forEach(inp => {
      const key = inp.id.replace('mcp-env-','');
      if (inp.value.trim()) envValues[key] = inp.value.trim();
    });
    let payload;
    if (capId) {
      payload = {action:'test_connection', capability_id: capId, env_values: envValues};
    } else {
      payload = {action:'test_connection', config:{
        name: document.getElementById('mcp-name').value,
        transport: document.getElementById('mcp-transport').value,
        command: document.getElementById('mcp-command').value,
        url: document.getElementById('mcp-url').value,
        env: envValues,
      }};
    }
    const r = await api('POST', '/api/portal/mcp/manage', payload);
    if (!box) return;
    if (!r) { box.innerHTML = '<span style="color:var(--danger)">✗ 请求失败</span>'; return; }
    const color = r.ok ? 'var(--success, #3fb950)' : 'var(--danger, #f85149)';
    const icon = r.ok ? '✓' : '✗';
    let html = '<div style="color:'+color+';font-weight:600">'+icon+' '+esc(r.message||'')+'</div>';
    if (r.server_info && r.server_info.serverInfo && r.server_info.serverInfo.name) {
      html += '<div style="color:var(--muted);margin-top:4px">Server: '+esc(r.server_info.serverInfo.name)+' v'+esc(r.server_info.serverInfo.version||'?')+'</div>';
    }
    if (r.stderr && !r.ok) {
      html += '<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted)">stderr</summary><pre style="font-size:10px;max-height:120px;overflow:auto;background:var(--surface3);padding:6px;border-radius:4px;white-space:pre-wrap">'+esc(r.stderr)+'</pre></details>';
    }
    box.innerHTML = html;
  } catch (e) {
    if (box) box.innerHTML = '<span style="color:var(--danger)">✗ '+esc(e.message||String(e))+'</span>';
  }
}

async function saveMCP() {
  try {
    const capIdEl = document.getElementById('mcp-catalog');
    const capId = capIdEl ? capIdEl.value : '';
    // Collect env values
    const envFields = document.getElementById('mcp-env-fields').querySelectorAll('input');
    const envValues = {};
    envFields.forEach(inp => {
      const key = inp.id.replace('mcp-env-','');
      if (inp.value.trim()) envValues[key] = inp.value.trim();
    });

    // Scope decides where the MCP lives and whether it propagates.
    // Invariant D: a Global MCP is eagerly copied into every node's
    // available_mcps server-side, so we only need to call one endpoint
    // per scope — the backend owns the propagation semantics.
    var scopeSel = document.getElementById('mcp-scope');
    var scope = scopeSel ? scopeSel.value : 'node';
    var targetNodes = null;
    if (scope === 'multi_node') {
      var raw = (document.getElementById('mcp-scope-nodes-input').value || '').trim();
      targetNodes = raw.split(',').map(function(s){return s.trim();}).filter(Boolean);
      if (!targetNodes.length) {
        alert('Multi-Node scope 需要至少一个目标 node id');
        return;
      }
    }

    let result;
    if (scope === 'global') {
      // ── Global: single authoritative source, eager-copied into all nodes ──
      if (capId) {
        result = await api('POST', '/api/portal/mcp/manage', {
          action: 'add_global_mcp',
          capability_id: capId,
          env_values: envValues,
        });
      } else {
        const name = (document.getElementById('mcp-name').value||'').trim();
        if (!name) { alert('请填写 MCP Name'); return; }
        result = await api('POST', '/api/portal/mcp/manage', {
          action: 'add_global_mcp',
          config: {
            name: name,
            transport: document.getElementById('mcp-transport').value,
            command: document.getElementById('mcp-command').value,
            url: document.getElementById('mcp-url').value,
            env: envValues,
          }
        });
      }
    } else if (capId) {
      // ── Node-scope from catalog: use the existing one-click install ──
      result = await api('POST', '/api/portal/mcp/manage', {
        action: 'install',
        capability_id: capId,
        env_values: envValues,
      });
    } else {
      // ── Node-scope custom config ──
      const name = (document.getElementById('mcp-name').value||'').trim();
      if (!name) { alert('请填写 MCP Name'); return; }
      result = await api('POST', '/api/portal/mcp/manage', {
        action: 'add_mcp',
        config: {
          name: name,
          transport: document.getElementById('mcp-transport').value,
          command: document.getElementById('mcp-command').value,
          url: document.getElementById('mcp-url').value,
          env: envValues,
        }
      });
    }
    // Post-save: if user asked for multi_node, re-scope immediately.
    // We first create the MCP (as Global or Node, whichever branch
    // ran) and then call change_scope to narrow/broaden it. This
    // keeps all creation paths going through the standard create
    // endpoints and change_scope as the single re-scoping authority.
    if (scope === 'multi_node' && result && (result.ok || result.mcp || result.id)) {
      var createdId = (result.mcp && result.mcp.id) || result.id
                    || (capId ? capId : (document.getElementById('mcp-name').value||'').trim());
      if (createdId) {
        await api('POST', '/api/portal/mcp/manage', {
          action: 'change_scope',
          mcp_id: createdId,
          scope: 'multi_node',
          target_nodes: targetNodes,
        });
      }
    }
    if (result === null) {
      alert('添加 MCP 失败，请查看浏览器控制台或服务器日志');
      return;
    }
    if (result && !result.ok && result.message) {
      alert(result.message);
      return;
    }
    if (result && result.error) {
      alert('添加 MCP 失败: ' + result.error);
      return;
    }

    // 如果正在安装，显示进度并轮询
    if (result && result.status === 'installing' && result.task_id) {
      document.querySelectorAll('#modal-overlay').forEach(function(o){ o.remove(); });
      _showInstallProgress(result.task_id, result.message || '安装中...');
      return;
    }

    // Remove all modal overlays
    document.querySelectorAll('#modal-overlay').forEach(function(o){ o.remove(); });
    renderMCPConfig();
  } catch (e) {
    console.error('saveMCP failed', e);
    alert('添加 MCP 出错: ' + (e && e.message ? e.message : e));
  }
}

function _showInstallProgress(taskId, initialMsg) {
  // 创建安装进度提示条
  var bar = document.createElement('div');
  bar.id = 'mcp-install-bar';
  bar.style.cssText = 'position:fixed;bottom:20px;right:20px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px 20px;box-shadow:0 4px 16px rgba(0,0,0,0.15);z-index:9999;min-width:300px;font-size:13px;';
  bar.innerHTML = '<div style="display:flex;align-items:center;gap:8px">'
    + '<span class="material-symbols-outlined" style="font-size:18px;animation:spin 1s linear infinite">progress_activity</span>'
    + '<span id="install-msg">' + esc(initialMsg) + '</span></div>'
    + '<div id="install-output" style="margin-top:8px;font-size:11px;color:var(--text3);max-height:80px;overflow:auto;display:none"></div>';
  document.body.appendChild(bar);

  // 轮询安装状态
  var pollTimer = setInterval(async function() {
    try {
      var task = await api('POST', '/api/portal/mcp/manage', {action:'install_status', task_id: taskId});
      if (!task || task.error) return;
      var msgEl = document.getElementById('install-msg');
      var outEl = document.getElementById('install-output');
      if (task.status === 'completed') {
        clearInterval(pollTimer);
        if (msgEl) msgEl.innerHTML = '<span style="color:var(--success)">✓ 安装成功</span>';
        if (outEl && task.output) { outEl.style.display='block'; outEl.textContent = task.output.slice(-300); }
        setTimeout(function(){ var b = document.getElementById('mcp-install-bar'); if(b) b.remove(); renderMCPConfig(); }, 2500);
      } else if (task.status === 'failed') {
        clearInterval(pollTimer);
        if (msgEl) msgEl.innerHTML = '<span style="color:var(--danger)">✗ 安装失败</span>';
        if (outEl) { outEl.style.display='block'; outEl.textContent = (task.error || task.output || '').slice(-300); }
        setTimeout(function(){ var b = document.getElementById('mcp-install-bar'); if(b) b.remove(); renderMCPConfig(); }, 5000);
      }
    } catch(e) { /* ignore poll errors */ }
  }, 2000);
}

async function retryMCPInstall(nodeId, mcpId) {
  var result = await api('POST', '/api/portal/mcp/manage', {action:'retry_install', node_id: nodeId, mcp_id: mcpId});
  if (result && result.task_id) {
    _showInstallProgress(result.task_id, result.message || '重新安装中...');
  } else {
    alert(result && result.message ? result.message : '重试失败');
  }
}

async function showEditMCPEnv(nodeId, mcpId) {
  try {
    const nodeData = await api('GET', '/api/portal/mcp/node/' + nodeId);
    const mcp = (nodeData.available_mcps || {})[mcpId];
    if (!mcp) { alert('MCP not found'); return; }
    const envOverrides = (nodeData.env_overrides || {})[mcpId] || {};
    // Merge base env + overrides
    const merged = Object.assign({}, mcp.env || {}, envOverrides);
    let html = '<div style="padding:20px;max-width:500px">';
    html += '<h3 style="margin-bottom:4px">Edit Env: ' + esc(mcp.name) + '</h3>';
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:14px;font-family:monospace">' + esc(mcpId) + '</div>';
    // Existing env vars. Each row has a delete (×) button that removes
    // the row from the DOM; saveMCPEnv() diffs _originalKeys against the
    // surviving inputs to build the delete_keys list sent to the backend.
    var keys = Object.keys(merged);
    window._mcpEnvOriginalKeys = keys.slice();
    if (keys.length) {
      keys.forEach(function(k) {
        var safeId = 'mcp-env-row-' + encodeURIComponent(k).replace(/%/g,'_');
        html += '<div id="'+safeId+'" style="margin-bottom:8px">';
        html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:2px">';
        html += '<label style="font-size:12px;font-weight:600">'+esc(k)+'</label>';
        html += '<button type="button" title="Delete this variable" onclick="deleteMCPEnvRow(\''+esc(k).replace(/\\/g,'\\\\').replace(/\'/g,"\\'")+'\')" style="background:transparent;border:none;color:var(--text3);cursor:pointer;padding:2px 6px;font-size:16px;line-height:1">×</button>';
        html += '</div>';
        html += '<input data-env-key="'+esc(k)+'" id="mcp-env-edit-'+esc(k)+'" class="input" value="'+esc(merged[k] || '')+'" style="font-size:12px">';
        html += '</div>';
      });
    }
    // Add new env var
    html += '<div style="border-top:1px solid var(--border-light);padding-top:10px;margin-top:10px">';
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">Add New Variable</div>';
    html += '<div style="display:flex;gap:6px"><input id="mcp-env-new-key" class="input" placeholder="KEY" style="flex:1;font-size:12px"><input id="mcp-env-new-val" class="input" placeholder="VALUE" style="flex:2;font-size:12px"></div>';
    html += '</div>';
    html += '<div style="display:flex;gap:8px;margin-top:16px">';
    html += '<button class="btn btn-primary" onclick="saveMCPEnv(\''+nodeId+'\',\''+mcpId+'\')">Save</button>';
    html += '<button class="btn" onclick="closeModal()">Cancel</button></div></div>';
    showModalHTML(html);
  } catch(e) { alert('Failed: ' + (e.message||e)); }
}

// Soft-delete an env row from the dialog. Actual deletion is applied
// at save time via the delete_keys diff — until Save, the user can
// cancel to abort.
function deleteMCPEnvRow(key) {
  var safeId = 'mcp-env-row-' + encodeURIComponent(key).replace(/%/g,'_');
  var row = document.getElementById(safeId);
  if (row && row.parentNode) row.parentNode.removeChild(row);
}

async function saveMCPEnv(nodeId, mcpId) {
  const envObj = {};
  // Use data-env-key attribute (safer than parsing the DOM id, which
  // may contain characters that were escaped).
  document.querySelectorAll('input[data-env-key]').forEach(function(inp) {
    const key = inp.getAttribute('data-env-key');
    if (key) envObj[key] = inp.value;
  });
  // New key
  const newKey = (document.getElementById('mcp-env-new-key') || {}).value || '';
  const newVal = (document.getElementById('mcp-env-new-val') || {}).value || '';
  if (newKey.trim()) envObj[newKey.trim()] = newVal;
  // Diff: whatever was in the original set but is no longer in envObj
  // has been removed by the user and must be deleted on the backend.
  var originals = window._mcpEnvOriginalKeys || [];
  var deleteKeys = originals.filter(function(k) { return !(k in envObj); });
  try {
    await api('POST', '/api/portal/mcp/manage', {
      action: 'set_env',
      node_id: nodeId,
      mcp_id: mcpId,
      env: envObj,
      delete_keys: deleteKeys,
    });
    closeModal();
    renderMCPConfig();
  } catch(e) { alert('Save failed: ' + (e.message||e)); }
}

async function showEditAgentMCPEnv(nodeId, agentId, mcpId) {
  try {
    // Get base MCP env + node overrides + agent overrides
    const nodeData = await api('GET', '/api/portal/mcp/node/' + nodeId);
    const mcp = (nodeData.available_mcps || {})[mcpId];
    if (!mcp) { alert('MCP not found'); return; }
    const nodeEnvOverrides = (nodeData.env_overrides || {})[mcpId] || {};
    const agentEnvOverrides = ((nodeData.agent_env_overrides || {})[agentId] || {})[mcpId] || {};
    // Show base env (read-only) and agent-specific overrides (editable)
    const baseEnv = Object.assign({}, mcp.env || {}, nodeEnvOverrides);
    const allAgents = (await api('GET', '/api/portal/agents')).agents || [];
    const agentName = (allAgents.find(a => a.id === agentId) || {}).name || agentId;
    let html = '<div style="padding:20px;max-width:500px">';
    html += '<h3 style="margin-bottom:4px">Agent 专属 Env</h3>';
    html += '<div style="font-size:12px;color:var(--text2);margin-bottom:4px">Agent: <b>' + esc(agentName) + '</b></div>';
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:14px;font-family:monospace">' + esc(mcpId) + '</div>';
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:10px;background:var(--surface3);padding:8px;border-radius:6px">';
    html += '此处设置的变量仅对此 Agent 生效，优先级高于节点级配置。留空则使用节点级默认值。';
    html += '</div>';
    // Show each env key with agent override value
    var keys = Object.keys(baseEnv);
    if (keys.length) {
      keys.forEach(function(k) {
        var agentVal = agentEnvOverrides[k] || '';
        var baseVal = baseEnv[k] || '';
        html += '<div style="margin-bottom:8px">';
        html += '<label style="font-size:12px;font-weight:600">' + esc(k) + '</label>';
        html += '<div style="font-size:10px;color:var(--text3);margin-bottom:2px">节点默认: ' + esc(baseVal ? baseVal.substring(0, 30) + (baseVal.length > 30 ? '...' : '') : '(空)') + '</div>';
        html += '<input id="agent-env-edit-' + esc(k) + '" class="input" value="' + esc(agentVal) + '" placeholder="留空使用默认值" style="font-size:12px">';
        html += '</div>';
      });
    }
    // Add new env var
    html += '<div style="border-top:1px solid var(--border-light);padding-top:10px;margin-top:10px">';
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">添加新变量 (Agent专属)</div>';
    html += '<div style="display:flex;gap:6px"><input id="agent-env-new-key" class="input" placeholder="KEY" style="flex:1;font-size:12px"><input id="agent-env-new-val" class="input" placeholder="VALUE" style="flex:2;font-size:12px"></div>';
    html += '</div>';
    html += '<div style="display:flex;gap:8px;margin-top:16px">';
    html += '<button class="btn btn-primary" onclick="saveAgentMCPEnv(\''+nodeId+'\',\''+agentId+'\',\''+mcpId+'\')">Save</button>';
    html += '<button class="btn" onclick="showBindMCP(\''+nodeId+'\',\''+mcpId+'\')">Back</button></div></div>';
    showModalHTML(html);
  } catch(e) { alert('Failed: ' + (e.message||e)); }
}

async function saveAgentMCPEnv(nodeId, agentId, mcpId) {
  const envObj = {};
  document.querySelectorAll('[id^="agent-env-edit-"]').forEach(function(inp) {
    const key = inp.id.replace('agent-env-edit-','');
    if (inp.value.trim()) envObj[key] = inp.value;  // Only save non-empty overrides
  });
  const newKey = (document.getElementById('agent-env-new-key') || {}).value || '';
  const newVal = (document.getElementById('agent-env-new-val') || {}).value || '';
  if (newKey.trim()) envObj[newKey.trim()] = newVal;
  try {
    await api('POST', '/api/portal/mcp/manage', {action:'set_env', node_id: nodeId, agent_id: agentId, mcp_id: mcpId, env: envObj});
    showBindMCP(nodeId, mcpId);  // Go back to bind modal
  } catch(e) { alert('Save failed: ' + (e.message||e)); }
}

async function syncGlobalToNode(nodeId) {
  try {
    var result = await api('POST', '/api/portal/mcp/manage', {action:'sync_global_to_node', node_id: nodeId});
    if (result && result.synced) {
      var msg = '已同步 ' + result.synced.length + ' 个 Global MCP 到 ' + nodeId;
      if (result.errors && result.errors.length) msg += '\n部分错误: ' + result.errors.join(', ');
      alert(msg);
    } else {
      alert('同步完成');
    }
    renderMCPConfig();
  } catch(e) {
    alert('同步失败: ' + (e.message||e));
  }
}

function _switchMCPCatalogTab(tab) {
  var globalDiv = document.getElementById('mcp-catalog-global');
  var nodeDiv = document.getElementById('mcp-catalog-node');
  var globalTab = document.getElementById('mcp-tab-global');
  var nodeTab = document.getElementById('mcp-tab-node');
  var hint = document.getElementById('mcp-catalog-hint');
  if (tab === 'global') {
    if (globalDiv) globalDiv.style.display = '';
    if (nodeDiv) nodeDiv.style.display = 'none';
    if (globalTab) { globalTab.style.borderBottom = '2px solid var(--primary)'; globalTab.style.color = 'var(--primary)'; globalTab.style.fontWeight = '700'; }
    if (nodeTab) { nodeTab.style.borderBottom = '2px solid transparent'; nodeTab.style.color = 'var(--text3)'; nodeTab.style.fontWeight = '400'; }
    if (hint) hint.textContent = 'Global MCP: 调用外部 API / 云服务，配置一次可同步到所有 Node';
  } else {
    if (globalDiv) globalDiv.style.display = 'none';
    if (nodeDiv) nodeDiv.style.display = '';
    if (nodeTab) { nodeTab.style.borderBottom = '2px solid var(--primary)'; nodeTab.style.color = 'var(--primary)'; nodeTab.style.fontWeight = '700'; }
    if (globalTab) { globalTab.style.borderBottom = '2px solid transparent'; globalTab.style.color = 'var(--text3)'; globalTab.style.fontWeight = '400'; }
    if (hint) hint.textContent = 'Node MCP: 需要本地安装，依赖本机进程/资源，每个 Node 独立管理';
  }
}

function showCatalogDetail(capId) {
  api('GET', '/api/portal/mcp/catalog').then(data => {
    const cap = (data.catalog||{})[capId];
    if (!cap) return;
    let html = '<div style="padding:20px;max-width:550px">';
    html += '<h3>'+cap.name+'</h3>';
    html += '<p style="margin:8px 0;color:var(--muted)">'+cap.description+'</p>';
    // Scope badge
    var scopeLabel = cap.scope==='global'
      ? '<span style="display:inline-block;font-size:11px;background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:5px;font-weight:600;margin-bottom:8px">Global — 配置一次，同步所有 Node</span>'
      : '<span style="display:inline-block;font-size:11px;background:#fef3c7;color:#92400e;padding:2px 8px;border-radius:5px;font-weight:600;margin-bottom:8px">Node — 每个节点独立安装</span>';
    html += '<div style="margin:4px 0">'+scopeLabel+'</div>';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;margin:12px 0">';
    html += '<div><strong>Type:</strong> '+cap.server_type+'</div>';
    html += '<div><strong>Transport:</strong> '+cap.transport+'</div>';
    html += '</div>';
    if (cap.tools_provided && cap.tools_provided.length) {
      html += '<div style="margin:8px 0"><strong style="font-size:12px">Tools:</strong><br>';
      cap.tools_provided.forEach(t => html += '<span class="badge" style="font-size:10px;margin:2px">'+t+'</span>');
      html += '</div>';
    }
    if (cap.compatible_roles && cap.compatible_roles.length) {
      html += '<div style="margin:8px 0"><strong style="font-size:12px">Best for roles:</strong> '+cap.compatible_roles.join(', ')+'</div>';
    }
    if (cap.install_command) {
      html += '<div style="margin:8px 0"><strong style="font-size:12px">Install:</strong><br><code style="font-size:11px;background:var(--bg-secondary);padding:4px 8px;border-radius:4px">'+cap.install_command+'</code></div>';
    }
    html += '<div style="margin-top:16px;display:flex;gap:8px">';
    html += '<button class="btn btn-primary" onclick="closeModal();showAddMCP(\''+capId+'\')">'+(cap.scope==='global'?'配置并安装':'安装到 Node')+'</button>';
    html += '</div>';
    html += '</div>';
    showModalHTML(html);
  });
}

async function showBindMCP(nodeId, mcpId) {
  try {
    const agentsData = await api('GET', '/api/portal/agents');
    const allAgents = (agentsData && agentsData.agents) || [];
    const nodeData = await api('GET', '/api/portal/mcp/node/'+nodeId);
    const bindings = (nodeData && nodeData.agent_bindings) || {};

    // Only agents on THIS node can bind to the node's MCP
    const agents = allAgents.filter(a => (a.node_id || '') === nodeId);

    let html = '<div style="padding:20px;max-width:420px">';
    html += '<h3 style="margin-bottom:6px">Bind MCP to Agents</h3>';
    html += '<p style="font-size:11px;color:var(--muted);margin-bottom:12px">Node: <code>'+esc(nodeId)+'</code> — 只能绑定此节点上的 agents</p>';
    if (agents.length === 0) {
      html += '<div style="background:var(--surface3);padding:12px;border-radius:6px;font-size:12px;color:var(--text2)">';
      html += '此节点没有 agent。请先在 Agents 页面创建一个属于该节点的 agent，再来绑定 MCP。';
      html += '</div>';
    } else {
      const agentEnvOverrides = (nodeData && nodeData.agent_env_overrides) || {};
      agents.forEach(a => {
        const bound = (bindings[a.id]||[]).includes(mcpId);
        const hasAgentEnv = agentEnvOverrides[a.id] && agentEnvOverrides[a.id][mcpId] && Object.keys(agentEnvOverrides[a.id][mcpId]).length > 0;
        html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 0">';
        html += '<label style="display:flex;align-items:center;gap:8px;flex:1;cursor:pointer">';
        html += '<input type="checkbox" '+(bound?'checked':'')+' onchange="toggleMCPBind(\''+nodeId+'\',\''+a.id+'\',\''+mcpId+'\',this.checked)">';
        html += '<span>'+esc(a.name)+' <span style="color:var(--muted);font-size:11px">('+esc(a.role||'')+')</span></span></label>';
        if (bound) {
          html += '<button class="btn btn-sm" style="font-size:10px;padding:2px 8px" title="Agent专属Env配置" onclick="showEditAgentMCPEnv(\''+nodeId+'\',\''+a.id+'\',\''+mcpId+'\')">';
          html += '<span class="material-symbols-outlined" style="font-size:14px">tune</span>';
          if (hasAgentEnv) html += ' <span style="color:var(--primary);font-size:10px">●</span>';
          html += '</button>';
        }
        html += '</div>';
      });
      html += '<div style="margin-top:8px;padding:8px;background:var(--surface3);border-radius:6px;font-size:11px;color:var(--text3)">';
      html += '<span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle">info</span> ';
      html += '点击 <span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle">tune</span> 可为每个Agent设置独立的MCP环境变量（如不同邮箱账号）。● 表示已有专属配置。';
      html += '</div>';
    }
    html += '<div style="margin-top:16px;display:flex;justify-content:flex-end"><button class="btn btn-sm" onclick="closeModal();renderMCPConfig()">Done</button></div></div>';
    showModalHTML(html);
  } catch (e) {
    alert('打开 Bind 失败: '+(e && e.message || e));
  }
}

async function toggleMCPBind(nodeId, agentId, mcpId, bind) {
  await api('POST', '/api/portal/mcp/manage', {
    action: bind ? 'bind_agent' : 'unbind_agent',
    node_id: nodeId, agent_id: agentId, mcp_id: mcpId,
  });
}

async function removeMCPFromNode(nodeId, mcpId) {
  if (!confirm('Remove this MCP server from the node?')) return;
  await api('POST', '/api/portal/mcp/manage', {action:'remove_mcp', node_id:nodeId, mcp_id:mcpId});
  renderMCPConfig();
}

// ============ Enhancement Panel ============
async function showEnhancementPanel(agentId) {
  const data = await api('GET', '/api/portal/agent/' + agentId + '/enhancement');
  if (!data) return;
  const enh = data.enhancement;
  const presets = data.presets || [];
  const ag = agents.find(a => a.id === agentId) || {};

  let html = '<div style="padding:20px;max-height:80vh;overflow-y:auto">';
  html += '<h3 style="margin-bottom:4px;font-family:\'Plus Jakarta Sans\',sans-serif">专业领域</h3>';
  html += '<p style="color:var(--text2);font-size:12px;margin-bottom:16px">' + esc(ag.name || agentId) + ' — 为 Agent 装载专业领域，让它在特定场景变得更专业</p>';

  if (!enh) {
    // Not enabled — show preset multi-selection (up to 8)
    window._enhSelected = window._enhSelected || {};
    window._enhSelected[agentId] = [];
    html += '<div style="margin-bottom:16px"><p style="font-size:13px;color:var(--text2);margin-bottom:8px">选择最多 8 个专业领域来装载 (点击卡片切换选中):</p>';
    html += '<div id="enh-selected-bar" style="min-height:28px;margin-bottom:10px;font-size:12px;color:var(--text3)">未选择</div>';
    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">';
    presets.forEach(p => {
      html += '<div class="enh-preset-card" data-preset-id="' + esc(p.id) + '" ' +
        'style="background:var(--surface3);border-radius:10px;padding:14px;cursor:pointer;border:1px solid var(--border-light);transition:all 0.2s" ' +
        'onclick="toggleEnhPreset(\'' + agentId + '\',\'' + esc(p.id) + '\',this)">' +
        '<div style="font-size:20px;margin-bottom:6px">' + (p.icon||'📦') + '</div>' +
        '<div style="font-weight:700;font-size:13px">' + esc(p.name) + '</div>' +
        '<div style="font-size:11px;color:var(--text3);margin-top:4px">' + esc(p.description) + '</div>' +
      '</div>';
    });
    // Custom domain
    html += '<div style="background:var(--surface3);border-radius:10px;padding:14px;cursor:pointer;border:1px dashed var(--border-light)" ' +
      'onclick="enableCustomEnhancement(\'' + agentId + '\')">' +
      '<div style="font-size:20px;margin-bottom:6px">✏️</div>' +
      '<div style="font-weight:700;font-size:13px">自定义技能</div>' +
      '<div style="font-size:11px;color:var(--text3);margin-top:4px">创建空白技能，手动添加知识和思维模式</div>' +
    '</div>';
    html += '</div>';
    html += '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">' +
      '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary btn-sm" id="enh-apply-btn" disabled onclick="applySelectedEnhancements(\'' + agentId + '\')">装载选中技能</button>' +
      '</div>';
    html += '</div>';
  } else {
    // Enabled — show stats and management
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">';
    html += '<div style="display:flex;align-items:center;gap:8px">';
    html += '<span style="background:var(--primary);color:#000;padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700">' + esc(enh.domain) + '</span>';
    html += '<span style="color:var(--success);font-size:12px">● Active</span>';
    html += '</div>';
    html += '<button class="btn btn-sm" style="color:var(--error);font-size:11px" onclick="disableEnhancement(\'' + agentId + '\')">Unload</button>';
    html += '</div>';

    // Stats
    html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px">';
    [{l:'Knowledge',v:enh.knowledge_entries,i:'auto_stories'},{l:'Reasoning',v:enh.reasoning_patterns,i:'psychology'},{l:'Memory',v:enh.memory_nodes,i:'neurology'},{l:'Tool Chains',v:enh.tool_chains,i:'account_tree'}].forEach(s => {
      html += '<div style="background:var(--surface3);border-radius:8px;padding:12px;text-align:center">' +
        '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">' + s.i + '</span>' +
        '<div style="font-size:18px;font-weight:700;margin:4px 0">' + s.v + '</div>' +
        '<div style="font-size:10px;color:var(--text3);text-transform:uppercase">' + s.l + '</div></div>';
    });
    html += '</div>';

    // Add knowledge button
    html += '<div style="display:flex;gap:8px;margin-bottom:16px">';
    html += '<button class="btn btn-primary btn-sm" onclick="showAddKnowledge(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:14px">add</span> Add Knowledge</button>';
    html += '<button class="btn btn-sm" onclick="showAddReasoningPattern(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:14px">psychology</span> Add Reasoning</button>';
    html += '<button class="btn btn-sm" onclick="showAddMemory(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:14px">neurology</span> Add Memory</button>';
    html += '</div>';

    // Knowledge entries list
    if (enh.knowledge_list && enh.knowledge_list.length) {
      html += '<div style="margin-bottom:12px"><div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Knowledge Entries</div>';
      enh.knowledge_list.forEach(function(k) {
        html += '<div style="display:flex;align-items:center;justify-content:space-between;background:var(--surface3);border-radius:6px;padding:8px 10px;margin-bottom:4px">' +
          '<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text)">' + esc(k.title) + '</div><div style="font-size:10px;color:var(--text3)">' + esc(k.category||'') + '</div></div>' +
          '<button class="btn btn-sm" style="color:var(--error);font-size:10px;padding:2px 6px" onclick="removeEnhItem(\'' + agentId + '\',\'remove_knowledge\',\'entry_id\',\'' + k.id + '\')">✕</button></div>';
      });
      html += '</div>';
    }

    // Reasoning patterns list
    if (enh.reasoning_list && enh.reasoning_list.length) {
      html += '<div style="margin-bottom:12px"><div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Reasoning Patterns</div>';
      enh.reasoning_list.forEach(function(r) {
        html += '<div style="display:flex;align-items:center;justify-content:space-between;background:var(--surface3);border-radius:6px;padding:8px 10px;margin-bottom:4px">' +
          '<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text)">' + esc(r.name) + '</div><div style="font-size:10px;color:var(--text3)">' + esc(r.description||'') + '</div></div>' +
          '<button class="btn btn-sm" style="color:var(--error);font-size:10px;padding:2px 6px" onclick="removeEnhItem(\'' + agentId + '\',\'remove_reasoning_pattern\',\'pattern_id\',\'' + r.id + '\')">✕</button></div>';
      });
      html += '</div>';
    }

    // Memory nodes list
    if (enh.memory_list && enh.memory_list.length) {
      html += '<div style="margin-bottom:12px"><div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Memory Nodes</div>';
      enh.memory_list.forEach(function(m) {
        html += '<div style="display:flex;align-items:center;justify-content:space-between;background:var(--surface3);border-radius:6px;padding:8px 10px;margin-bottom:4px">' +
          '<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text)">' + esc(m.title) + '</div><div style="font-size:10px;color:var(--text3)">' + esc(m.kind||'') + ' — imp: ' + (m.importance||0).toFixed(1) + '</div></div>' +
          '<button class="btn btn-sm" style="color:var(--error);font-size:10px;padding:2px 6px" onclick="removeEnhItem(\'' + agentId + '\',\'remove_memory\',\'node_id\',\'' + m.id + '\')">✕</button></div>';
      });
      html += '</div>';
    }

    // Usage stats
    html += '<div style="font-size:11px;color:var(--text3);padding:8px 0;border-top:1px solid var(--border-light)">';
    html += 'Enhance calls: ' + (enh.enhance_count||0) + ' | Recalls: ' + (enh.recall_count||0) + ' | Reflections: ' + (enh.reflection_count||0) + ' | Learned: ' + (enh.learn_count||0);
    html += '</div>';
  }
  html += '</div>';
  showModalHTML(html);
}

async function enableEnhancement(agentId, domain) {
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {action:'enable', domain});
  closeModal();
  refresh();
}

function toggleEnhPreset(agentId, presetId, el) {
  window._enhSelected = window._enhSelected || {};
  const list = window._enhSelected[agentId] || [];
  const idx = list.indexOf(presetId);
  if (idx >= 0) {
    list.splice(idx, 1);
    el.style.borderColor = 'var(--border-light)';
    el.style.background = 'var(--surface3)';
  } else {
    if (list.length >= 8) { alert('最多只能选择 8 个技能'); return; }
    list.push(presetId);
    el.style.borderColor = 'var(--primary)';
    el.style.background = 'rgba(203,201,255,0.08)';
  }
  window._enhSelected[agentId] = list;
  const bar = document.getElementById('enh-selected-bar');
  if (bar) bar.textContent = list.length ? ('已选 ' + list.length + '/8: ' + list.join(' + ')) : '未选择';
  const btn = document.getElementById('enh-apply-btn');
  if (btn) btn.disabled = list.length === 0;
}

async function applySelectedEnhancements(agentId) {
  const list = (window._enhSelected && window._enhSelected[agentId]) || [];
  if (!list.length) return;
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {action:'enable', domains:list});
  window._enhSelected[agentId] = [];
  closeModal();
  refresh();
}

function enableCustomEnhancement(agentId) {
  const domain = prompt('输入自定义技能名称 (如: blockchain, game_dev):');
  if (domain) enableEnhancement(agentId, domain.trim());
}

async function disableEnhancement(agentId) {
  if (!confirm('确定要卸载所有技能吗？学习记忆将丢失。')) return;
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {action:'disable'});
  closeModal();
  refresh();
}

function showAddKnowledge(agentId) {
  const html = '<div style="padding:16px"><h3 style="margin-bottom:16px">Add Domain Knowledge</h3>' +
    '<div class="form-group"><label>Title</label><input id="enh-kb-title" placeholder="e.g. SQL注入检测模式"></div>' +
    '<div class="form-group"><label>Content</label><textarea id="enh-kb-content" rows="4" style="width:100%;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px;font-size:13px" placeholder="详细知识内容..."></textarea></div>' +
    '<div class="form-group"><label>Category</label><select id="enh-kb-cat"><option value="pattern">Pattern</option><option value="best_practice">Best Practice</option><option value="constraint">Constraint</option><option value="pitfall">Pitfall</option><option value="reference">Reference</option></select></div>' +
    '<div class="form-group"><label>Tags (comma separated)</label><input id="enh-kb-tags" placeholder="security, sql, web"></div>' +
    '<div class="form-group"><label>Priority (0-10)</label><input id="enh-kb-priority" type="number" value="5" min="0" max="10"></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">' +
    '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
    '<button class="btn btn-primary btn-sm" onclick="saveKnowledgeEntry(\'' + agentId + '\')">Save</button></div></div>';
  showModalHTML(html);
}

async function saveKnowledgeEntry(agentId) {
  const tags = document.getElementById('enh-kb-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {
    action: 'add_knowledge',
    title: document.getElementById('enh-kb-title').value,
    content: document.getElementById('enh-kb-content').value,
    category: document.getElementById('enh-kb-cat').value,
    tags: tags,
    priority: parseInt(document.getElementById('enh-kb-priority').value) || 5,
  });
  closeModal();
  showEnhancementPanel(agentId);
}

function showAddReasoningPattern(agentId) {
  const html = '<div style="padding:16px"><h3 style="margin-bottom:16px">Add Reasoning Pattern</h3>' +
    '<div class="form-group"><label>Pattern Name</label><input id="enh-rp-name" placeholder="e.g. 安全审计分析"></div>' +
    '<div class="form-group"><label>Description</label><input id="enh-rp-desc" placeholder="What type of task this pattern guides"></div>' +
    '<div class="form-group"><label>Trigger Keywords (comma separated)</label><input id="enh-rp-kw" placeholder="安全, audit, 漏洞"></div>' +
    '<div class="form-group"><label>Steps (JSON array)</label><textarea id="enh-rp-steps" rows="5" style="width:100%;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px;font-family:monospace;font-size:12px" placeholder=\'[{"name":"Step 1","instruction":"What to think about"}]\'></textarea></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">' +
    '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
    '<button class="btn btn-primary btn-sm" onclick="saveReasoningPattern(\'' + agentId + '\')">Save</button></div></div>';
  showModalHTML(html);
}

async function saveReasoningPattern(agentId) {
  let steps = [];
  try { steps = JSON.parse(document.getElementById('enh-rp-steps').value); } catch(e) { alert('Invalid JSON for steps'); return; }
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {
    action: 'add_reasoning_pattern',
    name: document.getElementById('enh-rp-name').value,
    description: document.getElementById('enh-rp-desc').value,
    trigger_keywords: document.getElementById('enh-rp-kw').value.split(',').map(t => t.trim()).filter(Boolean),
    steps: steps,
  });
  closeModal();
  showEnhancementPanel(agentId);
}

function showAddMemory(agentId) {
  const html = '<div style="padding:16px"><h3 style="margin-bottom:16px">Add Memory</h3>' +
    '<div class="form-group"><label>Title</label><input id="enh-mem-title" placeholder="What was learned"></div>' +
    '<div class="form-group"><label>Content</label><textarea id="enh-mem-content" rows="3" style="width:100%;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px;font-size:13px" placeholder="Details..."></textarea></div>' +
    '<div class="form-group"><label>Kind</label><select id="enh-mem-kind"><option value="observation">Observation</option><option value="lesson">Lesson</option><option value="error_fix">Error Fix</option><option value="success_pattern">Success Pattern</option></select></div>' +
    '<div class="form-group"><label>Importance (0.0 - 1.0)</label><input id="enh-mem-imp" type="number" value="0.5" min="0" max="1" step="0.1"></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">' +
    '<button class="btn btn-sm" onclick="closeModal()">Cancel</button>' +
    '<button class="btn btn-primary btn-sm" onclick="saveMemoryEntry(\'' + agentId + '\')">Save</button></div></div>';
  showModalHTML(html);
}

async function saveMemoryEntry(agentId) {
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', {
    action: 'add_memory',
    title: document.getElementById('enh-mem-title').value,
    content: document.getElementById('enh-mem-content').value,
    kind: document.getElementById('enh-mem-kind').value,
    importance: parseFloat(document.getElementById('enh-mem-imp').value) || 0.5,
  });
  closeModal();
  showEnhancementPanel(agentId);
}

async function removeEnhItem(agentId, action, idKey, idVal) {
  if (!confirm('确定要删除这个条目吗？')) return;
  var body = {action: action};
  body[idKey] = idVal;
  await api('POST', '/api/portal/agent/' + agentId + '/enhancement', body);
  showEnhancementPanel(agentId);
}

// ============ Skill Panel ============
// Shows per-agent real granted skill packages (send_email, jimeng_video, ...) as
// the primary view. Prompt packs (the old markdown-based SkillSystem, now renamed
// PromptEnhancer) are shown as a collapsible secondary section.
// Catalog browsing / installation lives in the left-nav "技能商店 Store" page.
async function showSkillPanel(agentId) {
  // Primary data: real granted skill packages for this agent
  var granted = [];
  try {
    var gd = await api('GET', '/api/portal/agent/' + agentId + '/skill-pkgs');
    granted = (gd && gd.skills) || [];
  } catch(e) { granted = []; }

  // Secondary data: bound prompt packs
  var packsData = {};
  try {
    packsData = await api('GET', '/api/portal/agent/' + agentId + '/prompt-packs') || {};
  } catch(e) { packsData = {}; }
  var boundPacks = (packsData && packsData.bound_skills) || [];

  // Agent detail for MCP/tool summary
  try { window._currentAgentForSkillPanel = await api('GET', '/api/portal/agent/' + agentId); } catch(e) { window._currentAgentForSkillPanel = {}; }
  var agDetail = window._currentAgentForSkillPanel || {};
  var mcpList = agDetail.mcp_servers || [];

  var html = '<div style="padding:20px;max-width:900px;min-width:600px">';
  html += '<h3 style="margin-bottom:4px"><span class="material-symbols-outlined" style="vertical-align:middle;color:#a78bfa">build_circle</span> 能力扩展 Capabilities</h3>';
  html += '<p style="font-size:11px;color:var(--text3);margin-bottom:16px">此 Agent 可调用的真实能力：已授权的 skill 包、MCP 服务，以及提示词增强 Prompt Packs。</p>';

  // ── MCP Services overview ──
  if (mcpList.length) {
    html += '<div style="margin-bottom:16px;padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:8px">';
    html += '<div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:8px">MCP Services</div>';
    mcpList.forEach(function(m) {
      var name = (typeof m === 'string') ? m : (m.name || m.id || '');
      html += '<span style="display:inline-block;background:rgba(167,139,250,0.1);border:1px solid rgba(167,139,250,0.3);border-radius:6px;padding:3px 8px;font-size:11px;color:#a78bfa;margin:2px 4px 2px 0">' + esc(name) + '</span>';
    });
    html += '</div>';
  }

  // ── Section 1: Granted Skill Packages (primary) ──
  html += '<div style="margin-bottom:18px">';
  html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">';
  html += '<div style="font-size:13px;font-weight:700;color:var(--text)">已授权技能 Granted Skills <span style="color:var(--text3);font-weight:400">(' + granted.length + ')</span></div>';
  html += '<button class="btn btn-sm" style="font-size:10px" onclick="showView(\'roles-skills\', document.querySelector(\'[data-view=roles-skills]\'));closeModal();">+ 从技能商店授权更多</button>';
  html += '</div>';
  if (!granted.length) {
    html += '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center;border:1px dashed var(--border);border-radius:8px">此 Agent 暂未授权任何可执行技能。<br>去 <a href="#" onclick="showView(\'roles-skills\', document.querySelector(\'[data-view=roles-skills]\'));closeModal();return false;" style="color:var(--primary)">技能商店</a> 授权可执行技能包。</div>';
  } else {
    granted.forEach(function(s) {
      var manifest = s.manifest || {};
      var name = manifest.name || s.id || '';
      var desc = manifest.description || '';
      var runtime = manifest.runtime || '';
      var mcpDeps = (manifest.depends_on && manifest.depends_on.mcp) || [];
      var status = s.status || 'installed';
      var statusColor = status === 'ready' ? '#10b981' : (status === 'needs_dependencies' ? '#f59e0b' : 'var(--text3)');
      html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">';
      html += '<div style="flex:1;min-width:0">';
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
      html += '<span style="font-size:13px;font-weight:600">' + esc(name) + '</span>';
      html += '<span style="font-size:10px;color:' + statusColor + ';border:1px solid ' + statusColor + ';border-radius:4px;padding:1px 6px">' + esc(status) + '</span>';
      html += '</div>';
      html += '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">' + esc(desc) + '</div>';
      html += '<div style="font-size:10px;color:var(--text3)">';
      if (runtime) html += 'runtime: <span style="color:var(--text)">' + esc(runtime) + '</span>';
      if (mcpDeps.length) {
        html += ' · 依赖 MCP: ';
        mcpDeps.forEach(function(m, i) {
          var mname = (typeof m === 'string') ? m : (m.name || m.id || '');
          html += '<span style="color:#a78bfa">' + esc(mname) + '</span>' + (i < mcpDeps.length - 1 ? ', ' : '');
        });
      }
      html += '</div>';
      html += '</div>';
      html += '<button class="btn btn-sm btn-ghost" style="font-size:10px;color:var(--error);white-space:nowrap" onclick="revokeGrantedSkill(\'' + agentId + '\',\'' + esc(s.id) + '\')">撤销授权</button>';
      html += '</div></div>';
    });
  }
  html += '</div>';

  // ── Section 2: Prompt Packs (secondary, collapsible) ──
  html += '<details style="margin-bottom:12px" open>';
  html += '<summary style="cursor:pointer;font-size:13px;font-weight:700;color:var(--text);padding:8px 0;border-top:1px solid var(--border)">提示词增强 Prompt Packs <span style="color:var(--text3);font-weight:400">(' + boundPacks.length + ')</span></summary>';
  html += '<div style="padding:8px 0 4px 0">';
  html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">';
  html += '<div style="font-size:11px;color:var(--text3)">注入到 system prompt 的参考文档，不是可执行代码。用来给 agent 提供背景知识或行为规范。</div>';
  html += '<div style="display:flex;gap:6px;flex-shrink:0;margin-left:10px">';
  html += '<button class="btn btn-sm" style="font-size:10px" onclick="ppOpenCatalog(\''+agentId+'\')"><span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle">storefront</span> 市场</button>';
  html += '<button class="btn btn-sm" style="font-size:10px" onclick="ppOpenDiscovered(\''+agentId+'\')"><span class="material-symbols-outlined" style="font-size:13px;vertical-align:middle">inventory_2</span> 已发现</button>';
  html += '</div></div>';

  // Bound packs list
  if (!boundPacks.length) {
    html += '<div style="color:var(--text3);font-size:12px;padding:16px;text-align:center">暂未绑定任何提示词增强包。</div>';
  } else {
    boundPacks.forEach(function(s) {
      html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center">';
      html += '<div style="flex:1">';
      html += '<span style="font-size:12px;font-weight:600">' + esc(s.name||'') + '</span>';
      html += '<div style="font-size:10px;color:var(--text3);margin-top:2px">' + esc(s.description||'') + '</div>';
      html += '</div>';
      html += '<button class="btn btn-sm btn-ghost" style="font-size:10px;margin-left:10px;white-space:nowrap" onclick="unbindSkill(\'' + agentId + '\',\'' + esc(s.skill_id||'') + '\')">解除</button>';
      html += '</div></div>';
    });
  }

  // Hint
  html += '<div style="margin-top:8px;font-size:10px;color:var(--text3);text-align:center">'
    + '技能包（可执行）请前往 <a href="#" onclick="event.preventDefault();closeModal();switchTab(\'skill-store\')" style="color:var(--primary);text-decoration:underline">技能商店</a> 管理'
    + '</div>';

  html += '</div></details>';

  // Footer
  html += '<div style="margin-top:16px;display:flex;gap:8px;border-top:1px solid var(--border);padding-top:14px">';
  html += '<button class="btn btn-ghost" onclick="closeModal()">Close</button>';
  html += '</div></div>';

  showModalHTML(html);
}

// Revoke a granted skill package from an agent (uses the existing skill-pkgs
// revoke endpoint, which clears both registry grant and agent.granted_skills).
async function revokeGrantedSkill(agentId, skillInstallId) {
  if (!confirm('确认撤销对该 agent 的技能授权？')) return;
  try {
    await api('POST', '/api/portal/skill-pkgs/' + encodeURIComponent(skillInstallId) + '/revoke', {agent_id: agentId});
    showSkillPanel(agentId);
  } catch(e) {
    alert('撤销失败: ' + (e.message || e));
  }
}

async function switchSkillTab(agentId, tabName, event) {
  event.preventDefault();
  // Hide all tabs
  document.getElementById('tab-bound').style.display = 'none';
  document.getElementById('tab-all').style.display = 'none';
  document.getElementById('tab-marketplace').style.display = 'none';
  document.getElementById('tab-local').style.display = 'none';

  // Show selected tab
  document.getElementById('tab-' + tabName).style.display = 'block';

  // Update button styles
  var buttons = document.querySelectorAll('.skill-tab-btn');
  buttons.forEach(function(btn) {
    if (btn.getAttribute('data-tab') === tabName) {
      btn.style.color = 'var(--primary)';
      btn.style.borderBottom = '2px solid var(--primary)';
    } else {
      btn.style.color = 'var(--text3)';
      btn.style.borderBottom = 'none';
    }
  });
}

async function loadAllSkillsTab(agentId) {
  var tabDiv = document.getElementById('tab-all');
  try {
    const data = await api('GET', '/api/portal/prompt-packs');
    var skills = (data && data.skills) || [];
    var bound = (data && data.bound_prompt_packs) || [];

    if (!skills.length) {
      tabDiv.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text3)">No skills discovered</div>';
      return;
    }

    let html = '';
    skills.forEach(function(s) {
      var isBound = bound.includes(s.skill_id);
      html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center">';
      html += '<div style="flex:1">';
      html += '<span style="font-size:13px;font-weight:600">' + esc(s.name) + '</span>';
      html += '<div style="font-size:11px;color:var(--text3);margin-top:4px">' + esc(s.description||'') + '</div>';
      html += '</div>';
      if (!isBound) {
        html += '<button class="btn btn-sm" style="font-size:10px;margin-left:10px;white-space:nowrap" onclick="bindSkill(\'' + agentId + '\',\'' + s.skill_id + '\')">Bind</button>';
      } else {
        html += '<span style="font-size:10px;color:var(--text3);margin-left:10px">Bound</span>';
      }
      html += '</div></div>';
    });
    tabDiv.innerHTML = html;
  } catch (e) {
    tabDiv.innerHTML = '<div style="padding:20px;color:red">Error loading skills: ' + e.message + '</div>';
  }
}

async function loadMarketplaceTab(agentId) {
  var tabDiv = document.getElementById('tab-marketplace');
  try {
    const catalogData = await api('POST', '/api/portal/prompt-packs', {action: 'catalog'});
    var skills = (catalogData && catalogData.skills) || [];
    var categories = (catalogData && catalogData.categories) || [];

    let html = '<div style="margin-bottom:16px">';
    html += '<div style="display:flex;gap:8px;margin-bottom:12px">';
    html += '<select id="marketplace-category-filter" onchange="filterMarketplaceSkills(\'' + agentId + '\')" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:12px">';
    html += '<option value="">All Categories</option>';
    categories.forEach(function(cat) {
      html += '<option value="' + esc(cat) + '">' + esc(cat) + '</option>';
    });
    html += '</select>';
    html += '<input type="text" id="marketplace-search" placeholder="Search skills..." onkeyup="filterMarketplaceSkills(\'' + agentId + '\')" style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:12px">';
    html += '</div></div>';

    html += '<div id="marketplace-skills">';
    if (!skills.length) {
      html += '<div style="padding:20px;text-align:center;color:var(--text3)">No skills in catalog</div>';
    } else {
      skills.forEach(function(s) {
        html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px">';
        html += '<div style="display:flex;align-items:flex-start;gap:12px">';
        html += '<div style="font-size:24px">' + esc(s.icon||'') + '</div>';
        html += '<div style="flex:1">';
        html += '<div style="display:flex;justify-content:space-between;align-items:start">';
        html += '<div style="flex:1">';
        html += '<span style="font-size:13px;font-weight:600">' + esc(s.name) + '</span>';
        html += '<div style="font-size:11px;color:var(--text3);margin-top:4px;margin-bottom:6px">' + esc(s.description||'') + '</div>';
        html += '<span style="display:inline-block;font-size:10px;background:var(--primary);color:white;padding:2px 8px;border-radius:4px">' + esc(s.category||'') + '</span>';
        html += '</div>';
        html += '<button class="btn btn-sm" style="font-size:10px;white-space:nowrap" onclick="importFromCatalog(\'' + agentId + '\', [\'' + esc(s.id) + '\'])">Import</button>';
        html += '</div></div></div></div>';
      });
    }
    html += '</div>';

    tabDiv.innerHTML = html;
  } catch (e) {
    tabDiv.innerHTML = '<div style="padding:20px;color:red">Error loading catalog: ' + e.message + '</div>';
  }
}

async function filterMarketplaceSkills(agentId) {
  var category = document.getElementById('marketplace-category-filter').value;
  var search = document.getElementById('marketplace-search').value;

  try {
    const catalogData = await api('POST', '/api/portal/prompt-packs', {action: 'catalog', category: category, search: search});
    var skills = (catalogData && catalogData.skills) || [];

    let html = '';
    if (!skills.length) {
      html = '<div style="padding:20px;text-align:center;color:var(--text3)">No matching skills</div>';
    } else {
      skills.forEach(function(s) {
        html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:8px">';
        html += '<div style="display:flex;align-items:flex-start;gap:12px">';
        html += '<div style="font-size:24px">' + esc(s.icon||'') + '</div>';
        html += '<div style="flex:1">';
        html += '<div style="display:flex;justify-content:space-between;align-items:start">';
        html += '<div style="flex:1">';
        html += '<span style="font-size:13px;font-weight:600">' + esc(s.name) + '</span>';
        html += '<div style="font-size:11px;color:var(--text3);margin-top:4px;margin-bottom:6px">' + esc(s.description||'') + '</div>';
        html += '<span style="display:inline-block;font-size:10px;background:var(--primary);color:white;padding:2px 8px;border-radius:4px">' + esc(s.category||'') + '</span>';
        html += '</div>';
        html += '<button class="btn btn-sm" style="font-size:10px;white-space:nowrap" onclick="importFromCatalog(\'' + agentId + '\', [\'' + esc(s.id) + '\'])">Import</button>';
        html += '</div></div></div></div>';
      });
    }
    document.getElementById('marketplace-skills').innerHTML = html;
  } catch (e) {
    document.getElementById('marketplace-skills').innerHTML = '<div style="padding:20px;color:red">Error: ' + e.message + '</div>';
  }
}

async function bindSkill(agentId, skillId) {
  await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'bind', skill_id: skillId});
  showSkillPanel(agentId);
}

async function unbindSkill(agentId, skillId) {
  await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'unbind', skill_id: skillId});
  showSkillPanel(agentId);
}

async function importFromCatalog(agentId, skillIds) {
  console.log('[importFromCatalog]', agentId, skillIds);
  try {
    var r = await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'import_from_catalog', skill_ids: skillIds});
    console.log('[importFromCatalog] response', r);
    if (!r) {
      alert('导入失败：无响应');
      return;
    }
    if (r.error) {
      alert('导入失败: ' + r.error);
      return;
    }
    // Inline success toast inside the marketplace panel
    var mkt = document.getElementById('marketplace-skills');
    if (mkt) {
      var banner = document.createElement('div');
      banner.style.cssText = 'padding:10px;background:#10b981;color:white;border-radius:6px;font-size:12px;margin-bottom:10px';
      banner.textContent = '✅ 已导入 ' + (r.imported || 0) + ' 个技能并绑定到 Agent';
      mkt.parentNode.insertBefore(banner, mkt);
      setTimeout(function(){ banner.remove(); }, 3000);
    }
    // Refresh skill panel but stay on marketplace tab
    showSkillPanel(agentId);
    setTimeout(function(){
      try { switchSkillTab(agentId, 'marketplace', {currentTarget: document.querySelector('[data-tab="marketplace"]')}); } catch(_){}
    }, 50);
  } catch (e) {
    console.error('[importFromCatalog] error', e);
    alert('导入失败: ' + (e && e.message ? e.message : e));
  }
}

async function importLocal(agentId) {
  var path = document.getElementById('local-import-path').value;
  if (!path.trim()) {
    alert('Please enter a directory path');
    return;
  }
  try {
    const result = await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'import_local', path: path});
    var resultDiv = document.getElementById('local-import-result');
    if (result.ok) {
      resultDiv.innerHTML = '<div style="padding:10px;background:#10b981;color:white;border-radius:6px;font-size:12px">Successfully imported ' + result.new_skills + ' new skills from ' + esc(path) + '</div>';
    } else {
      resultDiv.innerHTML = '<div style="padding:10px;background:#ef4444;color:white;border-radius:6px;font-size:12px">Error: ' + esc(result.error) + '</div>';
    }
  } catch (e) {
    var resultDiv = document.getElementById('local-import-result');
    resultDiv.innerHTML = '<div style="padding:10px;background:#ef4444;color:white;border-radius:6px;font-size:12px">Error: ' + e.message + '</div>';
  }
}

async function discoverSkills(agentId) {
  await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'discover'});
  showSkillPanel(agentId);
}

// ── Prompt Pack import helpers (used by the inline import panel in showSkillPanel) ──

function _ppFlash(agentId, ok, msg) {
  var box = document.getElementById('pp-import-result-' + agentId);
  if (!box) { alert(msg); return; }
  var bg = ok ? '#10b981' : '#ef4444';
  box.innerHTML = '<div style="padding:8px 10px;background:'+bg+';color:white;border-radius:6px;font-size:11px">'+esc(msg)+'</div>';
  setTimeout(function(){ if (box) box.innerHTML = ''; }, 4000);
}

async function ppDiscoverNow(agentId) {
  try {
    var r = await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'discover'});
    _ppFlash(agentId, true, '✅ 已扫描，新增 ' + (r && r.new_skills || 0) + ' 个 pack（共 ' + (r && r.total || 0) + '）');
    showSkillPanel(agentId);
  } catch(e) { _ppFlash(agentId, false, '扫描失败: ' + (e.message || e)); }
}

async function ppImportLocal(agentId) {
  var inp = document.getElementById('pp-import-path-' + agentId);
  var path = inp ? inp.value.trim() : '';
  if (!path) { _ppFlash(agentId, false, '请输入本地目录路径'); return; }
  try {
    var r = await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'import_local', path: path});
    if (r && r.ok) {
      _ppFlash(agentId, true, '✅ 已从 ' + path + ' 导入 ' + (r.new_skills || 0) + ' 个 pack');
      showSkillPanel(agentId);
    } else {
      _ppFlash(agentId, false, '导入失败: ' + ((r && r.error) || '未知错误'));
    }
  } catch(e) { _ppFlash(agentId, false, '导入失败: ' + (e.message || e)); }
}

async function ppOpenCatalog(agentId) {
  var html = '<div style="padding:20px;max-width:720px;min-width:520px">' +
    '<h3 style="margin:0 0 6px"><span class="material-symbols-outlined" style="vertical-align:middle">storefront</span> Prompt Pack 市场</h3>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:14px">从社区目录中选择并一键导入到当前 Agent</div>' +
    '<div style="display:flex;gap:8px;margin-bottom:12px">' +
      '<select id="pp-cat-filter" style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px"><option value="">全部分类</option></select>' +
      '<input id="pp-cat-search" placeholder="搜索..." style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px">' +
      '<button class="btn btn-sm" onclick="_ppCatalogReload(\''+agentId+'\')">搜索</button>' +
    '</div>' +
    '<div id="pp-cat-list" style="max-height:55vh;overflow:auto"><div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">加载中...</div></div>' +
    '<div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn" onclick="closeModal()">关闭</button></div>' +
  '</div>';
  showModalHTML(html);
  // Wire enter-key search
  setTimeout(function(){
    var s = document.getElementById('pp-cat-search');
    if (s) s.addEventListener('keyup', function(ev){ if (ev.key === 'Enter') _ppCatalogReload(agentId); });
  }, 30);
  _ppCatalogReload(agentId);
}

async function _ppCatalogReload(agentId) {
  var list = document.getElementById('pp-cat-list');
  if (!list) return;
  var catEl = document.getElementById('pp-cat-filter');
  var qEl = document.getElementById('pp-cat-search');
  var category = catEl ? catEl.value : '';
  var search = qEl ? qEl.value : '';
  list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">加载中...</div>';
  try {
    var r = await api('POST', '/api/portal/prompt-packs', {action: 'catalog', category: category, search: search});
    var skills = (r && r.skills) || [];
    var cats = (r && r.categories) || [];
    // Populate categories on first load
    if (catEl && catEl.options.length <= 1 && cats.length) {
      cats.forEach(function(c){
        var o = document.createElement('option'); o.value = c; o.textContent = c; catEl.appendChild(o);
      });
    }
    if (!skills.length) {
      list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">无匹配的 Prompt Pack</div>';
      return;
    }
    var html = '';
    skills.forEach(function(s){
      var sid = esc(s.id||'');
      html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px;display:flex;align-items:flex-start;gap:10px">' +
        '<div style="font-size:20px;line-height:1">'+esc(s.icon||'📦')+'</div>' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:13px;font-weight:600">'+esc(s.name||'')+'</div>' +
          '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+esc(s.description||'')+'</div>' +
          (s.category?'<span style="display:inline-block;font-size:10px;background:var(--primary);color:white;padding:2px 8px;border-radius:4px;margin-top:4px">'+esc(s.category)+'</span>':'') +
        '</div>' +
        '<button class="btn btn-sm btn-primary" style="font-size:10px;white-space:nowrap" onclick="_ppCatalogImport(\''+agentId+'\',\''+sid+'\', this)">导入并绑定</button>' +
      '</div>';
    });
    list.innerHTML = html;
  } catch(e) {
    list.innerHTML = '<div style="color:var(--error);font-size:12px;padding:20px;text-align:center">加载失败: '+esc(e.message||String(e))+'</div>';
  }
}

async function _ppCatalogImport(agentId, skillId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '导入中...'; }
  try {
    var r = await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'import_from_catalog', skill_ids: [skillId]});
    if (r && r.ok) {
      if (btn) { btn.textContent = '✓ 已导入'; btn.style.background = '#10b981'; }
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '导入并绑定'; }
      alert('导入失败: ' + ((r && r.error) || '未知错误'));
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '导入并绑定'; }
    alert('导入失败: ' + (e.message || e));
  }
}

async function ppOpenDiscovered(agentId) {
  var html = '<div style="padding:20px;max-width:720px;min-width:520px">' +
    '<h3 style="margin:0 0 6px"><span class="material-symbols-outlined" style="vertical-align:middle">inventory_2</span> 已发现的 Prompt Packs</h3>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:14px">已通过本地扫描或之前导入注册的 pack。可直接绑定到当前 Agent。</div>' +
    '<div id="pp-disc-list" style="max-height:55vh;overflow:auto"><div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">加载中...</div></div>' +
    '<div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn" onclick="closeModal()">关闭</button></div>' +
  '</div>';
  showModalHTML(html);
  try {
    var pair = await Promise.all([
      api('GET', '/api/portal/prompt-packs'),
      api('GET', '/api/portal/agent/' + agentId + '/prompt-packs').catch(function(){ return {}; }),
    ]);
    var r = pair[0] || {};
    var agentR = pair[1] || {};
    var skills = r.skills || [];
    var boundList = (agentR.bound_prompt_packs || []).map(function(b){ return (b && b.skill_id) || b; });
    var bound = boundList;
    var list = document.getElementById('pp-disc-list');
    if (!list) return;
    if (!skills.length) {
      list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">尚未发现任何 pack。试试 "重新扫描" 或 "从目录导入"。</div>';
      return;
    }
    var html2 = '';
    skills.forEach(function(s){
      var sid = esc(s.skill_id || s.id || '');
      var isBound = bound.indexOf(s.skill_id || s.id) >= 0;
      html2 += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px;display:flex;align-items:center;gap:10px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:13px;font-weight:600">'+esc(s.name||'')+'</div>' +
          '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+esc(s.description||'')+'</div>' +
          (s.origin?'<div style="font-size:10px;color:var(--text3);margin-top:2px">来源: '+esc(s.origin)+'</div>':'') +
        '</div>' +
        (isBound
          ? '<span style="font-size:10px;color:var(--text3);white-space:nowrap">已绑定</span>'
          : '<button class="btn btn-sm btn-primary" style="font-size:10px;white-space:nowrap" onclick="_ppDiscBind(\''+agentId+'\',\''+sid+'\', this)">绑定</button>') +
      '</div>';
    });
    list.innerHTML = html2;
  } catch(e) {
    var l = document.getElementById('pp-disc-list');
    if (l) l.innerHTML = '<div style="color:var(--error);font-size:12px;padding:20px;text-align:center">加载失败: '+esc(e.message||String(e))+'</div>';
  }
}

async function _ppDiscBind(agentId, skillId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    await api('POST', '/api/portal/agent/' + agentId + '/prompt-packs', {action: 'bind', skill_id: skillId});
    if (btn) { btn.textContent = '✓ 已绑定'; btn.style.background = '#10b981'; }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '绑定'; }
    alert('绑定失败: ' + (e.message || e));
  }
}

// ============ Universal Skill Import ============

function switchImportTab(agentId, tab) {
  var localDiv = document.getElementById('imp-local-' + agentId);
  var urlDiv   = document.getElementById('imp-url-' + agentId);
  var localBtn = document.getElementById('imp-tab-local-' + agentId);
  var urlBtn   = document.getElementById('imp-tab-url-' + agentId);
  if (!localDiv || !urlDiv) return;
  if (tab === 'url') {
    localDiv.style.display = 'none';
    urlDiv.style.display = 'flex';
    if (localBtn) { localBtn.style.background = ''; localBtn.style.color = ''; }
    if (urlBtn) { urlBtn.style.background = 'var(--primary)'; urlBtn.style.color = 'white'; }
  } else {
    localDiv.style.display = 'flex';
    urlDiv.style.display = 'none';
    if (localBtn) { localBtn.style.background = 'var(--primary)'; localBtn.style.color = 'white'; }
    if (urlBtn) { urlBtn.style.background = ''; urlBtn.style.color = ''; }
  }
}

async function skillStoreImportLocal(agentId) {
  var inp = document.getElementById('imp-local-path-' + agentId);
  var path = inp ? inp.value.trim() : '';
  if (!path) { _ppFlash(agentId, false, '请输入本地路径'); return; }
  try {
    // Detect if path points at a single skill (has SKILL.md) or a directory of skills
    var r = await api('POST', '/api/portal/skill-store', {action: 'import', src_path: path, auto_install: true});
    if (r && r.ok) {
      _ppFlash(agentId, true, '已导入: ' + (r.name || path) + (r.install ? ' (已安装)' : ''));
      showSkillPanel(agentId);
    } else if (r && r.error && r.error.indexOf('no SKILL.md') >= 0) {
      // Try bulk import
      var rb = await api('POST', '/api/portal/skill-store', {action: 'import_bulk', src_root: path, auto_install: true});
      if (rb && rb.ok) {
        var names = (rb.results || []).filter(function(x){return x.ok;}).map(function(x){return x.name;});
        _ppFlash(agentId, true, '批量导入: ' + names.join(', '));
        showSkillPanel(agentId);
      } else {
        _ppFlash(agentId, false, '导入失败: ' + ((rb && rb.error) || '目录中未找到 SKILL.md'));
      }
    } else {
      _ppFlash(agentId, false, '导入失败: ' + ((r && r.error) || '未知错误'));
    }
  } catch(e) { _ppFlash(agentId, false, '导入失败: ' + (e.message || e)); }
}

async function skillStoreImportURL(agentId) {
  var inp = document.getElementById('imp-url-input-' + agentId);
  var url = inp ? inp.value.trim() : '';
  if (!url) { _ppFlash(agentId, false, '请输入 URL'); return; }
  _ppFlash(agentId, true, '正在下载...');
  try {
    var r = await api('POST', '/api/portal/skill-store', {action: 'import_from_url', url: url, auto_install: true});
    if (r && r.ok) {
      _ppFlash(agentId, true, '已导入: ' + (r.name || url));
      showSkillPanel(agentId);
    } else {
      _ppFlash(agentId, false, '导入失败: ' + ((r && r.error) || '未知错误'));
    }
  } catch(e) { _ppFlash(agentId, false, '导入失败: ' + (e.message || e)); }
}

async function skillStoreRescan(agentId) {
  try {
    var r = await api('POST', '/api/portal/skill-store', {action: 'rescan'});
    _ppFlash(agentId, true, '已扫描: ' + (r && r.count || 0) + ' 个技能');
    showSkillPanel(agentId);
  } catch(e) { _ppFlash(agentId, false, '扫描失败: ' + (e.message || e)); }
}

async function skillStoreOpenCatalog(agentId) {
  var html = '<div style="padding:20px;max-width:750px;min-width:540px">' +
    '<h3 style="margin:0 0 6px"><span class="material-symbols-outlined" style="vertical-align:middle">storefront</span> 技能市场 Skill Store</h3>' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:11px;color:var(--text3)">浏览已注册的技能。支持可执行技能 (python/shell) 和指引型技能 (markdown/SKILL.md)。</div>' +
      '<button class="btn btn-sm btn-primary" style="font-size:11px;white-space:nowrap;margin-left:12px" onclick="closeModal();skillCreatorOpen(\''+agentId+'\')">+ 新建技能</button>' +
    '</div>' +
    '<div style="display:flex;gap:8px;margin-bottom:12px">' +
      '<select id="ss-cat-source" style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px">' +
        '<option value="">全部来源</option><option value="official">official</option><option value="maintainer">maintainer</option><option value="community">community</option><option value="local">local</option>' +
      '</select>' +
      '<input id="ss-cat-q" placeholder="搜索技能名或描述..." style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px">' +
      '<button class="btn btn-sm" onclick="_ssCatalogReload(\''+agentId+'\')">搜索</button>' +
    '</div>' +
    '<div id="ss-cat-list" style="max-height:55vh;overflow:auto"><div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">加载中...</div></div>' +
    '<div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn" onclick="closeModal()">关闭</button></div>' +
  '</div>';
  showModalHTML(html);
  setTimeout(function(){
    var s = document.getElementById('ss-cat-q');
    if (s) s.addEventListener('keyup', function(ev){ if (ev.key === 'Enter') _ssCatalogReload(agentId); });
  }, 30);
  _ssCatalogReload(agentId);
}

async function _ssCatalogReload(agentId) {
  var list = document.getElementById('ss-cat-list');
  if (!list) return;
  var srcEl = document.getElementById('ss-cat-source');
  var qEl = document.getElementById('ss-cat-q');
  var source = srcEl ? srcEl.value : '';
  var q = qEl ? qEl.value : '';
  list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">加载中...</div>';
  try {
    var qs = '?all=1';
    if (source) qs += '&source=' + encodeURIComponent(source);
    if (q) qs += '&q=' + encodeURIComponent(q);
    var r = await api('GET', '/api/portal/skill-store' + qs);
    var entries = (r && r.entries) || [];
    if (!entries.length) {
      list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px;text-align:center">目录为空。把 SKILL.md 或 manifest.yaml 放到 data/skill_catalog/ 下再点"重新扫描"。</div>';
      return;
    }
    var h = '';
    entries.forEach(function(e){
      var runtimeIcon = (e.runtime === 'markdown') ? '📖' : (e.runtime === 'python' ? '🐍' : '📦');
      var installedBadge = e.installed
        ? '<span style="font-size:10px;color:#10b981;border:1px solid #10b981;border-radius:4px;padding:1px 6px;margin-left:6px">已安装</span>'
        : '';
      h += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px;display:flex;align-items:flex-start;gap:10px">' +
        '<div style="font-size:20px;line-height:1">'+runtimeIcon+'</div>' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:13px;font-weight:600">'+esc(e.name||'')+ installedBadge +'</div>' +
          '<div style="font-size:11px;color:var(--text3);margin-top:2px;max-height:40px;overflow:hidden">'+esc((e.description||'').substring(0,160))+'</div>' +
          '<div style="display:flex;gap:8px;margin-top:4px;font-size:10px;color:var(--text3)">' +
            '<span>runtime: '+esc(e.runtime||'')+'</span>' +
            '<span>spec: '+esc(e.spec||'')+'</span>' +
            '<span>source: '+esc(e.source||'')+'</span>' +
            (e.version ? '<span>v'+esc(e.version)+'</span>':'') +
          '</div>' +
        '</div>';
      h += '<div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end">';
      if (!e.installed) {
        h += '<button class="btn btn-sm btn-primary" style="font-size:10px;white-space:nowrap" onclick="_ssCatalogInstall(\''+agentId+'\',\''+esc(e.id||'')+'\',this)">安装</button>';
      } else {
        h += '<button class="btn btn-sm" style="font-size:10px;white-space:nowrap" onclick="_ssCatalogGrant(\''+agentId+'\',\''+esc(e.installed_id||'')+'\',this)">授权给 Agent</button>';
      }
      if (e.spec === 'agent-skills' || e.runtime === 'markdown') {
        h += '<button class="btn btn-sm btn-ghost" style="font-size:10px;white-space:nowrap" onclick="closeModal();skillEditorOpen(\''+agentId+'\',\''+esc(e.id||'')+'\')">编辑</button>';
      }
      h += '</div></div>';
    });
    list.innerHTML = h;
  } catch(e) {
    list.innerHTML = '<div style="color:var(--error);font-size:12px;padding:20px;text-align:center">加载失败: '+esc(e.message||String(e))+'</div>';
  }
}

async function _ssCatalogInstall(agentId, entryId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '安装中...'; }
  try {
    var r = await api('POST', '/api/portal/skill-store', {action: 'install', entry_id: entryId});
    if (r && r.ok) {
      if (btn) { btn.textContent = '已安装'; btn.style.background = '#10b981'; }
      // Refresh list after short delay
      setTimeout(function(){ _ssCatalogReload(agentId); }, 500);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '安装'; }
      alert('安装失败: ' + ((r && r.error) || ''));
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '安装'; }
    alert('安装失败: ' + (e.message || e));
  }
}

async function _ssCatalogGrant(agentId, installedId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '授权中...'; }
  try {
    await api('POST', '/api/portal/skill-store', {action: 'grant', installed_id: installedId, agent_id: agentId});
    if (btn) { btn.textContent = '已授权'; btn.style.background = '#10b981'; }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '授权给 Agent'; }
    alert('授权失败: ' + (e.message || e));
  }
}

// ── Skill Creator / Editor ──

function skillCreatorOpen(agentId) {
  var template = '---\nname: my-new-skill\ndescription: \"Describe what this skill does\"\nmetadata:\n  source: local\n  tags: []\n---\n\n# My New Skill\n\n## Overview\n\nDescribe what this skill enables the agent to do.\n\n## Instructions\n\n1. Step one...\n2. Step two...\n';
  _skillEditorModal(agentId, '', template, true);
}

async function skillEditorOpen(agentId, entryId) {
  try {
    var r = await api('POST', '/api/portal/skill-store', {action: 'preview', entry_id: entryId});
    if (!r || !r.ok) { alert('读取失败'); return; }
    _skillEditorModal(agentId, entryId, r.content || '', false, r.files || []);
  } catch(e) { alert('读取失败: ' + (e.message || e)); }
}

function _skillEditorModal(agentId, entryId, content, isNew, files) {
  var title = isNew ? '新建技能' : '编辑技能';
  var saveLabel = isNew ? '创建并安装' : '保存';
  var filesHtml = '';
  if (files && files.length) {
    filesHtml = '<div style="margin-bottom:10px;font-size:11px;color:var(--text3)">附属文件: ';
    files.forEach(function(f,i){ filesHtml += (i?', ':'') + esc(f.name) + ' ('+((f.size/1024).toFixed(1))+'KB)'; });
    filesHtml += '</div>';
  }
  var html = '<div style="padding:20px;max-width:800px;min-width:600px">' +
    '<h3 style="margin:0 0 8px">'+title+'</h3>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:10px">SKILL.md 格式：YAML frontmatter (name, description 必填) + Markdown 正文。</div>' +
    filesHtml +
    '<div style="position:relative">' +
      '<textarea id="skill-editor-content" style="width:100%;height:45vh;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;color:var(--text);font-family:monospace;font-size:12px;resize:vertical;tab-size:2">' + esc(content) + '</textarea>' +
    '</div>' +
    '<div id="skill-editor-issues" style="margin-top:8px"></div>' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:12px">' +
      '<button class="btn btn-sm" style="font-size:11px" onclick="_skillEditorValidate()">检查 SKILL.md</button>' +
      '<div style="display:flex;gap:8px">' +
        '<button class="btn" onclick="closeModal()">取消</button>' +
        '<button class="btn btn-primary" onclick="_skillEditorSave(\''+agentId+'\',\''+esc(entryId||'')+'\','+(isNew?'true':'false')+')">' + saveLabel + '</button>' +
      '</div>' +
    '</div>' +
  '</div>';
  showModalHTML(html);
}

async function _skillEditorValidate() {
  var ta = document.getElementById('skill-editor-content');
  var box = document.getElementById('skill-editor-issues');
  if (!ta || !box) return;
  try {
    var r = await api('POST', '/api/portal/skill-store', {action: 'validate', content: ta.value});
    if (r && r.ok) {
      box.innerHTML = '<div style="padding:8px;background:#10b981;color:white;border-radius:6px;font-size:11px">SKILL.md 格式正确 (name: '+esc(r.parsed_meta.name||'')+', body: '+r.body_length+' chars)</div>';
    } else {
      var issues = (r && r.issues) || ['unknown error'];
      box.innerHTML = '<div style="padding:8px;background:#ef4444;color:white;border-radius:6px;font-size:11px">问题: ' + issues.map(esc).join('; ') + '</div>';
    }
  } catch(e) {
    box.innerHTML = '<div style="padding:8px;background:#ef4444;color:white;border-radius:6px;font-size:11px">检查失败: '+esc(e.message||String(e))+'</div>';
  }
}

async function _skillEditorSave(agentId, entryId, isNew) {
  var ta = document.getElementById('skill-editor-content');
  if (!ta) return;
  var content = ta.value;
  if (!content.trim()) { alert('内容不能为空'); return; }
  try {
    var action = isNew ? 'create_new' : 'save_edit';
    var payload = {action: action, content: content};
    if (!isNew) payload.entry_id = entryId;
    var r = await api('POST', '/api/portal/skill-store', payload);
    if (r && r.ok) {
      alert((isNew ? '创建' : '保存') + '成功: ' + (r.name || ''));
      closeModal();
      // Refresh catalog if open
      setTimeout(function(){ skillStoreOpenCatalog(agentId); }, 200);
    } else {
      alert('失败: ' + ((r && r.error) || '未知错误'));
    }
  } catch(e) { alert('失败: ' + (e.message || e)); }
}

// ============ Execution Analysis Panel ============
async function showAnalysisPanel(agentId) {
  const data = await api('GET', '/api/portal/agent/' + agentId + '/analyses');
  let html = '<div style="padding:20px;max-width:750px;min-width:520px">';
  html += '<h3 style="margin-bottom:4px"><span class="material-symbols-outlined" style="vertical-align:middle;color:#34d399">analytics</span> Execution Analysis</h3>';
  html += '<div style="font-size:11px;color:var(--text3);margin-bottom:16px">Auto-analysis of recent task executions</div>';
  var analyses = (data && data.analyses) || [];
  if (!analyses.length) {
    html += '<div style="color:var(--text3);font-size:13px">No analyses yet. Analyses are generated automatically after each chat interaction.</div>';
  } else {
    analyses.forEach(function(a) {
      var ratingColor = a.auto_rating >= 4 ? '#10b981' : a.auto_rating >= 3 ? '#f59e0b' : '#ef4444';
      var statusIcon = a.task_completed ? '✅' : '❌';
      html += '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:10px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">';
      html += '<div style="font-size:13px;font-weight:600">' + statusIcon + ' ' + esc(a.task_id) + '</div>';
      html += '<div style="display:flex;align-items:center;gap:8px">';
      html += '<span style="font-size:12px;color:' + ratingColor + ';font-weight:700">' + '★'.repeat(a.auto_rating) + '<span style="color:var(--text3)">' + '★'.repeat(5-a.auto_rating) + '</span></span>';
      html += '<span style="font-size:10px;color:var(--text3)">' + new Date(a.analyzed_at*1000).toLocaleString() + '</span>';
      html += '</div></div>';
      html += '<div style="font-size:11px;color:var(--text2);margin-bottom:6px">' + esc(a.execution_note) + '</div>';
      html += '<div style="display:flex;gap:12px;font-size:10px;color:var(--text3)">';
      html += '<span>Tools: ' + a.tool_call_count + '</span>';
      html += '<span>Errors: ' + a.error_count + '</span>';
      html += '<span>Duration: ' + a.total_duration.toFixed(1) + 's</span>';
      if (a.inferred_skill_tags && a.inferred_skill_tags.length) {
        html += '<span>Skills: ' + a.inferred_skill_tags.join(', ') + '</span>';
      }
      html += '</div>';
      if (a.tool_issues && a.tool_issues.length) {
        html += '<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.04)">';
        a.tool_issues.forEach(function(ti) {
          var sevColor = ti.severity==='high'?'#ef4444':ti.severity==='medium'?'#f59e0b':'var(--text3)';
          html += '<div style="font-size:10px;color:' + sevColor + '">⚠ ' + esc(ti.tool_name) + ': ' + esc(ti.issue_type) + ' — ' + esc(ti.description) + '</div>';
        });
        html += '</div>';
      }
      html += '</div>';
    });
  }
  html += '<div style="margin-top:16px"><button class="btn" onclick="closeModal()">Close</button></div>';
  html += '</div>';
  showModalHTML(html);
}

// ============ Role Growth Path Panel ============
async function showGrowthPathPanel(agentId) {
  const data = await api('GET', '/api/portal/agent/' + agentId + '/growth');
  let html = '<div style="padding:20px;max-width:800px;min-width:560px">';
  html += '<h3 style="margin-bottom:4px"><span class="material-symbols-outlined" style="vertical-align:middle;color:rgb(251,146,60)">school</span> Role Growth Path</h3>';
  if (!data || !data.growth_path) {
    html += '<div style="color:var(--text3);font-size:13px;margin:16px 0">' + (data && data.message ? esc(data.message) : 'No growth path available.') + '</div>';
    html += '<button class="btn" onclick="initGrowthPath(\'' + agentId + '\')">Initialize Growth Path</button>';
  } else {
    var gp = data.growth_path;
    var summary = data.summary;
    html += '<div style="font-size:11px;color:var(--text3);margin-bottom:16px">' + esc(summary.role_name) + ' — ' + esc(summary.current_stage) + ' (' + summary.overall_progress + '% overall)</div>';
    html += '<div style="background:var(--surface2);border-radius:6px;height:8px;margin-bottom:20px;overflow:hidden">';
    html += '<div style="background:linear-gradient(90deg,rgb(251,146,60),rgb(234,88,12));height:100%;width:' + summary.overall_progress + '%;border-radius:6px;transition:width 0.3s"></div></div>';
    gp.stages.forEach(function(stage, idx) {
      var isCurrent = idx === gp.current_stage_idx;
      var borderColor = isCurrent ? 'rgba(251,146,60,0.5)' : 'var(--border)';
      var bgColor = isCurrent ? 'rgba(251,146,60,0.04)' : 'var(--surface)';
      html += '<div style="background:' + bgColor + ';border:1px solid ' + borderColor + ';border-radius:10px;padding:14px 16px;margin-bottom:12px">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">';
      html += '<div style="display:flex;align-items:center;gap:8px">';
      if (isCurrent) html += '<span style="font-size:12px;background:rgba(251,146,60,0.15);color:rgb(251,146,60);padding:2px 8px;border-radius:4px;font-weight:700">CURRENT</span>';
      html += '<span style="font-size:14px;font-weight:700">' + esc(stage.name) + '</span>';
      html += '</div>';
      html += '<span style="font-size:12px;color:var(--text3)">' + stage.completion_rate + '%</span>';
      html += '</div>';
      html += '<div style="background:var(--surface2);border-radius:4px;height:4px;margin-bottom:10px;overflow:hidden">';
      html += '<div style="background:rgb(251,146,60);height:100%;width:' + stage.completion_rate + '%;border-radius:4px"></div></div>';
      stage.objectives.forEach(function(obj) {
        var check = obj.completed ? '✅' : '⬜';
        html += '<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:4px;font-size:12px">';
        html += '<span style="flex-shrink:0">' + check + '</span>';
        html += '<span style="color:' + (obj.completed ? 'var(--text3)' : 'var(--text)') + '">' + esc(obj.title) + '</span>';
        if (!obj.completed && isCurrent) {
          html += '<button class="btn btn-sm" style="margin-left:auto;font-size:10px;padding:2px 8px" onclick="completeObjective(\'' + agentId + '\',\'' + obj.id + '\')">Complete</button>';
        }
        html += '</div>';
      });
      html += '</div>';
    });
    html += '<div style="display:flex;gap:10px;margin-top:16px">';
    html += '<button class="btn" onclick="triggerGrowthLearning(\'' + agentId + '\')"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">auto_stories</span> Trigger Learning</button>';
    html += '<button class="btn btn-ghost" onclick="closeModal()">Close</button>';
    html += '</div>';
  }
  html += '</div>';
  showModalHTML(html);
}
async function initGrowthPath(agentId) {
  await api('POST', '/api/portal/agent/' + agentId + '/growth', {action: 'init'});
  showGrowthPathPanel(agentId);
}
async function completeObjective(agentId, objectiveId) {
  const result = await api('POST', '/api/portal/agent/' + agentId + '/growth', {action: 'complete_objective', objective_id: objectiveId});
  if (result && result.advanced) { showToast('Advanced to next stage!', 'success'); }
  showGrowthPathPanel(agentId);
}
async function triggerGrowthLearning(agentId) {
  const result = await api('POST', '/api/portal/agent/' + agentId + '/growth', {action: 'trigger_learning'});
  if (result && result.ok && result.objective) {
    let html = '<div style="padding:20px;max-width:650px">';
    html += '<h3><span class="material-symbols-outlined" style="vertical-align:middle;color:rgb(251,146,60)">auto_stories</span> Learning Task</h3>';
    html += '<div style="font-size:13px;font-weight:600;margin:12px 0">' + esc(result.objective.title) + '</div>';
    html += '<div style="font-size:11px;color:var(--text2);margin-bottom:12px">' + esc(result.objective.description) + '</div>';
    html += '<pre style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11px;white-space:pre-wrap;max-height:300px;overflow-y:auto">' + esc(result.learning_prompt) + '</pre>';
    html += '<div style="margin-top:16px"><button class="btn" onclick="closeModal()">Close</button></div>';
    html += '</div>';
    showModalHTML(html);
  } else {
    showToast(result && result.message ? result.message : 'No pending objectives', 'info');
  }
}

// ============ Providers ============
function renderProviders(container) {
  var c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  var epoch = _renderEpoch;
  // Show header immediately
  c.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:28px"><div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:28px;font-weight:800;letter-spacing:-0.5px">LLM Providers</h2><p style="color:var(--text2);font-size:14px;margin-top:4px">Manage external compute endpoints and local model integrations.</p></div><button class="btn btn-primary" onclick="showModal(\'add-provider\')" style="flex-shrink:0"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle;margin-right:4px">add</span>Add Provider</button></div><div id="providers-grid" style="display:grid;grid-template-columns:repeat(2,1fr);gap:20px"><div style="color:var(--text3);padding:20px">Loading...</div></div>';
  api('GET', '/api/portal/providers').then(function(data) {
    if (!data || epoch !== _renderEpoch) return;
    providers = data.providers || [];
    var grid = document.getElementById('providers-grid');
    if (!grid) return;
    var kindIcon = function(k) { return k==='ollama'?'terminal':k==='claude'?'smart_toy':'cloud'; };
    var kindLabel = function(k) { return k==='ollama'?'Local':k==='claude'?'Anthropic':'External'; };
    grid.innerHTML = providers.map(function(p) { return '<div style="background:var(--surface);border-radius:14px;padding:24px;border:1px solid var(--border-light);transition:all 0.2s" onmouseenter="this.style.background=\'var(--surface2)\'" onmouseleave="this.style.background=\'var(--surface)\'">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px">' +
        '<div style="display:flex;align-items:center;gap:14px">' +
          '<div style="width:48px;height:48px;background:var(--surface3);border-radius:10px;display:flex;align-items:center;justify-content:center"><span class="material-symbols-outlined" style="font-size:28px;color:var(--primary)">' + kindIcon(p.kind) + '</span></div>' +
          '<div>' +
            '<div style="display:flex;align-items:center;gap:8px"><span style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:16px;font-weight:700">' + esc(p.name) + '</span><span style="font-size:10px;background:rgba(203,201,255,0.1);color:var(--primary);padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:0.5px;font-weight:700">' + kindLabel(p.kind) + '</span></div>' +
            '<div style="font-size:12px;color:var(--text3);margin-top:2px">' + esc(p.base_url) + '</div>' +
          '</div>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;color:' + (p.enabled ? 'var(--success)' : 'var(--error)') + '"><span style="width:6px;height:6px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor;display:inline-block"></span>' + (p.enabled ? 'ENABLED' : 'DISABLED') + '</div>' +
      '</div>' +
      '<div style="margin-bottom:20px">' +
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700;margin-bottom:8px">Models (' + (p.manual_models||p.models_cache||[]).length + ')</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
          (((p.manual_models||p.models_cache||[]).length > 0) ? (p.manual_models||p.models_cache||[]).map(function(m) { return '<span style="background:var(--surface3);padding:5px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px">' + esc(m) + ' <span class="material-symbols-outlined" style="font-size:14px;color:var(--success)">check_circle</span></span>'; }).join('') : '<span style="font-size:12px;color:var(--text3)">No models configured — click Edit to add models</span>') +
        '</div>' +
      '</div>' +
      '<div style="margin-bottom:20px">' +
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700;margin-bottom:8px">Concurrency &amp; Scheduling</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:8px">' +
          '<span style="background:var(--surface3);padding:4px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">speed</span>Max ' + (p.max_concurrent||1) + ' concurrent</span>' +
          '<span style="background:var(--surface3);padding:4px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">schedule</span>' + esc(p.schedule_strategy||'serial') + '</span>' +
          (p.rate_limit_rpm > 0 ? '<span style="background:var(--surface3);padding:4px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">timer</span>' + p.rate_limit_rpm + ' RPM</span>' : '') +
          '<span style="background:var(--surface3);padding:4px 10px;border-radius:6px;font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">' + (p.scope==='cloud'?'cloud':'computer') + '</span>' + esc(p.scope||'local') + '</span>' +
        '</div>' +
        (Object.keys(p.model_concurrency||{}).length > 0 ? '<div style="margin-top:6px;font-size:10px;color:var(--text3)">Per-model: ' + Object.entries(p.model_concurrency||{}).map(function(e) { return e[0] + '=' + e[1]; }).join(', ') + '</div>' : '') +
      '</div>' +
      (p.api_key ? '<div style="margin-bottom:20px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);font-weight:700">API Key</span><span style="font-size:10px;color:var(--text3)">Masked</span></div><div style="background:var(--bg);border:1px solid var(--border-light);border-radius:6px;padding:10px 14px;display:flex;align-items:center;gap:10px"><span class="material-symbols-outlined" style="font-size:16px;color:var(--text3)">key</span><span style="font-size:12px;color:var(--text3);font-family:monospace;letter-spacing:1px">&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;</span></div></div>' : '') +
      '<div style="display:flex;align-items:center;justify-content:space-between;padding-top:16px;border-top:1px solid var(--border-light)">' +
        '<div style="display:flex;gap:12px">' +
          '<button style="background:none;border:none;color:var(--text2);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0" onclick="editProvider(\'' + esc(p.id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">edit</span> Edit</button>' +
        '</div>' +
        '<button style="background:none;border:none;color:var(--error);font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:4px;padding:0;opacity:0.7" onclick="deleteProvider(\'' + esc(p.id) + '\')"><span class="material-symbols-outlined" style="font-size:16px">delete</span> Delete</button>' +
      '</div>' +
    '</div>'; }).join('') +
    (providers.length === 0 ? '<div style="color:var(--text3);padding:20px;grid-column:1/-1">No providers configured yet. Click "+ Add Provider" to get started.</div>' : '');
  });
}

// ---- Model tag chip helpers ----
function addModelTag(prefix) {
  var input = document.getElementById(prefix + '-model-input');
  var val = (input.value || '').trim();
  if (!val) return;
  var container = document.getElementById(prefix + '-model-tags');
  // Prevent duplicates
  var existing = container.querySelectorAll('.model-tag');
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].dataset.model === val) { input.value = ''; return; }
  }
  var tag = document.createElement('span');
  tag.className = 'model-tag';
  tag.dataset.model = val;
  tag.style.cssText = 'display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;font-size:12px;color:var(--text);font-family:monospace';
  tag.innerHTML = esc(val) + '<span onclick="this.parentElement.remove()" style="cursor:pointer;color:var(--text3);font-size:14px;line-height:1;margin-left:2px">&times;</span>';
  container.appendChild(tag);
  input.value = '';
  input.focus();
}

function getModelTags(prefix) {
  var container = document.getElementById(prefix + '-model-tags');
  var tags = container.querySelectorAll('.model-tag');
  var models = [];
  for (var i = 0; i < tags.length; i++) models.push(tags[i].dataset.model);
  return models;
}

function setModelTags(prefix, models) {
  var container = document.getElementById(prefix + '-model-tags');
  container.innerHTML = '';
  (models || []).forEach(function(m) {
    if (!m) return;
    var tag = document.createElement('span');
    tag.className = 'model-tag';
    tag.dataset.model = m;
    tag.style.cssText = 'display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;font-size:12px;color:var(--text);font-family:monospace';
    tag.innerHTML = esc(m) + '<span onclick="this.parentElement.remove()" style="cursor:pointer;color:var(--text3);font-size:14px;line-height:1;margin-left:2px">&times;</span>';
    container.appendChild(tag);
  });
}

async function addProvider() {
  const name = document.getElementById('ap-name').value.trim();
  const kind = document.getElementById('ap-kind').value;
  const base_url = document.getElementById('ap-url').value.trim();
  const api_key = document.getElementById('ap-key').value.trim();
  if (!name || !base_url) { alert('Name and Base URL are required'); return; }
  const manual_models = getModelTags('ap');
  const max_concurrent = parseInt(document.getElementById('ap-max-concurrent').value) || 1;
  const rate_limit_rpm = parseInt(document.getElementById('ap-rate-limit-rpm').value) || 0;
  const schedule_strategy = document.getElementById('ap-schedule-strategy').value;
  const scope = document.getElementById('ap-scope').value;
  await api('POST', '/api/portal/providers', { name, kind, base_url, api_key, manual_models, max_concurrent, rate_limit_rpm, schedule_strategy, scope });
  hideModal('add-provider');
  document.getElementById('ap-name').value = '';
  document.getElementById('ap-url').value = '';
  document.getElementById('ap-key').value = '';
  document.getElementById('ap-max-concurrent').value = '1';
  document.getElementById('ap-rate-limit-rpm').value = '0';
  document.getElementById('ap-schedule-strategy').value = 'serial';
  document.getElementById('ap-scope').value = 'local';
  setModelTags('ap', []);
  loadAvailableModels();
  renderProviders();
}

function editProvider(id) {
  const p = providers.find(x => x.id === id);
  if (!p) return;
  document.getElementById('ep-name').value = p.name;
  document.getElementById('ep-kind').value = p.kind;
  document.getElementById('ep-url').value = p.base_url;
  document.getElementById('ep-key').value = p.api_key || '';
  document.getElementById('ep-enabled').value = String(p.enabled);
  // Concurrency fields
  document.getElementById('ep-max-concurrent').value = p.max_concurrent || 1;
  document.getElementById('ep-rate-limit-rpm').value = p.rate_limit_rpm || 0;
  document.getElementById('ep-schedule-strategy').value = p.schedule_strategy || 'serial';
  document.getElementById('ep-scope').value = p.scope || 'local';
  document.getElementById('ep-context-length').value = p.context_length || 0;
  // Per-model concurrency overrides
  var mcDiv = document.getElementById('ep-model-concurrency');
  var models = p.manual_models || p.models_cache || [];
  var mc = p.model_concurrency || {};
  mcDiv.innerHTML = models.length === 0 ? '<div style="font-size:11px;color:var(--text3)">Add models first to set per-model concurrency.</div>' :
    models.map(function(m) {
      return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
        '<span style="font-size:11px;color:var(--text);font-family:monospace;min-width:120px">' + esc(m) + '</span>' +
        '<input type="number" min="0" max="100" value="' + (mc[m] || 0) + '" data-mc-model="' + esc(m) + '" style="width:60px;font-size:11px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text)">' +
        '<span style="font-size:10px;color:var(--text3)">(0=use platform default)</span></div>';
    }).join('');
  // Show manual models as tag chips
  setModelTags('ep', models);
  window._editProviderId = id;
  showModal('edit-provider');
}

async function saveProvider() {
  const id = window._editProviderId;
  if (!id) return;
  const body = {
    name: document.getElementById('ep-name').value.trim(),
    kind: document.getElementById('ep-kind').value,
    base_url: document.getElementById('ep-url').value.trim(),
    api_key: document.getElementById('ep-key').value.trim(),
    enabled: document.getElementById('ep-enabled').value === 'true',
    max_concurrent: parseInt(document.getElementById('ep-max-concurrent').value) || 1,
    rate_limit_rpm: parseInt(document.getElementById('ep-rate-limit-rpm').value) || 0,
    schedule_strategy: document.getElementById('ep-schedule-strategy').value,
    scope: document.getElementById('ep-scope').value,
    context_length: parseInt(document.getElementById('ep-context-length').value) || 0,
  };
  // Collect per-model concurrency overrides
  var mcInputs = document.querySelectorAll('[data-mc-model]');
  var model_concurrency = {};
  mcInputs.forEach(function(inp) {
    var v = parseInt(inp.value) || 0;
    if (v > 0) model_concurrency[inp.dataset.mcModel] = v;
  });
  body.model_concurrency = model_concurrency;
  body.manual_models = getModelTags('ep');
  await api('POST', `/api/portal/providers/${id}/update`, body);
  hideModal('edit-provider');
  loadAvailableModels();
  renderProviders();
}

async function deleteProvider(id) {
  if (!confirm('Delete this provider?')) return;
  await api('DELETE', `/api/portal/providers/${id}`);
  loadAvailableModels();
  renderProviders();
}

async function detectModels(id) {
  const btn = event.target;
  btn.textContent = 'Detecting...';
  btn.disabled = true;
  const data = await api('POST', `/api/portal/providers/${id}/detect`);
  btn.textContent = 'Detect Models';
  btn.disabled = false;
  if (data && data.models) {
    alert(`Found ${data.models.length} model(s): ${data.models.join(', ') || '(none)'}`);
  }
  loadAvailableModels();
  renderProviders();
}

async function detectAllModels() {
  const data = await api('POST', '/api/portal/providers/detect-all');
  if (data && data.models) {
    let total = 0;
    for (const k of Object.keys(data.models)) total += data.models[k].length;
    alert(`Detected ${total} model(s) across ${Object.keys(data.models).length} provider(s)`);
  }
  loadAvailableModels();
  if (currentView === 'providers') renderProviders();
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
    _defaultProvider = cfg.provider || 'ollama';
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
    'ea-learning-provider', 'ea-multimodal-provider', 'ea-coding-provider'
  ];
  selIds.forEach(selId => {
    const sel = document.getElementById(selId);
    if (!sel) return;
    const curVal = sel.value;
    var blankLabel = (selId === 'ea-learning-provider' || selId === 'ea-multimodal-provider' || selId === 'ea-coding-provider')
      ? '（使用默认）'
      : 'Default (global)';
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
  const provider = providerEl.value || _defaultProvider;
  const models = _availableModels[provider] || [];
  modelEl.innerHTML = '<option value="">Default (global)</option>';
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
var _CLOUD_PROVIDER_KINDS = ['openai', 'claude', 'anthropic'];

function _updateMmToolsHint() {
  var hintEl = document.getElementById('ea-mm-tools-hint');
  var cb = document.getElementById('ea-mm-supports-tools');
  var provEl = document.getElementById('ea-multimodal-provider');
  if (!hintEl || !cb || !provEl) return;

  var provId = provEl.value || '';
  if (!provId) { hintEl.style.display = 'none'; return; }

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
loadAvailableModels();

// ============ Agent management ============
function populateNodeSelect() {
  const sel = document.getElementById('ca-node');
  if (!sel) return;
  sel.innerHTML = '<option value="local">Local (this machine)</option>';
  nodes.filter(n => !n.is_self && n.status === 'online').forEach(n => {
    const opt = document.createElement('option');
    opt.value = n.node_id;
    opt.textContent = `${n.name} (${n.url})`;
    sel.appendChild(opt);
  });
}

// ============ Skill picker (used in Create Agent dialog) ============
window._caSelectedSkills = window._caSelectedSkills || [];
window._caSkillCatalog = null;

const CA_SKILL_CATEGORY_LABELS = {
  'builtin': '🏆 内置经典', 'marketing': '📣 市场营销', 'engineering': '💻 工程研发',
  'sales': '🤝 销售', 'design': '🎨 设计', 'product': '📋 产品', 'strategy': '🧭 战略',
  'support': '🎧 客户支持', 'testing': '🧪 测试', 'academic': '🎓 学术研究',
  'finance': '💰 财务', 'legal': '⚖️ 法务', 'hr': '👥 人力', 'paid-media': '📊 投放',
  'project-management': '📅 项目管理', 'specialized': '✨ 特殊场景',
  'spatial-computing': '🥽 空间计算', 'game-development': '🎮 游戏', 'supply-chain': '🏭 供应链',
  'community': '📦 社区'
};

async function openCreateAgentSkillPicker() {
  if (!window._caSkillCatalog) {
    const res = await api('GET', '/api/portal/enhancement-presets');
    window._caSkillCatalog = (res && res.presets) || [];
  }
  renderSkillPickerModal();
}

function renderSkillPickerModal(filter) {
  filter = (filter||'').trim().toLowerCase();
  const all = window._caSkillCatalog || [];
  // Group by category
  const groups = {};
  all.forEach(function(p) {
    const cat = p.category || (p.source ? p.source : 'builtin');
    const catKey = (cat === 'agency-agents-zh') ? 'community' : cat;
    if (!groups[catKey]) groups[catKey] = [];
    if (filter) {
      const hay = ((p.name||'') + ' ' + (p.description||'') + ' ' + (p.id||'')).toLowerCase();
      if (hay.indexOf(filter) < 0) return;
    }
    groups[catKey].push(p);
  });
  // Order categories
  const builtin_cats = ['builtin'];
  const sorted_cats = Object.keys(groups).sort(function(a,b){
    if (a==='builtin') return -1;
    if (b==='builtin') return 1;
    return (CA_SKILL_CATEGORY_LABELS[a]||a).localeCompare(CA_SKILL_CATEGORY_LABELS[b]||b);
  });
  const selected = window._caSelectedSkills || [];

  let html = '<div style="padding:18px;max-height:80vh;display:flex;flex-direction:column;min-width:680px">';
  html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">';
  html += '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;margin:0">选择专业领域 <span style="font-size:12px;color:var(--text3);font-weight:400">(最多 8 个，已选 <span id="sp-count">' + selected.length + '</span>/8)</span></h3>';
  html += '<input id="sp-search" type="text" placeholder="🔍 搜索专业领域..." style="padding:6px 10px;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;width:240px" oninput="renderSkillPickerModal(this.value)" value="' + esc(filter) + '">';
  html += '</div>';
  html += '<div style="overflow-y:auto;flex:1;max-height:60vh;padding-right:4px">';

  let total = 0;
  sorted_cats.forEach(function(cat) {
    const items = groups[cat] || [];
    if (!items.length) return;
    total += items.length;
    const label = CA_SKILL_CATEGORY_LABELS[cat] || cat;
    html += '<div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin:12px 0 6px">' + esc(label) + ' <span style="color:var(--text3);font-weight:400;text-transform:none">(' + items.length + ')</span></div>';
    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">';
    items.forEach(function(p) {
      const isSelected = selected.indexOf(p.id) >= 0;
      html += '<div data-skill-id="' + esc(p.id) + '" onclick="toggleCaSkill(\'' + esc(p.id) + '\',this)" ' +
        'style="background:' + (isSelected?'rgba(203,201,255,0.12)':'var(--surface3)') + ';border-radius:8px;padding:10px;cursor:pointer;border:1px solid ' + (isSelected?'var(--primary)':'var(--border-light)') + ';transition:all 0.15s">' +
        '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">' +
        '<span style="font-size:15px">' + (p.icon||'📦') + '</span>' +
        '<div style="font-weight:600;font-size:12px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(p.name) + '</div>' +
        '</div>' +
        '<div style="font-size:10px;color:var(--text3);line-height:1.4;max-height:28px;overflow:hidden">' + esc(p.description||'') + '</div>' +
      '</div>';
    });
    html += '</div>';
  });
  if (total === 0) {
    html += '<div style="text-align:center;color:var(--text3);padding:40px 0">未找到匹配的技能</div>';
  }
  html += '</div>';
  html += '<div style="display:flex;justify-content:flex-end;gap:8px;padding-top:14px;border-top:1px solid var(--border-light);margin-top:12px">';
  html += '<button class="btn btn-sm" onclick="closeModal()">取消</button>';
  html += '<button class="btn btn-primary btn-sm" onclick="confirmCaSkillSelection()">确认 (<span id="sp-btn-count">' + selected.length + '</span>)</button>';
  html += '</div>';
  html += '</div>';
  showModalHTML(html);
}

function toggleCaSkill(presetId, el) {
  const list = window._caSelectedSkills || [];
  const idx = list.indexOf(presetId);
  if (idx >= 0) {
    list.splice(idx, 1);
    el.style.borderColor = 'var(--border-light)';
    el.style.background = 'var(--surface3)';
  } else {
    if (list.length >= 8) { alert('最多只能选择 8 个技能'); return; }
    list.push(presetId);
    el.style.borderColor = 'var(--primary)';
    el.style.background = 'rgba(203,201,255,0.12)';
  }
  window._caSelectedSkills = list;
  const c1 = document.getElementById('sp-count');
  const c2 = document.getElementById('sp-btn-count');
  if (c1) c1.textContent = list.length;
  if (c2) c2.textContent = list.length;
}

function confirmCaSkillSelection() {
  const list = window._caSelectedSkills || [];
  const chips = document.getElementById('ca-skills-chips');
  if (chips) {
    if (!list.length) {
      chips.innerHTML = '未选择任何专业领域';
      chips.style.color = 'var(--text3)';
    } else {
      const catalog = window._caSkillCatalog || [];
      const byId = {};
      catalog.forEach(function(p){ byId[p.id] = p; });
      chips.style.color = 'var(--text)';
      chips.innerHTML = list.map(function(id) {
        const p = byId[id] || {name:id, icon:'📦'};
        return '<span style="background:var(--primary);color:#0e141b;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:600;display:inline-flex;align-items:center;gap:4px">' +
          (p.icon||'📦') + ' ' + esc(p.name) +
          '<span onclick="event.stopPropagation();removeCaSkill(\'' + esc(id) + '\')" style="cursor:pointer;margin-left:2px">✕</span></span>';
      }).join('');
    }
  }
  closeModal();
}

function removeCaSkill(id) {
  const list = window._caSelectedSkills || [];
  const idx = list.indexOf(id);
  if (idx >= 0) list.splice(idx, 1);
  window._caSelectedSkills = list;
  confirmCaSkillSelection();
}

// ── Tag (Expertise + Skills) management ──────────────────
window._caTags = [];
window._eaTags = [];

function _renderTags(tags, containerId, removeFunc) {
  var c = document.getElementById(containerId);
  if (!c) return;
  if (!tags.length) { c.innerHTML = '<span style="color:var(--text3);font-size:12px">暂无标签</span>'; return; }
  c.innerHTML = tags.map(function(t, i) {
    return '<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 10px;background:var(--primary);color:#fff;border-radius:12px;font-size:12px;white-space:nowrap">'
      + t + ' <span onclick="'+removeFunc+'('+i+')" style="cursor:pointer;font-size:14px;line-height:1;opacity:0.8">&times;</span></span>';
  }).join('');
}

function caAddTags(input) {
  var val = input.value.trim();
  if (!val) return;
  val.split(',').forEach(function(s) {
    s = s.trim();
    if (s && window._caTags.indexOf(s) < 0) window._caTags.push(s);
  });
  input.value = '';
  _renderTags(window._caTags, 'ca-tags-display', 'caRemoveTag');
}
function caRemoveTag(i) { window._caTags.splice(i, 1); _renderTags(window._caTags, 'ca-tags-display', 'caRemoveTag'); }

function eaAddTags(input) {
  var val = input.value.trim();
  if (!val) return;
  val.split(',').forEach(function(s) {
    s = s.trim();
    if (s && window._eaTags.indexOf(s) < 0) window._eaTags.push(s);
  });
  input.value = '';
  _renderTags(window._eaTags, 'ea-tags-display', 'eaRemoveTag');
}
function eaRemoveTag(i) { window._eaTags.splice(i, 1); _renderTags(window._eaTags, 'ea-tags-display', 'eaRemoveTag'); }

// ──────────────── Multi-LLM slot helpers (Edit Agent) ────────────────
function eaToggleLlmPanel() {
  var panel = document.getElementById('ea-llm-panel');
  var icon = document.getElementById('ea-llm-toggle-label');
  if (!panel) return;
  var willShow = panel.style.display === 'none';
  panel.style.display = willShow ? 'block' : 'none';
  if (icon) icon.textContent = willShow ? 'expand_less' : 'expand_more';
}

function eaAddExtraLlmRow() {
  if (!Array.isArray(window._eaExtraLlms)) window._eaExtraLlms = [];
  window._eaExtraLlms.push({label:'', provider:'', model:'', purpose:'', note:''});
  eaRenderExtraLlms();
}

function eaRemoveExtraLlm(idx) {
  if (!Array.isArray(window._eaExtraLlms)) return;
  window._eaExtraLlms.splice(idx, 1);
  eaRenderExtraLlms();
}

// In-place mutate (so in-flight typing isn't blown away by re-render)
function eaExtraLlmSet(idx, field, value) {
  if (!Array.isArray(window._eaExtraLlms)) return;
  if (!window._eaExtraLlms[idx]) return;
  window._eaExtraLlms[idx][field] = value;
  // provider change needs model dropdown refresh
  if (field === 'provider') {
    var modelSelId = 'ea-extra-llm-model-' + idx;
    var provSelId = 'ea-extra-llm-provider-' + idx;
    // temporarily stash value so updateModelSelect can honor it
    window._eaExtraLlms[idx].model = '';
    updateModelSelect(provSelId, modelSelId, '');
  }
}

function eaRenderExtraLlms() {
  var host = document.getElementById('ea-extra-llms-rows');
  if (!host) return;
  var slots = window._eaExtraLlms || [];
  if (!slots.length) {
    host.innerHTML = '<div style="font-size:11px;color:var(--text3);padding:6px 0">（还没有 slot — 点击右上角「添加」来配置第一个）</div>';
    return;
  }
  // Build rows: [label | purpose | provider | model | note | ×]
  var providerOptions = '<option value="">（使用默认）</option>' +
    (_providerList || []).map(function(p){
      return '<option value="'+p.id+'">'+p.name+'</option>';
    }).join('');
  var html = '';
  slots.forEach(function(s, i){
    var esc = function(v){ return String(v==null?'':v).replace(/"/g,'&quot;'); };
    html += '<div style="display:grid;grid-template-columns:110px 110px 110px 140px 1fr 28px;gap:6px;margin-bottom:6px;align-items:center">' +
      '<input placeholder="label*" value="'+esc(s.label)+'" onchange="eaExtraLlmSet('+i+',\'label\',this.value)" style="font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px">' +
      '<input placeholder="purpose" value="'+esc(s.purpose)+'" onchange="eaExtraLlmSet('+i+',\'purpose\',this.value)" style="font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px">' +
      '<select id="ea-extra-llm-provider-'+i+'" onchange="eaExtraLlmSet('+i+',\'provider\',this.value)" style="font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px">'+providerOptions+'</select>' +
      '<select id="ea-extra-llm-model-'+i+'" onchange="eaExtraLlmSet('+i+',\'model\',this.value)" style="font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px"><option value="">（使用默认）</option></select>' +
      '<input placeholder="note（可选备注）" value="'+esc(s.note)+'" onchange="eaExtraLlmSet('+i+',\'note\',this.value)" style="font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px">' +
      '<button type="button" class="btn btn-ghost btn-sm" onclick="eaRemoveExtraLlm('+i+')" title="删除" style="padding:2px 4px"><span class="material-symbols-outlined" style="font-size:14px">close</span></button>' +
    '</div>';
  });
  host.innerHTML = html;
  // After insertion, set provider/model dropdown values + populate models
  slots.forEach(function(s, i){
    var provSel = document.getElementById('ea-extra-llm-provider-'+i);
    if (provSel) provSel.value = s.provider || '';
    updateModelSelect('ea-extra-llm-provider-'+i, 'ea-extra-llm-model-'+i, s.model || '');
  });
}

// ── Robot Avatar picker ──────────────────────────────────
var _AVATAR_ROLES = ['ceo','cto','coder','reviewer','researcher','architect','pm','designer','tester','devops','data','general'];
window._caSelectedAvatar = '';
window._eaSelectedAvatar = '';

function _renderAvatarGrid(containerId, selectedVar, clickFunc) {
  var c = document.getElementById(containerId);
  if (!c) return;
  c.innerHTML = _AVATAR_ROLES.map(function(r) {
    var id = 'robot_' + r;
    var sel = (window[selectedVar] === id) ? 'border:2px solid var(--primary);box-shadow:0 0 6px var(--primary)' : 'border:2px solid transparent';
    return '<div onclick="'+clickFunc+'(\''+id+'\')" style="cursor:pointer;border-radius:8px;padding:4px;text-align:center;'+sel+';transition:all .15s">'
      + '<img src="/static/robots/'+id+'.svg" style="width:40px;height:40px" onerror="this.outerHTML=\'<span class=material-symbols-outlined style=font-size:36px;color:var(--text3)>smart_toy</span>\'">'
      + '<div style="font-size:10px;color:var(--text2);margin-top:2px">'+r+'</div></div>';
  }).join('');
}

function caPickAvatar(id) {
  window._caSelectedAvatar = (window._caSelectedAvatar === id) ? '' : id;
  _renderAvatarGrid('ca-avatar-grid', '_caSelectedAvatar', 'caPickAvatar');
}
function eaPickAvatar(id) {
  window._eaSelectedAvatar = (window._eaSelectedAvatar === id) ? '' : id;
  _renderAvatarGrid('ea-avatar-grid', '_eaSelectedAvatar', 'eaPickAvatar');
}

// Persona template library — loaded once, reused on open
window._caPersonaCache = null;
async function loadPersonasForCreate() {
  const sel = document.getElementById('ca-persona');
  if (!sel || sel.options.length > 1) return; // already loaded
  try {
    const data = await api('GET', '/api/portal/personas');
    const list = (data && data.personas) || [];
    window._caPersonaCache = {};
    list.forEach(function(p){
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = (p.avatar_emoji||'👤') + ' ' + (p.name_cn||p.name_en||p.id) +
                      (p.role?' · '+p.role:'');
      sel.appendChild(o);
      window._caPersonaCache[p.id] = p;
    });
  } catch(e) { console.warn('load personas failed', e); }
  // Init avatar grid and clear tags
  window._caTags = [];
  _renderTags(window._caTags, 'ca-tags-display', 'caRemoveTag');
  window._caSelectedAvatar = '';
  _renderAvatarGrid('ca-avatar-grid', '_caSelectedAvatar', 'caPickAvatar');
}
async function caUpdateExpCount() {
  var role = document.getElementById('ca-role').value;
  var infoEl = document.getElementById('ca-exp-info');
  if (!infoEl) return;
  var siCheck = document.getElementById('ca-self-improve');
  var impCheck = document.getElementById('ca-import-exp');
  if (!siCheck.checked) { infoEl.textContent = 'Self-improvement disabled'; return; }
  if (!impCheck.checked) { infoEl.textContent = 'Experience import disabled'; return; }
  try {
    var data = await api('GET', '/api/portal/experience/stats');
    var roleInfo = (data && data.roles && data.roles[role]) ? data.roles[role] : null;
    if (roleInfo && roleInfo.total > 0) {
      infoEl.innerHTML = '📚 <b>'+roleInfo.total+'</b> experiences available for role <b>'+role+'</b> ('+roleInfo.core_count+' core). Will import up to 50.';
    } else {
      infoEl.textContent = 'No existing experiences for role: '+role+'. New library will be created.';
    }
  } catch(e) { infoEl.textContent = 'Experience library ready.'; }
}

function applyPersonaPreview() {
  const id = document.getElementById('ca-persona').value;
  const prev = document.getElementById('ca-persona-preview');
  if (!id) { prev.textContent = ''; return; }
  const p = (window._caPersonaCache||{})[id];
  if (!p) { prev.textContent = ''; return; }
  prev.textContent = (p.tagline||'') + (p.role?' · role='+p.role:'');
  // Auto-fill name if empty
  const nameEl = document.getElementById('ca-name');
  if (!nameEl.value.trim()) nameEl.value = p.name_cn || p.name_en || '';
}
function memoryToCount(pref) {
  return {short:20, medium:50, long:100, xlong:200}[pref] || 50;
}

async function createAgent() {
  const agentClass = document.getElementById('ca-agent-class').value;
  const memoryMode = document.getElementById('ca-memory-mode').value;
  const ragMode = document.getElementById('ca-rag-mode').value;
  const ragProviderId = document.getElementById('ca-rag-provider').value;
  const name = document.getElementById('ca-name').value.trim();
  const role = document.getElementById('ca-role').value;
  const priorityLevel = parseInt(document.getElementById('ca-priority-level').value) || 3;
  const roleTitle = document.getElementById('ca-role-title').value.trim();
  const personaId = document.getElementById('ca-persona').value;
  const personality = document.getElementById('ca-personality').value;
  const style = document.getElementById('ca-style').value;
  const allTags = (window._caTags || []).slice();
  const language = document.getElementById('ca-language').value;
  const memPref = document.getElementById('ca-memory').value;
  const temperature = parseFloat(document.getElementById('ca-temperature').value);
  const execPolicy = document.getElementById('ca-exec-policy').value;
  const model = document.getElementById('ca-model').value.trim();
  const provider = document.getElementById('ca-provider').value;
  const node_id = document.getElementById('ca-node').value;
  const workdir = document.getElementById('ca-workdir').value.trim();
  const prompt = document.getElementById('ca-prompt').value.trim();

  if(!name) { alert('Please enter agent name'); return; }

  try {
    const data = await api('POST', '/api/portal/agent/create', {
      name: name, role: role, model: model, provider: provider, node_id: node_id,
      working_dir: workdir,
      // Note: do NOT send `system_prompt` here — that would fully override
      // the persona's curated prompt. The textarea below is "Custom
      // Instructions" which gets appended via profile.custom_instructions.
      priority_level: priorityLevel, role_title: roleTitle,
      persona_id: personaId || '',
      robot_avatar: window._caSelectedAvatar || ('robot_' + role),
      profile: {
        agent_class: agentClass, memory_mode: memoryMode,
        rag_mode: ragMode, rag_provider_id: ragProviderId,
        personality: personality, communication_style: style,
        expertise: allTags,
        skills: allTags,
        language: language,
        custom_instructions: prompt,
        max_context_messages: memoryToCount(memPref),
        temperature: temperature,
        exec_policy: execPolicy
      }
    });

    if (data && data.id) {
      // Apply selected skills (enhancement presets) if any
      const selectedSkills = (window._caSelectedSkills || []).slice(0, 5);
      if (selectedSkills.length) {
        try {
          await api('POST', '/api/portal/agent/' + data.id + '/enhancement',
                    { action:'enable', domains: selectedSkills });
        } catch(err) { console.error('enable skills error:', err); }
      }
      // Enable self-improvement if checked
      const siEnabled = document.getElementById('ca-self-improve').checked;
      const siImport = document.getElementById('ca-import-exp').checked;
      if (siEnabled) {
        try {
          await api('POST', '/api/portal/agent/' + data.id + '/self-improvement/enable', {
            import_experience: siImport, import_limit: 50
          });
        } catch(err) { console.error('enable self-improvement error:', err); }
      }
      // Bind domain knowledge base if selected (for advisor/private mode)
      if (ragMode === 'private' || ragMode === 'both') {
        var domainKbId = (document.getElementById('ca-rag-domain-kb')||{}).value || '';
        if (domainKbId) {
          try {
            // Store the domain KB binding in agent's rag_collection_ids
            await api('POST', '/api/portal/agent/' + data.id + '/update', {
              profile: { rag_collection_ids: [domainKbId] }
            });
          } catch(err) { console.error('bind domain KB error:', err); }
        }
      }
      window._caSelectedSkills = [];
      hideModal('create-agent');
      document.getElementById('ca-name').value = '';
      document.getElementById('ca-priority-level').value = '3';
      document.getElementById('ca-role-title').value = '';
      document.getElementById('ca-persona').value = '';
      document.getElementById('ca-persona-preview').textContent = '';
      document.getElementById('ca-personality').value = 'helpful';
      document.getElementById('ca-style').value = 'technical';
      document.getElementById('ca-expertise').value = '';
      window._caTags = [];
      _renderTags(window._caTags, 'ca-tags-display', 'caRemoveTag');
      window._caSelectedAvatar = '';
      _renderAvatarGrid('ca-avatar-grid', '_caSelectedAvatar', 'caPickAvatar');
      document.getElementById('ca-agent-class').value = 'enterprise';
      document.getElementById('ca-memory-mode').value = 'full';
      document.getElementById('ca-rag-mode').value = 'shared';
      document.getElementById('ca-rag-provider').value = '';
      var dkbSel = document.getElementById('ca-rag-domain-kb');
      if (dkbSel) dkbSel.value = '';
      _onRagModeChange('shared');
      document.getElementById('ca-memory').value = 'medium';
      document.getElementById('ca-mem-label').textContent = '(50 条)';
      document.getElementById('ca-temperature').value = '0.7';
      document.getElementById('ca-temp-label').textContent = '(0.7 平衡)';
      document.getElementById('ca-exec-policy').value = 'ask';
      document.getElementById('ca-model').value = '';
      document.getElementById('ca-prompt').value = '';
      document.getElementById('ca-workdir').value = '';
      const chips = document.getElementById('ca-skills-chips');
      if (chips) chips.innerHTML = '未选择任何专业领域';
      await refresh();
      showAgentView(data.id, null);
    } else if (data && data.error) {
      alert('Failed to create agent: ' + data.error);
    } else {
      alert('Failed to create agent. Check console for details.');
    }
  } catch(e) {
    console.error('createAgent error:', e);
    alert('Error creating agent: ' + e.message);
  }
}

// ============ Tasks ============
function toggleTasks(agentId) {
  const panel = document.getElementById('tasks-panel-' + agentId);
  if (!panel) return;
  if (panel.classList.contains('hidden')) {
    panel.classList.remove('hidden');
    loadTasks(agentId);
  } else {
    panel.classList.add('hidden');
  }
}

async function loadAgentRuntimeStats(agentId) {
  // 拉取 token 用量 + 记忆比例，更新 agent 详情页的两个 stat 卡。
  try {
    var data = await api('GET', '/api/portal/agent/' + agentId + '/runtime-stats');
    if (!data) return;
    var tok = data.tokens || {};
    var mem = data.memory || {};
    var tEl = document.getElementById('agent-token-stats-' + agentId);
    var cEl = document.getElementById('agent-token-calls-' + agentId);
    if (tEl) {
      var fmt = function(n){ if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1e3) return (n/1e3).toFixed(1)+'k'; return String(n||0); };
      tEl.textContent = fmt(tok.prompt_tokens) + ' / ' + fmt(tok.completion_tokens);
    }
    if (cEl) cEl.textContent = (tok.calls||0) + ' calls';
    var mEl = document.getElementById('agent-memory-ratio-' + agentId);
    var bEl = document.getElementById('agent-memory-bar-' + agentId);
    if (mEl) {
      var pct = Math.round((mem.last_ratio || 0) * 100);
      var ema = Math.round((mem.ema_ratio || 0) * 100);
      var hr = Math.round((mem.hit_rate || 0) * 100);
      var hits = mem.hits || 0;
      var misses = mem.misses || 0;
      mEl.textContent = pct + '% · 命中率 ' + hr + '% (' + hits + '/' + (hits+misses) + ')';
    }
    if (bEl) {
      var w = Math.min(100, Math.round((mem.last_ratio || 0) * 100));
      bEl.style.width = w + '%';
    }
  } catch(e) { /* silent */ }
}

async function wakeAgent(agentId) {
  // 唤醒 agent：扫描所有项目里分配给它且未完成的任务，逐个 spawn 后台执行。
  try {
    var data = await api('POST', '/api/portal/agent/' + agentId + '/wake', {max_tasks: 5});
    if (!data || !data.ok) {
      alert('唤醒失败: ' + ((data&&data.error)||'unknown'));
      return;
    }
    var triggered = data.triggered || [];
    var skipped = data.skipped_paused || [];
    var msg = '已唤醒，触发 ' + triggered.length + ' 个任务';
    if (triggered.length === 0) {
      msg = '没有待处理任务';
      if (skipped.length) msg += '（' + skipped.length + ' 个项目被暂停，已跳过）';
    } else {
      msg += '\n\n' + triggered.map(function(t){ return '• ' + t.title + ' (' + t.project_name + ')'; }).join('\n');
      if (skipped.length) msg += '\n\n跳过的暂停项目: ' + skipped.join(', ');
    }
    alert(msg);
    // 触发后稍等几秒刷新事件日志
    setTimeout(function(){ if (typeof loadAgentEventLog === 'function') loadAgentEventLog(agentId); }, 2000);
  } catch(e) {
    alert('唤醒失败: ' + e.message);
  }
}

async function loadTasks(agentId) {
  const data = await api('GET', `/api/portal/agent/${agentId}/tasks`);
  if (!data) return;
  const el = document.getElementById('tasks-list-' + agentId);
  if (!el) return;
  const tasks = data.tasks || [];
  if (tasks.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);padding:8px">No tasks yet</div>';
    return;
  }
  const statusIcon = {todo:'⬜', in_progress:'🔄', done:'✅', blocked:'🚫', cancelled:'❌'};
  const priLabel = {0:'', 1:'🔶', 2:'🔴'};
  const sourceIcon = {admin:'👤', agent:'🤖', system:'⚙️', user:'👤'};
  const sourceColor = {admin:'var(--warning)', agent:'var(--primary)', system:'var(--text3)', user:'var(--success)'};
  el.innerHTML = tasks.map(t => {
    const dlTime = t.deadline ? new Date(t.deadline*1000) : null;
    const dlStr = dlTime ? dlTime.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    const isOverdue = dlTime && dlTime < new Date() && t.status !== 'done' && t.status !== 'cancelled';
    const src = t.source || 'admin';
    const srcAgent = t.source_agent_id ? ` (${esc(t.source_agent_id.slice(0,8))})` : '';
    return `
    <div style="padding:8px;border-bottom:1px solid var(--border);cursor:pointer${isOverdue?';background:rgba(255,80,80,0.08)':''}"
         onclick="toggleTaskStatus('${agentId}','${t.id}','${t.status}')">
      <div style="display:flex;align-items:center;gap:6px">
        <span>${statusIcon[t.status]||'⬜'}</span>
        <span style="${t.status==='done'?'text-decoration:line-through;color:var(--text3)':''}">${esc(t.title)}</span>
        ${priLabel[t.priority]||''}
        ${isOverdue?'<span style="color:var(--error);font-size:10px;font-weight:600">OVERDUE</span>':''}
      </div>
      ${t.description ? `<div style="font-size:11px;color:var(--text3);margin-top:2px;margin-left:22px">${esc(t.description.slice(0,80))}</div>` : ''}
      ${t.result ? `<div style="font-size:11px;color:var(--green);margin-top:2px;margin-left:22px">${esc(t.result.slice(0,80))}</div>` : ''}
      <div style="font-size:10px;color:var(--text3);margin-top:4px;margin-left:22px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="color:${sourceColor[src]||'var(--text3)'}" title="Source: ${src}${srcAgent}">${sourceIcon[src]||'👤'} ${src}${srcAgent}</span>
        ${dlStr ? `<span style="color:${isOverdue?'var(--error)':'var(--text3)'}" title="Deadline">⏰ ${dlStr}</span>` : ''}
        ${t.tags && t.tags.length > 0 ? t.tags.map(tag=>`<span class="tag tag-blue" style="font-size:9px">${esc(tag)}</span>`).join(' ') : ''}
        <button class="btn btn-sm" style="font-size:10px;padding:0 4px;border:none;color:var(--red)"
                onclick="event.stopPropagation();deleteTask('${agentId}','${t.id}')">×</button>
      </div>
    </div>`;
  }).join('');
}

async function toggleTaskStatus(agentId, taskId, currentStatus) {
  const next = {todo:'in_progress', in_progress:'done', done:'todo', blocked:'todo', cancelled:'todo'};
  await api('POST', `/api/portal/agent/${agentId}/tasks`, {
    action: 'update', task_id: taskId, status: next[currentStatus] || 'todo'
  });
  loadTasks(agentId);
}

async function addTaskDialog(agentId) {
  // Rich task creation dialog
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:10000;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML = `
    <div style="background:var(--card);border-radius:12px;padding:24px;width:440px;max-width:90vw;border:1px solid var(--border)">
      <h3 style="margin:0 0 16px;color:var(--text)">New Task</h3>
      <div style="display:flex;flex-direction:column;gap:12px">
        <div>
          <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Title *</label>
          <input id="_task_title" class="input" style="width:100%" placeholder="Task title...">
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Description</label>
          <textarea id="_task_desc" class="input" style="width:100%;height:60px;resize:vertical" placeholder="Optional description..."></textarea>
        </div>
        <div style="display:flex;gap:12px">
          <div style="flex:1">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Source</label>
            <select id="_task_source" class="input" style="width:100%">
              <option value="admin">Admin (管理员)</option>
              <option value="user">User (用户)</option>
              <option value="agent">Agent (其他Agent)</option>
              <option value="system">System (系统)</option>
            </select>
          </div>
          <div style="flex:1">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Priority</label>
            <select id="_task_priority" class="input" style="width:100%">
              <option value="0">Normal</option>
              <option value="1">High 🔶</option>
              <option value="2">Urgent 🔴</option>
            </select>
          </div>
        </div>
        <div style="display:flex;gap:12px">
          <div style="flex:1">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Deadline</label>
            <input id="_task_deadline" type="datetime-local" class="input" style="width:100%">
          </div>
          <div style="flex:1">
            <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Source Agent ID</label>
            <input id="_task_src_agent" class="input" style="width:100%" placeholder="If source=agent">
          </div>
        </div>
        <div>
          <label style="font-size:11px;color:var(--text3);display:block;margin-bottom:4px">Tags (comma-separated)</label>
          <input id="_task_tags" class="input" style="width:100%" placeholder="tag1, tag2...">
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn btn-ghost" onclick="this.closest('div[style*=fixed]').remove()">Cancel</button>
        <button class="btn btn-primary" id="_task_submit">Create Task</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  document.getElementById('_task_submit').onclick = async function() {
    const title = document.getElementById('_task_title').value.trim();
    if (!title) { alert('Title is required'); return; }
    const desc = document.getElementById('_task_desc').value.trim();
    const source = document.getElementById('_task_source').value;
    const priority = parseInt(document.getElementById('_task_priority').value) || 0;
    const dlInput = document.getElementById('_task_deadline').value;
    const deadline = dlInput ? new Date(dlInput).getTime() / 1000 : 0;
    const srcAgent = document.getElementById('_task_src_agent').value.trim();
    const tagsStr = document.getElementById('_task_tags').value.trim();
    const tags = tagsStr ? tagsStr.split(',').map(s=>s.trim()).filter(Boolean) : [];
    await api('POST', `/api/portal/agent/${agentId}/tasks`, {
      action: 'create', title, description: desc,
      source, source_agent_id: srcAgent,
      priority, deadline, tags
    });
    overlay.remove();
    loadTasks(agentId);
  };
  document.getElementById('_task_title').focus();
}

async function deleteTask(agentId, taskId) {
  await api('POST', `/api/portal/agent/${agentId}/tasks`, {
    action: 'delete', task_id: taskId
  });
  loadTasks(agentId);
}

// ── SOUL.md Editor + Robot Avatar Picker ──
var _robotList = [
  {id:'robot_ceo',label:'CEO',color:'#FFD700'},
  {id:'robot_cto',label:'CTO',color:'#4A90D9'},
  {id:'robot_coder',label:'Coder',color:'#4CAF50'},
  {id:'robot_reviewer',label:'Reviewer',color:'#9C27B0'},
  {id:'robot_researcher',label:'Researcher',color:'#009688'},
  {id:'robot_architect',label:'Architect',color:'#FF9800'},
  {id:'robot_devops',label:'DevOps',color:'#F44336'},
  {id:'robot_designer',label:'Designer',color:'#E91E63'},
  {id:'robot_pm',label:'PM',color:'#1A237E'},
  {id:'robot_tester',label:'Tester',color:'#8BC34A'},
  {id:'robot_data',label:'Data',color:'#00BCD4'},
  {id:'robot_general',label:'General',color:'#78909C'}
];

async function showSoulEditor(agentId) {
  // Fetch current soul
  var resp = await api('GET', '/api/portal/agent/'+agentId+'/soul');
  var soulMd = (resp && resp.soul_md) || '';
  var robotAvatar = (resp && resp.robot_avatar) || '';
  var agRole = (resp && resp.role) || 'general';
  // If no soul yet, load default template
  if (!soulMd) {
    try {
      var tplResp = await fetch('/static/templates/souls/soul_'+agRole+'.md');
      if (tplResp.ok) soulMd = await tplResp.text();
    } catch(e) {}
  }
  if (!robotAvatar) robotAvatar = 'robot_' + agRole;

  // Build modal
  var overlay = document.createElement('div');
  overlay.id = 'soul-editor-overlay';
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  overlay.innerHTML = '' +
    '<div style="background:var(--bg2);border-radius:16px;width:800px;max-width:90vw;max-height:90vh;display:flex;flex-direction:column;border:1px solid var(--border);overflow:hidden">' +
      '<div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<span class="material-symbols-outlined" style="color:var(--primary);font-size:22px">auto_awesome</span>' +
          '<span style="font-size:16px;font-weight:700;font-family:\'Plus Jakarta Sans\',sans-serif">SOUL.md — Agent Personality</span>' +
        '</div>' +
        '<button onclick="document.getElementById(\'soul-editor-overlay\').remove()" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:20px">✕</button>' +
      '</div>' +
      '<div style="padding:16px 20px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:16px">' +
        '<div>' +
          '<label style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--text3);margin-bottom:8px;display:block">Select Robot Avatar</label>' +
          '<div id="soul-robot-grid" style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px"></div>' +
        '</div>' +
        '<div style="flex:1;display:flex;flex-direction:column">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">' +
            '<label style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--text3)">SOUL.md (Markdown)</label>' +
            '<div style="display:flex;gap:6px">' +
              '<button class="btn btn-ghost btn-sm" onclick="_soulLoadTemplate(\''+agRole+'\')" style="font-size:10px">Load Default Template</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="soul-editor-textarea" style="flex:1;min-height:340px;width:100%;padding:12px 14px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;font-family:\'JetBrains Mono\',\'SF Mono\',monospace;font-size:12px;line-height:1.6;resize:vertical;box-sizing:border-box;tab-size:2"></textarea>' +
        '</div>' +
      '</div>' +
      '<div style="padding:12px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px">' +
        '<button class="btn btn-ghost" onclick="document.getElementById(\'soul-editor-overlay\').remove()">Cancel</button>' +
        '<button class="btn btn-primary" onclick="_saveSoul(\''+agentId+'\')">Save SOUL</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  // Populate robot grid
  var grid = document.getElementById('soul-robot-grid');
  _robotList.forEach(function(r) {
    var selected = (robotAvatar === r.id) ? 'border:2px solid var(--primary);box-shadow:0 0 8px var(--primary)' : 'border:2px solid transparent';
    grid.innerHTML += '<div class="soul-robot-option" data-robot="'+r.id+'" onclick="_selectRobot(this,\''+r.id+'\')" style="cursor:pointer;padding:8px;border-radius:10px;'+selected+';background:var(--surface);display:flex;flex-direction:column;align-items:center;gap:4px;transition:all 0.2s">' +
      '<img src="/static/robots/'+r.id+'.svg" style="width:40px;height:40px">' +
      '<span style="font-size:9px;color:var(--text3);font-weight:600">'+r.label+'</span>' +
    '</div>';
  });

  // Populate textarea
  document.getElementById('soul-editor-textarea').value = soulMd;
  // Store selected robot
  window._soulSelectedRobot = robotAvatar;
}

function _selectRobot(el, robotId) {
  document.querySelectorAll('.soul-robot-option').forEach(function(o) {
    o.style.border = '2px solid transparent';
    o.style.boxShadow = 'none';
  });
  el.style.border = '2px solid var(--primary)';
  el.style.boxShadow = '0 0 8px var(--primary)';
  window._soulSelectedRobot = robotId;
}

async function _soulLoadTemplate(role) {
  try {
    var resp = await fetch('/static/templates/souls/soul_'+role+'.md');
    if (resp.ok) {
      var text = await resp.text();
      document.getElementById('soul-editor-textarea').value = text;
    }
  } catch(e) { console.error('Failed to load template:', e); }
}

async function _saveSoul(agentId) {
  var soulMd = document.getElementById('soul-editor-textarea').value;
  var robotAvatar = window._soulSelectedRobot || '';
  await api('POST', '/api/portal/agent/'+agentId+'/soul', {
    soul_md: soulMd,
    robot_avatar: robotAvatar
  });
  document.getElementById('soul-editor-overlay').remove();
  // Refresh state to update sidebar avatars
  refreshState();
  if (currentAgent === agentId) renderCurrentView();
}

// ── Active Thinking Panel ──
async function showThinkingPanel(agentId) {
  var resp = await api('GET', '/api/portal/agent/'+agentId+'/thinking');
  var stats = (resp && resp.stats) || {enabled:false};
  var history = (resp && resp.history) || [];

  var overlay = document.createElement('div');
  overlay.id = 'thinking-panel-overlay';
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.7);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';

  var historyHtml = history.length === 0 ? '<div style="color:var(--text3);padding:16px;text-align:center">暂无思考记录。启用后点击"立即思考"触发第一次。</div>' :
    history.map(function(h) {
      var dt = new Date(h.created_at * 1000);
      var timeStr = dt.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
      var triggerBadge = {manual:'手动',time_driven:'定时',idle:'空闲',state_change:'状态变化',goal_gap:'目标差距'}[h.trigger]||h.trigger;
      return '<div style="padding:10px 12px;border-bottom:1px solid var(--border);cursor:pointer" onclick="this.querySelector(\'.think-detail\').style.display=this.querySelector(\'.think-detail\').style.display===\'none\'?\'block\':\'none\'">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<div><span style="font-size:11px;color:var(--text3)">'+timeStr+'</span> <span style="font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(203,201,255,0.15);color:var(--primary)">'+triggerBadge+'</span></div>' +
          '<div style="font-size:11px;color:var(--success)">质量: '+h.quality_score+'/100</div>' +
        '</div>' +
        '<div class="think-detail" style="display:none;margin-top:8px;font-size:12px;color:var(--text2);line-height:1.6">' +
          (h.step3_problem ? '<div><b>核心问题:</b> '+esc(h.step3_problem).substring(0,200)+'</div>' : '') +
          (h.step5_best_action ? '<div><b>最优行动:</b> '+esc(h.step5_best_action).substring(0,200)+'</div>' : '') +
          (h.step7_reflection ? '<div><b>反思:</b> '+esc(h.step7_reflection).substring(0,200)+'</div>' : '') +
          (h.proposed_actions && h.proposed_actions.length ? '<div style="margin-top:4px"><b>提议行动:</b><ol style="margin:2px 0 0 16px">' + h.proposed_actions.map(function(a){return '<li>'+esc(a)+'</li>';}).join('') + '</ol></div>' : '') +
        '</div>' +
      '</div>';
    }).join('');

  overlay.innerHTML = '' +
    '<div style="background:var(--bg2);border-radius:16px;width:700px;max-width:90vw;max-height:85vh;display:flex;flex-direction:column;border:1px solid var(--border);overflow:hidden">' +
      '<div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<span class="material-symbols-outlined" style="color:var(--primary);font-size:22px">psychology</span>' +
          '<span style="font-size:16px;font-weight:700;font-family:\'Plus Jakarta Sans\',sans-serif">主动思考 (Active Thinking)</span>' +
          '<span style="font-size:11px;padding:2px 8px;border-radius:4px;background:'+(stats.enabled?'rgba(76,175,80,0.15);color:var(--success)':'rgba(255,255,255,0.1);color:var(--text3)')+'">'+(stats.enabled?'已启用':'未启用')+'</span>' +
        '</div>' +
        '<button onclick="document.getElementById(\'thinking-panel-overlay\').remove()" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:20px">✕</button>' +
      '</div>' +
      '<div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;gap:12px;flex-wrap:wrap;align-items:center">' +
        '<button class="btn btn-sm '+(stats.enabled?'btn-danger':'btn-primary')+'" onclick="_toggleThinking(\''+agentId+'\','+(!stats.enabled)+')">'+(stats.enabled?'停用':'启用')+' Active Thinking</button>' +
        '<button class="btn btn-sm btn-ghost" onclick="_triggerThinking(\''+agentId+'\',\'manual\')"><span class="material-symbols-outlined" style="font-size:14px">play_arrow</span> 立即思考</button>' +
        '<div style="flex:1"></div>' +
        (stats.enabled ? '<div style="font-size:11px;color:var(--text3)">间隔: '+((stats.config&&stats.config.time_interval_minutes)||60)+'分钟 | 总次数: '+(stats.total_thinks||0)+' | 平均质量: '+(stats.avg_quality_score||0)+'</div>' : '') +
      '</div>' +
      '<div style="padding:0 4px">' +
        '<div style="padding:12px 16px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--text3)">思考历史 (Thinking History)</div>' +
      '</div>' +
      '<div style="flex:1;overflow-y:auto">' +
        historyHtml +
      '</div>' +
      '<div style="padding:12px 20px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">' +
        '<span style="font-size:11px;color:var(--text3)">配置：</span>' +
        '<label style="font-size:11px;color:var(--text2)">间隔(分钟)</label>' +
        '<input id="think-interval" type="number" value="'+((stats.config&&stats.config.time_interval_minutes)||60)+'" min="5" max="1440" style="width:60px;padding:4px;background:var(--surface3);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px">' +
        '<label style="font-size:11px;color:var(--text2);margin-left:8px">空闲触发</label>' +
        '<input id="think-idle" type="checkbox" '+((stats.config&&stats.config.auto_think_on_idle)?'checked':'')+' style="margin-left:4px">' +
        '<div style="flex:1"></div>' +
        '<button class="btn btn-ghost btn-sm" onclick="_saveThinkingConfig(\''+agentId+'\')">保存配置</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
}

async function _toggleThinking(agentId, enable) {
  if (enable) {
    var interval = parseInt(document.getElementById('think-interval').value) || 60;
    var idle = document.getElementById('think-idle').checked;
    await api('POST', '/api/portal/agent/'+agentId+'/thinking/enable', {
      time_interval_minutes: interval,
      auto_think_on_idle: idle
    });
  } else {
    await api('POST', '/api/portal/agent/'+agentId+'/thinking/disable', {});
  }
  document.getElementById('thinking-panel-overlay').remove();
  showThinkingPanel(agentId);
}

async function _triggerThinking(agentId, trigger) {
  var btn = event.target.closest('button');
  if (btn) { btn.disabled = true; btn.textContent = '思考中...'; }
  await api('POST', '/api/portal/agent/'+agentId+'/thinking/trigger', {trigger: trigger});
  if (btn) { btn.disabled = false; btn.textContent = '✓ 完成'; }
  setTimeout(function() {
    document.getElementById('thinking-panel-overlay').remove();
    showThinkingPanel(agentId);
  }, 1000);
}

async function _saveThinkingConfig(agentId) {
  var interval = parseInt(document.getElementById('think-interval').value) || 60;
  var idle = document.getElementById('think-idle').checked;
  await api('POST', '/api/portal/agent/'+agentId+'/thinking/enable', {
    time_interval_minutes: interval,
    auto_think_on_idle: idle
  });
  document.getElementById('thinking-panel-overlay').remove();
  showThinkingPanel(agentId);
}

async function editAgentProfile(agentId) {
  const agent = agents.find(a => a.id === agentId);
  if (!agent) return;

  const prof = agent.profile || {};
  // Populate core fields
  document.getElementById('ea-name').value = agent.name || '';
  document.getElementById('ea-role').value = agent.role || 'general';
  document.getElementById('ea-workdir').value = agent.working_dir || '';
  // Populate provider and model selects
  document.getElementById('ea-provider').value = agent.provider || '';
  updateModelSelect('ea-provider', 'ea-model', agent.model || '');
  // ── 方案甲: learning / multimodal slots ──
  var lpEl = document.getElementById('ea-learning-provider');
  if (lpEl) {
    lpEl.value = agent.learning_provider || '';
    updateModelSelect('ea-learning-provider', 'ea-learning-model', agent.learning_model || '');
  }
  var mpEl = document.getElementById('ea-multimodal-provider');
  if (mpEl) {
    mpEl.value = agent.multimodal_provider || '';
    updateModelSelect('ea-multimodal-provider', 'ea-multimodal-model', agent.multimodal_model || '');
  }
  var cpEl = document.getElementById('ea-coding-provider');
  if (cpEl) {
    cpEl.value = agent.coding_provider || '';
    updateModelSelect('ea-coding-provider', 'ea-coding-model', agent.coding_model || '');
  }
  // multimodal_supports_tools checkbox + hint
  var mmToolsCb = document.getElementById('ea-mm-supports-tools');
  if (mmToolsCb) mmToolsCb.checked = !!agent.multimodal_supports_tools;
  _updateMmToolsHint();
  // ── 方案乙(a): extra_llms 任意命名 slot ──
  window._eaExtraLlms = Array.isArray(agent.extra_llms) ? agent.extra_llms.map(function(s){
    return {
      label: (s && s.label) || '',
      provider: (s && s.provider) || '',
      model: (s && s.model) || '',
      purpose: (s && s.purpose) || '',
      note: (s && s.note) || '',
    };
  }) : [];
  eaRenderExtraLlms();
  // ── 方案乙(b): auto_route 启发式 ──
  var ar = agent.auto_route || {};
  var arEn = document.getElementById('ea-ar-enabled');
  if (arEn) arEn.checked = !!ar.enabled;
  var arDef = document.getElementById('ea-ar-default'); if (arDef) arDef.value = ar['default'] || '';
  var arCpx = document.getElementById('ea-ar-complex'); if (arCpx) arCpx.value = ar['complex'] || '';
  var arMm = document.getElementById('ea-ar-multimodal'); if (arMm) arMm.value = ar['multimodal'] || '';
  var arCd = document.getElementById('ea-ar-coding'); if (arCd) arCd.value = ar['coding'] || '';
  var arTh = document.getElementById('ea-ar-threshold'); if (arTh) arTh.value = ar['complex_threshold_chars'] || 2000;
  // 面板默认收起；如果用户已经配过任意 extra_llms / learning / multimodal / auto_route，就自动展开
  var panel = document.getElementById('ea-llm-panel');
  var toggleIcon = document.getElementById('ea-llm-toggle-label');
  var hasCustom = !!(agent.learning_provider || agent.multimodal_provider || agent.coding_provider
    || (window._eaExtraLlms && window._eaExtraLlms.length) || (ar && ar.enabled));
  if (panel) {
    panel.style.display = hasCustom ? 'block' : 'none';
    if (toggleIcon) toggleIcon.textContent = hasCustom ? 'expand_less' : 'expand_more';
  }
  // Populate profile fields
  document.getElementById('ea-personality').value = prof.personality || '';
  document.getElementById('ea-style').value = prof.communication_style || '';
  // Merge expertise + skills into unified tags
  var combinedTags = (prof.expertise || []).concat(prof.skills || []);
  window._eaTags = [];
  combinedTags.forEach(function(t) { if (t && window._eaTags.indexOf(t) < 0) window._eaTags.push(t); });
  _renderTags(window._eaTags, 'ea-tags-display', 'eaRemoveTag');
  document.getElementById('ea-expertise').value = '';
  // Avatar
  window._eaSelectedAvatar = agent.robot_avatar || ('robot_' + (agent.role || 'general'));
  _renderAvatarGrid('ea-avatar-grid', '_eaSelectedAvatar', 'eaPickAvatar');
  document.getElementById('ea-language').value = prof.language || 'auto';
  document.getElementById('ea-prompt').value = prof.custom_instructions || '';

  // Self-improvement state
  var si = agent.self_improvement;
  document.getElementById('ea-self-improve').checked = !!(si && si.enabled);
  document.getElementById('ea-import-exp').checked = !!(si && si.imported_count > 0);
  var eaExpInfo = document.getElementById('ea-exp-info');
  if (eaExpInfo) {
    if (si && si.enabled) {
      eaExpInfo.innerHTML = '📚 Imported: <b>'+(si.imported_count||0)+'</b> | Library: <b>'+(si.library_total||0)+'</b> | Retros: '+(si.retrospective_count||0)+' | Learnings: '+(si.learning_count||0);
    } else {
      eaExpInfo.textContent = 'Self-improvement not enabled';
    }
  }

  window._currentEditAgentId = agentId;
  showModal('edit-agent');
}

async function saveAgentProfile() {
  try {
    const agentId = window._currentEditAgentId;
    if (!agentId) { alert('No agent selected'); return; }

    const nameEl = document.getElementById('ea-name');
    const roleEl = document.getElementById('ea-role');
    const workdirEl = document.getElementById('ea-workdir');
    const providerEl = document.getElementById('ea-provider');
    const modelEl = document.getElementById('ea-model');
    const personalityEl = document.getElementById('ea-personality');
    const styleEl = document.getElementById('ea-style');
    const languageEl = document.getElementById('ea-language');
    const promptEl = document.getElementById('ea-prompt');

    if (!nameEl) { alert('Form elements not found'); return; }
    const name = nameEl.value.trim();
    if (!name) { alert('Agent name is required'); return; }

    const eaTags = window._eaTags || [];
    // 收集 extra_llms —— 过滤掉空行
    var extraLlms = (window._eaExtraLlms || []).filter(function(s){
      return s && String(s.label || '').trim();
    }).map(function(s){
      return {
        label: String(s.label || '').trim(),
        provider: String(s.provider || '').trim(),
        model: String(s.model || '').trim(),
        purpose: String(s.purpose || '').trim(),
        note: String(s.note || '').trim(),
      };
    });
    // 收集 auto_route
    var arEn = document.getElementById('ea-ar-enabled');
    var arDef = document.getElementById('ea-ar-default');
    var arCpx = document.getElementById('ea-ar-complex');
    var arMm = document.getElementById('ea-ar-multimodal');
    var arCd = document.getElementById('ea-ar-coding');
    var arTh = document.getElementById('ea-ar-threshold');
    var autoRoute = {
      enabled: !!(arEn && arEn.checked),
      'default': arDef ? arDef.value.trim() : '',
      'complex': arCpx ? arCpx.value.trim() : '',
      multimodal: arMm ? arMm.value.trim() : '',
      coding: arCd ? arCd.value.trim() : '',
      complex_threshold_chars: arTh ? (parseInt(arTh.value, 10) || 2000) : 2000,
    };
    // 学习 / 多模态 / 编码 slot
    var lp = document.getElementById('ea-learning-provider');
    var lm = document.getElementById('ea-learning-model');
    var mp = document.getElementById('ea-multimodal-provider');
    var mm = document.getElementById('ea-multimodal-model');
    var cdp = document.getElementById('ea-coding-provider');
    var cdm = document.getElementById('ea-coding-model');
    var mmToolsCb = document.getElementById('ea-mm-supports-tools');
    const payload = {
      name: name,
      role: roleEl ? roleEl.value : 'general',
      working_dir: workdirEl ? workdirEl.value.trim() : '',
      provider: providerEl ? providerEl.value : '',
      model: modelEl ? modelEl.value : '',
      learning_provider: lp ? lp.value : '',
      learning_model: lm ? lm.value : '',
      multimodal_provider: mp ? mp.value : '',
      multimodal_model: mm ? mm.value : '',
      coding_provider: cdp ? cdp.value : '',
      coding_model: cdm ? cdm.value : '',
      multimodal_supports_tools: !!(mmToolsCb && mmToolsCb.checked),
      extra_llms: extraLlms,
      auto_route: autoRoute,
      personality: personalityEl ? personalityEl.value.trim() : '',
      communication_style: styleEl ? styleEl.value.trim() : '',
      expertise: eaTags,
      skills: eaTags,
      robot_avatar: window._eaSelectedAvatar || '',
      language: languageEl ? languageEl.value : 'auto',
      custom_instructions: promptEl ? promptEl.value.trim() : '',
    };

    console.log('saveAgentProfile payload:', agentId, payload);
    const result = await api('POST', '/api/portal/agent/' + agentId + '/profile', payload);
    console.log('saveAgentProfile result:', result);
    if (result === null) {
      alert('Failed to save. Check browser console for details.');
      return;
    }
    // Handle self-improvement toggle
    var siCheck = document.getElementById('ea-self-improve');
    var impCheck = document.getElementById('ea-import-exp');
    if (siCheck && siCheck.checked) {
      try {
        await api('POST', '/api/portal/agent/' + agentId + '/self-improvement/enable', {
          import_experience: impCheck ? impCheck.checked : false, import_limit: 50
        });
      } catch(err) { console.error('enable self-improvement error:', err); }
    } else if (siCheck && !siCheck.checked) {
      try {
        await api('POST', '/api/portal/agent/' + agentId + '/self-improvement/disable', {});
      } catch(err) { console.error('disable self-improvement error:', err); }
    }
    hideModal('edit-agent');
    refresh();
  } catch(e) {
    console.error('saveAgentProfile error:', e);
    alert('Error saving agent: ' + e.message);
  }
}

async function deleteAgent(agentId) {
  if(!confirm('Delete this agent permanently?')) return;
  await api('DELETE', '/api/portal/agent/' + agentId);
  currentView = 'dashboard';
  currentAgent = '';
  refresh();
}

async function clearAgent(agentId) {
  if(!confirm('Clear all messages and conversation history for this agent?')) return;
  await api('POST', '/api/portal/agent/' + agentId + '/clear');
  // Re-render agent chat immediately to show empty state
  if (currentView === 'agent' && currentAgent === agentId) {
    renderAgentChat(agentId);
  }
  refresh();
}

async function delegateTask() {
  const fromId = document.getElementById('del-from').value;
  const toId = document.getElementById('del-to').value;
  const task = document.getElementById('del-task').value.trim();
  if(!fromId||!toId||!task) { alert('Please fill in all fields'); return; }
  await api('POST', '/api/hub/message', {
    from_agent: fromId, to_agent: toId, content: task, msg_type: 'task'
  });
  hideModal('delegate');
  refresh();
}

// ============ Channels ============
let channelList = [];
const channelTypeLabels = {
  slack:'Slack', telegram:'Telegram', discord:'Discord',
  dingtalk:'DingTalk', feishu:'Feishu', webhook:'Webhook', wechat_work:'WeChat Work'
};
const channelTypeIcons = {
  slack:'💬', telegram:'✈️', discord:'🎮',
  dingtalk:'🔔', feishu:'🐦', webhook:'🌐', wechat_work:'💼'
};

function renderChannels(container) {
  const c = container || document.getElementById('content');
  if (!container) c.style.padding = '24px';
  const epoch = _renderEpoch;
  api('GET', '/api/portal/channels').then(data => {
    if (!data || epoch !== _renderEpoch) return;
    channelList = data.channels || [];
    var chCountEl = document.getElementById('channel-count');
    if (chCountEl) chCountEl.textContent = channelList.length;
    c.innerHTML = `
      <div style="display:flex;gap:24px">
        <div style="flex:1;max-width:700px">
          <h3 style="font-size:14px;margin-bottom:12px;color:var(--text2)">Configured Channels</h3>
          ${channelList.length===0?'<div style="color:var(--text3);padding:20px">No channels configured. Click "+ Add Channel" to connect a messaging platform.</div>':''}
          <div style="display:grid;gap:12px">
            ${channelList.map(ch => `
              <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:flex-start">
                  <div>
                    <div style="font-weight:600;font-size:15px">
                      ${channelTypeIcons[ch.channel_type]||'🔗'} ${esc(ch.name)}
                      <span class="tag ${ch.enabled?'tag-green':'tag-red'}" style="font-size:10px;margin-left:6px">${ch.enabled?'active':'disabled'}</span>
                      <span class="tag tag-blue" style="font-size:10px;margin-left:4px">${channelTypeLabels[ch.channel_type]||ch.channel_type}</span>
                      <span class="tag" style="font-size:10px;margin-left:4px;background:${ch.mode==='polling'?'rgba(251,191,36,0.15);color:#f59e0b':'rgba(96,165,250,0.15);color:#60a5fa'}">${ch.mode==='polling'?'Polling':'Webhook'}</span>
                    </div>
                    <div style="font-size:12px;color:var(--text3);margin-top:4px">
                      Agent: <strong>${esc(ch.agent_id ? (agents.find(a=>a.id===ch.agent_id)||{}).name || ch.agent_id : '(unbound)')}</strong>
                    </div>
                    ${ch.mode==='webhook' ? '<div style="font-size:12px;color:var(--text3);margin-top:2px">Webhook URL: <code style="font-size:11px;background:var(--surface2);padding:2px 6px;border-radius:4px">/api/portal/channels/'+ch.id+'/webhook</code></div>' : ''}
                    ${ch.webhook_url ? '<div style="font-size:12px;color:var(--text3);margin-top:2px">Outbound: '+esc(ch.webhook_url)+'</div>' : ''}
                  </div>
                </div>
                <div style="margin-top:10px;display:flex;gap:6px">
                  <button class="btn btn-sm btn-ghost" onclick="editChannel('${esc(ch.id)}')">Edit</button>
                  <button class="btn btn-sm btn-ghost" onclick="testChannel('${esc(ch.id)}')">Test</button>
                  <button class="btn btn-sm btn-danger" onclick="deleteChannel('${esc(ch.id)}')">Delete</button>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
        <div style="width:320px">
          <h3 style="font-size:14px;margin-bottom:12px;color:var(--text2)">Event Log</h3>
          <div id="channel-event-log" style="font-size:12px"></div>
        </div>
      </div>
    `;
    loadChannelEvents();
  });
}

async function loadChannelEvents() {
  const data = await api('GET', '/api/portal/channels/events');
  if (!data) return;
  const el = document.getElementById('channel-event-log');
  if (!el) return;
  const events = data.events || [];
  if (events.length === 0) {
    el.innerHTML = '<div style="color:var(--text3);padding:8px">No events yet</div>';
    return;
  }
  el.innerHTML = events.slice(-50).reverse().map(e => `
    <div style="padding:6px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between">
        <span style="color:${e.direction==='inbound'?'var(--accent)':'var(--green)'}">${e.direction==='inbound'?'⬇':'⬆'} ${esc(e.platform)}</span>
        <span style="color:var(--text3);font-size:11px">${new Date(e.timestamp*1000).toLocaleTimeString()}</span>
      </div>
      <div style="color:var(--text2);margin-top:2px">${esc(e.sender||'system')}: ${esc((e.text||'').slice(0,100))}</div>
      ${e.reply ? '<div style="color:var(--green);margin-top:2px">↳ '+esc(e.reply.slice(0,100))+'</div>' : ''}
    </div>
  `).join('');
}

async function addChannel() {
  const name = document.getElementById('ac-name').value.trim();
  const channel_type = document.getElementById('ac-type').value;
  const agent_id = document.getElementById('ac-agent').value;
  const mode = (document.getElementById('ac-mode') || {}).value || 'polling';
  const bot_token = document.getElementById('ac-token').value.trim();
  const signing_secret = document.getElementById('ac-secret').value.trim();
  const webhook_url = document.getElementById('ac-webhook').value.trim();
  const app_id = document.getElementById('ac-appid').value.trim();
  const app_secret = document.getElementById('ac-appsecret').value.trim();
  if (!name) { alert('Channel name is required'); return; }
  await api('POST', '/api/portal/channels', {
    name, channel_type, agent_id, mode, bot_token, signing_secret,
    webhook_url, app_id, app_secret
  });
  hideModal('add-channel');
  ['ac-name','ac-token','ac-secret','ac-webhook','ac-appid','ac-appsecret'].forEach(id => {
    const el = document.getElementById(id); if(el) el.value = '';
  });
  renderChannels();
}

function editChannel(id) {
  const ch = channelList.find(x => x.id === id);
  if (!ch) return;
  document.getElementById('ec-name').value = ch.name;
  document.getElementById('ec-type').value = ch.channel_type;
  document.getElementById('ec-agent').value = ch.agent_id || '';
  var modeEl = document.getElementById('ec-mode');
  if (modeEl) { modeEl.value = ch.mode || 'polling'; }
  var wgEl = document.getElementById('ec-webhook-group');
  if (wgEl) { wgEl.style.display = (ch.mode === 'webhook') ? '' : 'none'; }
  document.getElementById('ec-token').value = ch.bot_token || '';
  document.getElementById('ec-secret').value = ch.signing_secret || '';
  document.getElementById('ec-webhook').value = ch.webhook_url || '';
  document.getElementById('ec-appid').value = ch.app_id || '';
  document.getElementById('ec-appsecret').value = ch.app_secret || '';
  document.getElementById('ec-enabled').value = String(ch.enabled);
  window._editChannelId = id;
  showModal('edit-channel');
}

async function saveChannel() {
  const id = window._editChannelId;
  if (!id) return;
  const body = {
    name: document.getElementById('ec-name').value.trim(),
    channel_type: document.getElementById('ec-type').value,
    agent_id: document.getElementById('ec-agent').value,
    mode: (document.getElementById('ec-mode') || {}).value || 'polling',
    bot_token: document.getElementById('ec-token').value.trim(),
    signing_secret: document.getElementById('ec-secret').value.trim(),
    webhook_url: document.getElementById('ec-webhook').value.trim(),
    app_id: document.getElementById('ec-appid').value.trim(),
    app_secret: document.getElementById('ec-appsecret').value.trim(),
    enabled: document.getElementById('ec-enabled').value === 'true',
  };
  await api('POST', `/api/portal/channels/${id}/update`, body);
  hideModal('edit-channel');
  renderChannels();
}

async function deleteChannel(id) {
  if (!confirm('Delete this channel?')) return;
  await api('DELETE', `/api/portal/channels/${id}`);
  renderChannels();
}

async function testChannel(id) {
  var btn = event && event.target ? event.target : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Testing...'; }
  try {
    const data = await api('POST', `/api/portal/channels/${id}/test`);
    if (!data) return;
    var ok = data.success;
    var msg = ok ? (data.message || 'Connected!') : ('Failed: ' + (data.error || 'unknown'));
    if (ok && data.polling) msg += ' · Polling: ' + data.polling;
    var card = btn ? btn.closest('.card') : null;
    var toast = document.createElement('div');
    toast.style.cssText = 'margin-top:8px;padding:8px 12px;border-radius:6px;font-size:12px;transition:opacity 0.3s;'
      + (ok ? 'background:rgba(16,185,129,0.12);color:#10b981;border:1px solid rgba(16,185,129,0.25)'
           : 'background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.25)');
    toast.textContent = (ok ? '✓ ' : '✗ ') + msg;
    if (card) { card.appendChild(toast); } else { document.body.appendChild(toast); }
    setTimeout(function(){ toast.style.opacity = '0'; setTimeout(function(){ toast.remove(); }, 300); }, 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Test'; }
  }
}

// ============ Agent option helpers ============
function buildAgentOptions() {
  const selIds = ['ac-agent', 'ec-agent', 'del-from', 'del-to'];
  selIds.forEach(selId => {
    const sel = document.getElementById(selId);
    if (!sel) return;
    const curVal = sel.value;
    sel.innerHTML = '<option value="">(none)</option>';
    agents.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.id;
      opt.textContent = (a.role||'general') + '-' + a.name;
      if (curVal === a.id) opt.selected = true;
      sel.appendChild(opt);
    });
  });
}

// ============ Projects ============
async function renderProjects() {
  var c = document.getElementById('content');
  c.style.padding = '24px';
  // Inline header with title + action button (top-right) — mirrors the
  // Workflow tab pattern. No button duplication in the global topbar.
  var _projHeader = ''
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">'
    + '  <div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:22px;font-weight:800;margin:0">Projects</h2>'
    + '    <p style="font-size:12px;color:var(--text3);margin-top:4px">组织多 agent 协作项目与任务</p></div>'
    + '  <button class="btn btn-primary btn-sm" onclick="showCreateProjectModal()"><span class="material-symbols-outlined" style="font-size:14px">add</span> New Project</button>'
    + '</div>';
  try {
    var data = await api('GET', '/api/portal/projects');
    var projects = data.projects || [];
    var pcEl = document.getElementById('project-count');
    if (pcEl) pcEl.textContent = projects.length;
    if (!projects.length) {
      c.innerHTML = _projHeader + '<div style="text-align:center;padding:60px 20px;color:var(--text3)"><span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:12px">folder_special</span><div style="font-size:18px;font-weight:700;margin-bottom:8px">No Projects Yet</div><div style="font-size:13px;margin-bottom:16px">Create a project to organize agents into collaborative teams</div><button class="btn btn-primary" onclick="showCreateProjectModal()"><span class="material-symbols-outlined" style="font-size:16px">add</span> Create Project</button></div>';
      return;
    }
    // Sort by created_at descending (newest first)
    projects.sort(function(a, b) {
      var da = a.created_at ? new Date(a.created_at).getTime() : 0;
      var db = b.created_at ? new Date(b.created_at).getTime() : 0;
      return db - da;
    });
    // Status metadata — label / color / bg per lifecycle state
    var STATUS_META = {
      planning:  { label: '未开始', zh: '未开始', color: '#9ca3af', bg: 'rgba(156,163,175,0.12)' },
      active:    { label: '进行中', zh: '进行中', color: 'var(--primary)', bg: 'rgba(203,201,255,0.12)' },
      suspended: { label: '挂起',   zh: '挂起',   color: '#f59e0b', bg: 'rgba(245,158,11,0.12)' },
      cancelled: { label: '停止',   zh: '停止',   color: '#ef4444', bg: 'rgba(239,68,68,0.12)' },
      completed: { label: '结束',   zh: '结束',   color: '#22c55e', bg: 'rgba(34,197,94,0.12)' },
      archived:  { label: '归档',   zh: '归档',   color: '#6b7280', bg: 'rgba(107,114,128,0.12)' },
    };
    function _statusMeta(s){
      return STATUS_META[s] || { label: s || 'unknown', color: 'var(--text3)', bg: 'rgba(255,255,255,0.05)' };
    }

    // Split into active (in-flight) vs closed (结束/停止/归档)
    var OPEN_STATES = {planning:1, active:1, suspended:1};
    var activeProjs    = projects.filter(function(p){ return OPEN_STATES[p.status] === 1; });
    var completedProjs = projects.filter(function(p){ return OPEN_STATES[p.status] !== 1; });

    function _projCard(p) {
      var ts = p.task_summary || {};
      var meta = _statusMeta(p.status);
      var isClosed = !OPEN_STATES[p.status];
      // Status transition buttons contextual to current state
      var statusBtns = '';
      function _btn(icon, label, target, color){
        var col = color || 'var(--text2)';
        return '<button class="btn btn-ghost btn-sm" style="flex:none;color:'+col+'" onclick="event.stopPropagation();changeProjectStatus(\''+p.id+'\',\''+target+'\')"><span class="material-symbols-outlined" style="font-size:14px">'+icon+'</span> '+label+'</button>';
      }
      if (p.status === 'planning') {
        statusBtns += _btn('play_arrow', '启动', 'active', 'var(--primary)');
        statusBtns += _btn('cancel', '停止', 'cancelled', '#ef4444');
      } else if (p.status === 'active') {
        statusBtns += _btn('pause', '挂起', 'suspended', '#f59e0b');
        statusBtns += _btn('check_circle', '结束', 'completed', '#22c55e');
        statusBtns += _btn('cancel', '停止', 'cancelled', '#ef4444');
      } else if (p.status === 'suspended') {
        statusBtns += _btn('play_arrow', '恢复', 'active', 'var(--primary)');
        statusBtns += _btn('cancel', '停止', 'cancelled', '#ef4444');
      } else if (p.status === 'cancelled' || p.status === 'completed') {
        statusBtns += _btn('restart_alt', '重新激活', 'active', 'var(--primary)');
        statusBtns += _btn('archive', '归档', 'archived', 'var(--text3)');
      } else if (p.status === 'archived') {
        statusBtns += _btn('unarchive', '取消归档', 'active', 'var(--primary)');
      }
      return '<div class="card" style="background:var(--surface);border-radius:14px;padding:24px;border:1px solid var(--border-light);cursor:pointer;transition:transform 0.15s,box-shadow 0.15s;opacity:'+(isClosed?'0.78':'1')+'" onmouseenter="this.style.transform=\'translateY(-2px)\';this.style.boxShadow=\'0 8px 24px rgba(0,0,0,0.2)\'" onmouseleave="this.style.transform=\'none\';this.style.boxShadow=\'none\'" onclick="openProject(\''+p.id+'\')">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">' +
          '<div style="font-size:17px;font-weight:700;color:var(--text)">'+esc(p.name)+'</div>' +
          '<span style="font-size:10px;padding:3px 10px;border-radius:6px;background:'+meta.bg+';color:'+meta.color+';font-weight:700;letter-spacing:0.5px">'+esc(meta.label)+'</span>' +
        '</div>' +
        '<div style="font-size:13px;color:var(--text3);margin-bottom:16px;line-height:1.4">'+esc(p.description||'No description')+'</div>' +
        '<div style="display:flex;gap:16px;margin-bottom:14px">' +
          '<div style="font-size:12px;color:var(--text3);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:16px">group</span> '+p.members.length+' members</div>' +
          '<div style="font-size:12px;color:var(--text3);display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:16px">chat</span> '+p.chat_count+' messages</div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;font-size:11px;margin-bottom:16px">' +
          '<span style="padding:3px 8px;border-radius:4px;background:rgba(34,197,94,0.1);color:#22c55e;font-weight:600">✓ '+(ts.done||0)+' done</span>' +
          '<span style="padding:3px 8px;border-radius:4px;background:rgba(59,130,246,0.1);color:#3b82f6;font-weight:600">⏳ '+(ts.in_progress||0)+' active</span>' +
          '<span style="padding:3px 8px;border-radius:4px;background:rgba(255,255,255,0.05);color:var(--text3);font-weight:600">📋 '+(ts.todo||0)+' todo</span>' +
        '</div>' +
        '<div style="display:flex;gap:6px;border-top:1px solid rgba(255,255,255,0.06);padding-top:14px;flex-wrap:wrap">' +
          '<button class="btn btn-primary btn-sm" style="flex:none" onclick="event.stopPropagation();openProject(\''+p.id+'\')"><span class="material-symbols-outlined" style="font-size:14px">open_in_new</span> Open</button>' +
          statusBtns +
          '<button class="btn btn-ghost btn-sm" style="flex:none" onclick="event.stopPropagation();editProject(\''+p.id+'\',\''+esc(p.name).replace(/'/g,"\\'")+'\',\''+esc(p.description||'').replace(/'/g,"\\'")+'\')"><span class="material-symbols-outlined" style="font-size:14px">edit</span> Edit</button>' +
          '<button class="btn btn-ghost btn-sm" style="flex:none;color:var(--error)" onclick="event.stopPropagation();deleteProject(\''+p.id+'\')"><span class="material-symbols-outlined" style="font-size:14px">delete</span> Delete</button>' +
        '</div>' +
      '</div>';
    }

    var html = '';
    // Active section
    if (activeProjs.length) {
      html += '<div style="margin-bottom:32px">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
          '<span class="material-symbols-outlined" style="font-size:20px;color:var(--primary)">rocket_launch</span>' +
          '<span style="font-size:15px;font-weight:700;color:var(--text)">Active Projects</span>' +
          '<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(203,201,255,0.12);color:var(--primary);font-weight:700">'+activeProjs.length+'</span>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:18px">' +
          activeProjs.map(_projCard).join('') +
        '</div>' +
      '</div>';
    }
    // Completed section
    if (completedProjs.length) {
      html += '<div>' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">' +
          '<span class="material-symbols-outlined" style="font-size:20px;color:#22c55e">check_circle</span>' +
          '<span style="font-size:15px;font-weight:700;color:var(--text)">Completed Projects</span>' +
          '<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(34,197,94,0.1);color:#22c55e;font-weight:700">'+completedProjs.length+'</span>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:18px">' +
          completedProjs.map(_projCard).join('') +
        '</div>' +
      '</div>';
    }
    // If all are in one category, still show something
    if (!html) {
      html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:18px">' +
        projects.map(_projCard).join('') +
      '</div>';
    }
    c.innerHTML = _projHeader + html;
  } catch(e) { c.innerHTML = _projHeader + '<div style="color:var(--error)">Error loading projects</div>'; }
}

function openProject(projId) {
  currentView = 'project_detail';
  currentProject = projId;
  document.querySelectorAll('.nav-item').forEach(function(n){ n.classList.remove('active'); });
  renderCurrentView();
}

async function renderProjectDetail(projId) {
  var c = document.getElementById('content');
  c.style.padding = '0';
  try {
    var proj = await api('GET', '/api/portal/projects/'+projId);
    if (!proj || proj.error) { c.innerHTML = '<div style="padding:24px;color:var(--error)">Project not found</div>'; return; }
    var titleEl = document.getElementById('view-title');
    titleEl.textContent = proj.name;
    var actionsEl = document.getElementById('topbar-actions');
    var pauseBtn = proj.paused
      ? '<button class="btn btn-primary btn-sm" onclick="resumeProject(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">play_arrow</span> Resume'+(proj.paused_queue_count?' ('+proj.paused_queue_count+')':'')+'</button>'
      : '<button class="btn btn-ghost btn-sm" style="color:var(--warning,#ff9800)" onclick="pauseProject(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">pause</span> Pause</button>';
    // Lifecycle-state buttons: 结束 (close successfully) and 停止 (cancel)
    var closeBtn = '';
    if (proj.status !== 'completed' && proj.status !== 'cancelled' && proj.status !== 'archived') {
      closeBtn =
        '<button class="btn btn-ghost btn-sm" style="color:#22c55e" onclick="changeProjectStatus(\''+projId+'\',\'completed\')"><span class="material-symbols-outlined" style="font-size:16px">check_circle</span> 结束</button>' +
        '<button class="btn btn-ghost btn-sm" style="color:#ef4444" onclick="changeProjectStatus(\''+projId+'\',\'cancelled\')"><span class="material-symbols-outlined" style="font-size:16px">cancel</span> 停止</button>';
    } else {
      closeBtn =
        '<button class="btn btn-ghost btn-sm" style="color:var(--primary)" onclick="changeProjectStatus(\''+projId+'\',\'active\')"><span class="material-symbols-outlined" style="font-size:16px">restart_alt</span> 重新激活</button>';
    }
    actionsEl.innerHTML = pauseBtn + closeBtn + '<button class="btn btn-ghost btn-sm" onclick="showProjectMemberModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">group_add</span> Members</button><button class="btn btn-ghost btn-sm" onclick="showProjectTaskModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">add_task</span> Task</button><button class="btn btn-ghost btn-sm" onclick="editProject(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">edit</span> Edit</button><button class="btn btn-ghost btn-sm" style="color:var(--error)" onclick="deleteProject(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">delete</span> Delete</button><button class="btn btn-ghost btn-sm" onclick="currentView=\'projects\';renderCurrentView()"><span class="material-symbols-outlined" style="font-size:16px">arrow_back</span> Back</button>';
    if (proj.paused) {
      var pausedBanner = '<div style="background:rgba(255,152,0,0.15);border-bottom:1px solid var(--warning,#ff9800);padding:8px 16px;color:var(--warning,#ff9800);font-size:12px">⏸️ 项目已暂停 — 暂停人: '+esc(proj.paused_by||'-')+(proj.paused_reason?'，原因: '+esc(proj.paused_reason):'')+(proj.paused_queue_count?'  ·  排队消息: '+proj.paused_queue_count:'')+'</div>';
      // banner inserted via wrapper below
      c.dataset.pausedBanner = pausedBanner;
    }

    c.style.display = 'flex';
    c.style.flexDirection = 'column';
    c.style.height = '100%';
    // Tab bar: Overview (default) | Goals | Milestones | Deliverables | Issues | Team Chat
    var _activeTab = (window._projectDetailTab && window._projectDetailTab[projId]) || 'overview';
    var _tabBtn = function(key, label, icon) {
      var active = (_activeTab === key);
      return '<button onclick="switchProjectTab(\''+projId+'\',\''+key+'\')" class="btn btn-ghost btn-sm" style="border-radius:0;border-bottom:2px solid '+(active?'var(--primary)':'transparent')+';color:'+(active?'var(--primary)':'var(--text2)')+';font-weight:'+(active?'700':'500')+';padding:10px 16px"><span class="material-symbols-outlined" style="font-size:16px;margin-right:4px">'+icon+'</span>'+label+'</button>';
    };
    var tabBar = '<div style="display:flex;gap:2px;border-bottom:1px solid rgba(255,255,255,0.05);background:var(--bg);flex-shrink:0">' +
      _tabBtn('overview','Overview','dashboard') +
      _tabBtn('goals','目标','flag') +
      _tabBtn('milestones','里程碑','timeline') +
      _tabBtn('deliverables','交付件','description') +
      _tabBtn('issues','问题','bug_report') +
      _tabBtn('chat','团队协作','forum') +
    '</div>';
    // Panes: one pane per tab; only the chat pane keeps the legacy grid+sidebar.
    var paneVis = function(key){ return _activeTab === key ? '' : 'display:none;'; };
    c.innerHTML = tabBar +
      '<div id="proj-pane-overview-'+projId+'" style="flex:1;overflow:auto;padding:20px;'+paneVis('overview')+'"></div>' +
      '<div id="proj-pane-goals-'+projId+'" style="flex:1;overflow:auto;padding:20px;'+paneVis('goals')+'"></div>' +
      '<div id="proj-pane-milestones-'+projId+'" style="flex:1;overflow:auto;padding:20px;'+paneVis('milestones')+'"></div>' +
      '<div id="proj-pane-deliverables-'+projId+'" style="flex:1;overflow:auto;padding:20px;'+paneVis('deliverables')+'"></div>' +
      '<div id="proj-pane-issues-'+projId+'" style="flex:1;overflow:auto;padding:20px;'+paneVis('issues')+'"></div>' +
      '<div id="proj-pane-chat-'+projId+'" style="flex:1;min-height:0;'+paneVis('chat')+'">' +
      '<div style="display:grid;grid-template-columns:1fr 300px;height:100%;min-height:0;overflow:hidden">' +
      '<!-- Chat Area -->' +
      '<div style="display:flex;flex-direction:column;min-height:0;border-right:1px solid rgba(255,255,255,0.05)">' +
        '<div id="project-chat-msgs-'+projId+'" style="flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;min-height:0"></div>' +
        '<div style="padding:12px 16px;border-top:1px solid rgba(255,255,255,0.05);display:flex;flex-direction:column;gap:6px;position:relative">' +
          '<div id="mention-dropdown-'+projId+'" class="mention-dropdown"></div>' +
          '<div id="proj-attach-preview-'+projId+'" style="display:none;flex-wrap:wrap;gap:6px"></div>' +
          '<div style="display:flex;gap:8px;align-items:center">' +
            '<input type="file" id="proj-attach-file-'+projId+'" multiple style="display:none" onchange="handleProjAttach(\''+projId+'\',this)">' +
            '<button class="btn btn-ghost btn-sm" title="Attach files" onclick="document.getElementById(\'proj-attach-file-'+projId+'\').click()"><span class="material-symbols-outlined" style="font-size:18px">attach_file</span></button>' +
            '<input type="text" id="project-chat-input-'+projId+'" placeholder="Message the team... (@ to mention)" style="flex:1;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:10px 14px;color:var(--text);font-size:13px" onkeydown="_projInputKeydown(event,\''+projId+'\')" oninput="_projInputChange(\''+projId+'\')">' +
            '<button class="btn btn-primary btn-sm" onclick="sendProjectMsg(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:18px">send</span></button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<!-- Sidebar: Members + Workflow Steps / Milestones + Tasks -->' +
      '<div style="overflow-y:auto;padding:16px;background:var(--bg)">' +
        '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:8px">Workspace</div>' +
        '<div style="font-size:10px;color:var(--text3);margin-bottom:12px;padding:8px;background:var(--surface2);border-radius:6px;border-left:2px solid var(--primary);word-break:break-all">' + (proj.working_directory ? esc(proj.working_directory) : '(not set)') + '</div>' +
        // Team Members: 有 workflow 时只显示文字列表，无 workflow 时显示机器人头像
        (proj.workflow_binding ?
          '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:8px">Team Members</div>' +
          '<div id="project-members-'+projId+'" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:16px"></div>'
          :
          '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:10px">Team Members</div>' +
          '<div id="project-members-'+projId+'" style="display:flex;flex-direction:column;gap:6px;margin-bottom:20px"></div>'
        ) +
        // Workflow Steps 或 Milestones
        (proj.workflow_binding ?
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3)">Workflow Steps</div></div>' +
          '<div id="project-wf-steps-'+projId+'" style="display:flex;flex-direction:column;gap:6px;margin-bottom:20px"></div>'
          :
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3)">Milestones</div><button class="btn btn-ghost btn-xs" onclick="showProjectMilestoneModal(\''+projId+'\')" title="Add milestone"><span class="material-symbols-outlined" style="font-size:14px">add</span></button></div>' +
          '<div id="project-milestones-'+projId+'" style="display:flex;flex-direction:column;gap:6px;margin-bottom:20px"></div>'
        ) +
        '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:10px">Tasks</div>' +
        '<div id="project-tasks-'+projId+'" style="display:flex;flex-direction:column;gap:6px"></div>' +
      '</div>' +
      '</div>' +
    '</div>';  // /proj-pane-chat

    // Populate members — 有 workflow 时紧凑文字，无 workflow 时机器人头像
    var membersEl = document.getElementById('project-members-'+projId);
    if (membersEl) {
      if (proj.workflow_binding) {
        // 紧凑标签式 + 像素机器人头像
        membersEl.innerHTML = proj.members.map(function(m) {
          var ag = agents.find(function(a){ return a.id === m.agent_id; });
          var name = ag ? ag.name : m.agent_id;
          var role = ag ? _resolveRobotRole(ag) : 'general';
          var avatarSrc = _miniRobotDataURL(role);
          return '<span style="display:inline-flex;align-items:center;gap:5px;background:var(--surface);border-radius:12px;padding:3px 10px 3px 4px;border:1px solid rgba(255,255,255,0.08);font-size:11px;color:var(--text)">' +
            '<img src="'+avatarSrc+'" style="width:20px;height:22px;image-rendering:pixelated" />' +
            esc(name) +
          '</span>';
        }).join('');
      } else {
        // 原始机器人头像卡片
        membersEl.innerHTML = proj.members.map(function(m) {
          var ag = agents.find(function(a){ return a.id === m.agent_id; });
          var name = ag ? (ag.role+'-'+ag.name) : m.agent_id;
          var role = ag ? _resolveRobotRole(ag) : 'general';
          var avatarSrc = _miniRobotDataURL(role);
          return '<div style="display:flex;align-items:center;gap:8px;background:var(--surface);border-radius:8px;padding:8px 10px;border:1px solid rgba(255,255,255,0.05);font-size:12px">' +
            '<img src="'+avatarSrc+'" style="width:32px;height:36px;flex-shrink:0;image-rendering:pixelated" />' +
            '<div style="min-width:0">' +
              '<div style="font-weight:700;color:var(--text)">'+esc(name)+'</div>' +
              '<div style="color:var(--text3);font-size:10px;margin-top:2px">'+esc(m.responsibility||'Member')+'</div>' +
            '</div>' +
          '</div>';
        }).join('');
      }
    }

    // Populate Workflow Steps 或 Milestones
    if (proj.workflow_binding) {
      // Workflow Steps 进度显示
      var wfStepsEl = document.getElementById('project-wf-steps-'+projId);
      if (wfStepsEl) {
        // 从 tasks 中提取 [WF Step] 任务，按标题中的步骤号排序
        var wfTasks = (proj.tasks||[]).filter(function(t){ return t.title.indexOf('[WF Step') === 0; });
        wfTasks.sort(function(a,b){
          var na = parseInt((a.title.match(/\[WF Step (\d+)\]/)||[])[1])||0;
          var nb = parseInt((b.title.match(/\[WF Step (\d+)\]/)||[])[1])||0;
          return na - nb;
        });
        // 构建 step_index → agent_id 映射
        var stepAgentMap = {};
        (proj.workflow_binding.step_assignments||[]).forEach(function(sa){
          stepAgentMap[sa.step_index] = sa.agent_id;
        });
        wfStepsEl.innerHTML = wfTasks.map(function(t) {
          var isDone = t.status === 'done';
          var isInProgress = t.status === 'in_progress';
          var statusIcon = isDone ? '☑️' : isInProgress ? '⏳' : '○';
          var stepName = t.title.replace(/^\[WF Step \d+\]\s*/, '');
          var stepNum = parseInt((t.title.match(/\[WF Step (\d+)\]/)||[])[1])||0;
          var stepIdx = stepNum - 1;
          var agentId = stepAgentMap[stepIdx] || t.assigned_to || '';
          var ag = agents.find(function(a){ return a.id === agentId; });
          var agentName = ag ? ag.name : (agentId || '—');
          var barColor = isDone ? 'var(--success, #4caf50)' : isInProgress ? 'var(--warning, #ff9800)' : 'rgba(255,255,255,0.1)';
          var toggleBtn = isDone ?
            '<span class="material-symbols-outlined" style="font-size:14px;cursor:pointer;color:var(--success,#4caf50)" onclick="toggleWfStep(\''+projId+'\',\''+t.id+'\',\'todo\')" title="标记为未完成">check_circle</span>' :
            '<span class="material-symbols-outlined" style="font-size:14px;cursor:pointer;color:var(--text3);opacity:0.5" onclick="toggleWfStep(\''+projId+'\',\''+t.id+'\',\'done\')" title="标记为完成">radio_button_unchecked</span>';
          var md = t.metadata || {};
          var pendingApproval = !!md.pending_approval;
          var approvalBar = pendingApproval
            ? '<div style="display:flex;align-items:center;gap:6px;margin-top:6px;padding:6px 8px;background:rgba(59,130,246,0.10);border:1px dashed #3b82f6;border-radius:4px">' +
                '<span style="font-size:10px;color:#3b82f6;flex:1">⏸️ 等待人工批准后启动</span>' +
                '<button onclick="approveWfStep(\''+projId+'\',\''+t.id+'\')" style="font-size:10px;background:#22c55e;color:#000;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-weight:600">✓ 批准启动</button>' +
              '</div>'
            : '';
          var borderExtra = pendingApproval ? ';border-color:rgba(59,130,246,0.4)' : '';
          return '<div style="background:var(--surface);border-radius:8px;padding:8px 10px;border:1px solid rgba(255,255,255,0.05)'+borderExtra+';font-size:11px;border-left:3px solid '+barColor+'">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;gap:6px">' +
              toggleBtn +
              '<span style="flex:1;font-weight:600;color:'+(isDone?'var(--text3)':'var(--text)')+';'+(isDone?'text-decoration:line-through':'')+'">' +esc(stepName)+'</span>' +
              '<span style="font-size:10px;color:var(--primary);white-space:nowrap">'+esc(agentName)+'</span>' +
            '</div>' +
            (t.description ? '<div style="color:var(--text3);font-size:10px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(t.description.split('\\n')[0])+'</div>' : '') +
            approvalBar +
          '</div>';
        }).join('') || '<div style="font-size:12px;color:var(--text3)">No workflow steps</div>';

        // 进度条
        var doneCount = wfTasks.filter(function(t){ return t.status==='done'; }).length;
        var pct = wfTasks.length > 0 ? Math.round(doneCount/wfTasks.length*100) : 0;
        wfStepsEl.insertAdjacentHTML('afterbegin',
          '<div style="margin-bottom:8px">' +
            '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-bottom:4px"><span>Progress</span><span>'+doneCount+'/'+wfTasks.length+' ('+pct+'%)</span></div>' +
            '<div style="height:4px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden"><div style="height:100%;width:'+pct+'%;background:var(--primary);border-radius:2px;transition:width 0.3s"></div></div>' +
          '</div>'
        );
      }
    } else {
      // 原始 Milestones 显示
      var milestonesEl = document.getElementById('project-milestones-'+projId);
      if (milestonesEl) {
        milestonesEl.innerHTML = (proj.milestones||[]).map(function(m) {
          var statusIcon = m.status==='confirmed'?'✅':m.status==='completed'?'☑️':m.status==='rejected'?'❌':m.status==='in_progress'?'⏳':'○';
          var ag = agents.find(function(a){ return a.id === m.responsible_agent_id; });
          var responsible = ag ? ag.name : (m.responsible_agent_id||'—');
          var dueDate = m.due_date ? new Date(m.due_date).toLocaleDateString() : '—';
          var actions = '';
          if (m.status === 'completed') {
            // Agent 已声明完成，等待 admin 确认
            actions = '<div style="display:flex;gap:4px;margin-top:6px" onclick="event.stopPropagation()">' +
              '<button class="btn btn-primary btn-xs" onclick="confirmMilestone(\''+projId+'\',\''+m.id+'\')" style="font-size:10px;padding:2px 8px">确认</button>' +
              '<button class="btn btn-ghost btn-xs" style="color:var(--error);font-size:10px;padding:2px 8px" onclick="rejectMilestone(\''+projId+'\',\''+m.id+'\')">驳回</button>' +
              '</div>';
          }
          var reasonLine = m.status==='rejected' && m.rejected_reason
            ? '<div style="color:var(--error);font-size:10px;margin-top:2px">驳回原因: '+esc(m.rejected_reason)+'</div>' : '';
          return '<div style="background:var(--surface);border-radius:8px;padding:8px 10px;border:1px solid rgba(255,255,255,0.05);font-size:11px;cursor:pointer" onclick="showMilestoneUpdateModal(\''+projId+'\',\''+m.id+'\',\''+esc(m.name).replace(/'/g,"\\\'")+'\',\''+m.status+'\')" title="Click to update">' +
            '<div style="font-weight:600">'+statusIcon+' '+esc(m.name)+'</div>' +
            '<div style="color:var(--text3);font-size:10px;margin-top:2px">'+esc(responsible)+' · '+dueDate+'</div>' +
            reasonLine + actions +
          '</div>';
        }).join('') || '<div style="font-size:12px;color:var(--text3)">No milestones yet</div>';
      }
    }

    // Populate tasks（绑定 workflow 时过滤掉 WF Step 任务，避免重复显示）
    var tasksEl = document.getElementById('project-tasks-'+projId);
    if (tasksEl) {
      var displayTasks = (proj.tasks||[]).filter(function(t){
        return !proj.workflow_binding || t.title.indexOf('[WF Step') !== 0;
      });
      tasksEl.innerHTML = displayTasks.map(function(t) {
        var statusIcon = t.status==='done'?'✅':t.status==='in_progress'?'⏳':t.status==='blocked'?'🚫':'📋';
        var ag = agents.find(function(a){ return a.id === t.assigned_to; });
        var assignee = ag ? ag.name : (t.assigned_to||'Unassigned');
        var stepsHtml = '';
        var steps = t.steps || [];
        var hasReview = steps.some(function(s){ return s.status === 'awaiting_review'; });
        if (steps.length > 0) {
          stepsHtml = '<div style="margin-top:6px;display:flex;flex-direction:column;gap:3px">' +
            steps.map(function(s, idx) {
              var sIcon = ({
                pending:'⚪', in_progress:'⏳', awaiting_review:'🔵',
                done:'✅', failed:'❌', skipped:'⏭️'
              })[s.status] || '⚪';
              var sColor = s.status==='awaiting_review' ? '#3b82f6'
                         : s.status==='done' ? 'var(--text3)'
                         : s.status==='failed' ? '#ef4444' : 'var(--text2)';
              var reviewBadge = s.manual_review
                ? '<span title="此步骤需要人工审核" style="font-size:9px;background:rgba(59,130,246,0.15);color:#3b82f6;padding:1px 5px;border-radius:3px;margin-left:4px">👤 人工</span>'
                : '';
              var actionBtns = '';
              if (s.status === 'awaiting_review') {
                actionBtns = '<button onclick="reviewStep(\''+esc(projId)+'\',\''+esc(t.id)+'\',\''+esc(s.id)+'\',\'approve\')" style="font-size:10px;background:#22c55e;color:#000;border:none;border-radius:3px;padding:2px 6px;cursor:pointer;margin-left:4px;font-weight:600">✓ 通过</button>' +
                              '<button onclick="reviewStep(\''+esc(projId)+'\',\''+esc(t.id)+'\',\''+esc(s.id)+'\',\'reject\')" style="font-size:10px;background:rgba(239,68,68,0.2);color:#ef4444;border:1px solid #ef4444;border-radius:3px;padding:2px 6px;cursor:pointer;margin-left:2px;font-weight:600">✗ 驳回</button>';
              }
              var draftPreview = '';
              if (s.status === 'awaiting_review' && s.result) {
                draftPreview = '<div style="font-size:10px;color:var(--text3);margin-left:18px;margin-top:2px;padding:4px 6px;background:rgba(59,130,246,0.06);border-left:2px solid #3b82f6;border-radius:2px;max-height:60px;overflow:hidden">'+esc(String(s.result).slice(0,200))+(s.result.length>200?'…':'')+'</div>';
              }
              return '<div style="font-size:11px;color:'+sColor+'">' +
                '<span>'+sIcon+' '+(idx+1)+'. '+esc(s.name)+'</span>'+reviewBadge+actionBtns +
                draftPreview +
              '</div>';
            }).join('') +
          '</div>';
        }
        var editBtn = '<button onclick="editTaskSteps(\''+esc(projId)+'\',\''+esc(t.id)+'\')" title="编辑步骤" style="font-size:10px;background:transparent;color:var(--text3);border:1px solid rgba(255,255,255,0.1);border-radius:3px;padding:1px 6px;cursor:pointer;margin-left:6px">⚙️ 步骤</button>';
        var pausedBadge = hasReview ? '<span style="font-size:9px;background:rgba(59,130,246,0.18);color:#3b82f6;padding:1px 6px;border-radius:3px;margin-left:6px">⏸ 等待审核</span>' : '';
        return '<div style="background:var(--surface);border-radius:8px;padding:8px 10px;border:1px solid '+(hasReview?'rgba(59,130,246,0.4)':'rgba(255,255,255,0.05)')+';font-size:12px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
            '<span style="font-weight:600">'+statusIcon+' '+esc(t.title)+pausedBadge+editBtn+'</span>' +
            '<span style="font-size:10px;color:var(--text3)">→ '+esc(assignee)+'</span>' +
          '</div>' +
          stepsHtml +
        '</div>';
      }).join('') || '<div style="font-size:12px;color:var(--text3)">No tasks yet</div>';
    }

    // Load chat history (lazy — only if chat tab active, but load anyway so tab-switch is instant)
    loadProjectChat(projId);
    // Load the active (default) tab content
    loadProjectTabContent(projId, _activeTab);
  } catch(e) { c.innerHTML = '<div style="padding:24px;color:var(--error)">Error: '+e.message+'</div>'; }
}

// ── Project detail tab switching ──
window._projectDetailTab = window._projectDetailTab || {};
function switchProjectTab(projId, tabKey) {
  window._projectDetailTab[projId] = tabKey;
  var keys = ['overview','goals','milestones','deliverables','issues','chat'];
  keys.forEach(function(k){
    var el = document.getElementById('proj-pane-'+k+'-'+projId);
    if (el) el.style.display = (k === tabKey) ? (k === 'chat' ? 'flex' : 'block') : 'none';
  });
  // Re-render tab bar highlighting (quick hack: re-run detail render)
  renderProjectDetail(projId);
}

async function loadProjectTabContent(projId, tabKey) {
  if (tabKey === 'chat') return;  // chat already bootstrapped
  var pane = document.getElementById('proj-pane-'+tabKey+'-'+projId);
  if (!pane) return;
  try {
    if (tabKey === 'overview') {
      var data = await api('GET', '/api/portal/projects/'+projId+'/overview');
      pane.innerHTML = _renderProjectOverview(projId, data);
    } else if (tabKey === 'goals') {
      var r = await api('GET', '/api/portal/projects/'+projId+'/goals');
      pane.innerHTML = _renderProjectGoals(projId, r.goals || []);
    } else if (tabKey === 'milestones') {
      var r2 = await api('GET', '/api/portal/projects/'+projId+'/milestones');
      pane.innerHTML = _renderProjectMilestonesTab(projId, r2.milestones || []);
    } else if (tabKey === 'deliverables') {
      var r3 = await api('GET', '/api/portal/projects/'+projId+'/deliverables-by-agent');
      pane.innerHTML = _renderProjectDeliverables(projId, r3);
    } else if (tabKey === 'issues') {
      var r4 = await api('GET', '/api/portal/projects/'+projId+'/issues');
      pane.innerHTML = _renderProjectIssues(projId, r4.issues || []);
    }
  } catch(e) {
    pane.innerHTML = '<div style="color:var(--error)">Error: '+esc(e.message)+'</div>';
  }
}

function _pctBar(pct) {
  pct = Math.max(0, Math.min(100, pct||0));
  var color = pct >= 100 ? '#22c55e' : pct >= 50 ? 'var(--primary)' : '#f59e0b';
  return '<div style="height:6px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden"><div style="height:100%;width:'+pct+'%;background:'+color+';transition:width 0.3s"></div></div>';
}

function _renderProjectOverview(projId, d) {
  var gs = d.goal_summary || {}; var ds = d.deliverable_summary || {}; var is = d.issue_summary || {}; var ts = d.task_summary || {};
  var goals = d.goals || []; var deliverables = d.deliverables || []; var issues = d.issues || [];
  var recentGoals = goals.slice(0,5).map(function(g){
    return '<div style="background:var(--surface);border-radius:8px;padding:10px 12px;border:1px solid rgba(255,255,255,0.05);margin-bottom:8px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-weight:600;font-size:13px">'+esc(g.name||'(未命名目标)')+'</span><span style="font-size:11px;color:var(--text3)">'+(g.progress||0).toFixed(1)+'%</span></div>' +
      _pctBar(g.progress||0) +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:12px">暂无目标，点击 Goals 标签创建</div>';
  var pending = deliverables.filter(function(x){return x.status==='submitted';});
  var pendingHtml = pending.slice(0,5).map(function(x){
    return '<div style="background:var(--surface);border-radius:8px;padding:8px 12px;border:1px solid rgba(255,255,255,0.05);margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">' +
      '<div><div style="font-weight:600;font-size:13px">'+esc(x.title)+'</div><div style="font-size:10px;color:var(--text3)">v'+x.version+' · '+esc(x.kind)+'</div></div>' +
      '<div style="display:flex;gap:4px">' +
        '<button class="btn btn-primary btn-xs" onclick="reviewDeliverable(\''+projId+'\',\''+x.id+'\',true)">通过</button>' +
        '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="reviewDeliverable(\''+projId+'\',\''+x.id+'\',false)">驳回</button>' +
      '</div>' +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:12px">没有等待审阅的交付件</div>';
  var openIssues = issues.filter(function(x){return x.status==='open'||x.status==='investigating';});
  var issuesHtml = openIssues.slice(0,5).map(function(x){
    var sevColor = x.severity==='critical'?'#ef4444':x.severity==='high'?'#f59e0b':x.severity==='medium'?'var(--primary)':'var(--text3)';
    return '<div style="background:var(--surface);border-radius:8px;padding:8px 12px;border:1px solid rgba(255,255,255,0.05);margin-bottom:6px;border-left:3px solid '+sevColor+'">' +
      '<div style="font-weight:600;font-size:13px">'+esc(x.title)+'</div>' +
      '<div style="font-size:10px;color:var(--text3);margin-top:2px">'+esc(x.severity)+' · '+esc(x.status)+(x.assigned_to?' · → '+esc(x.assigned_to):'')+'</div>' +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:12px">目前没有未解决的问题</div>';
  var statCard = function(label, val, sub){
    return '<div style="background:var(--surface);border-radius:8px;padding:14px 16px;border:1px solid rgba(255,255,255,0.05)">' +
      '<div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:0.6px">'+label+'</div>' +
      '<div style="font-size:22px;font-weight:700;color:var(--text);margin-top:4px">'+val+'</div>' +
      (sub?'<div style="font-size:10px;color:var(--text3);margin-top:2px">'+sub+'</div>':'') +
    '</div>';
  };
  return '<div style="max-width:1100px">' +
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px">' +
      statCard('目标完成', (gs.done||0)+'/'+(gs.total||0), '均值 '+(gs.avg_progress||0)+'%') +
      statCard('交付件等待审阅', (ds.submitted||0)+'', '总 '+(ds.total||0)+' · ✅'+(ds.approved||0)) +
      statCard('未解决问题', (is.open||0)+'', '总 '+(is.total||0)) +
      statCard('进行中任务', (ts.in_progress||0)+'', 'TODO '+(ts.todo||0)+' · DONE '+(ts.done||0)) +
    '</div>' +
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">' +
      '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:10px">目标概览 (Top 5)</div>'+recentGoals+'</div>' +
      '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:10px">待审阅交付件</div>'+pendingHtml+'</div>' +
    '</div>' +
    '<div style="margin-top:20px"><div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text3);margin-bottom:10px">未解决问题</div>'+issuesHtml+'</div>' +
  '</div>';
}

function _renderProjectGoals(projId, goals) {
  var rows = (goals||[]).map(function(g){
    var pct = g.progress||0;
    var metricLabel = g.metric==='count' ? (g.current_value+' / '+g.target_value) :
                      g.metric==='percent' ? (g.current_value+' / '+g.target_value+'%') :
                      g.metric==='boolean' ? (g.done?'✅ 已达成':'⬜ 未达成') :
                      esc(g.target_text||'');
    return '<div style="background:var(--surface);border-radius:10px;padding:14px 16px;border:1px solid rgba(255,255,255,0.06);margin-bottom:10px">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-weight:700;font-size:14px;color:var(--text)">'+esc(g.name||'(未命名)')+'</div>' +
          (g.description?'<div style="color:var(--text3);font-size:12px;margin-top:3px">'+esc(g.description)+'</div>':'') +
          '<div style="font-size:11px;color:var(--text2);margin-top:6px">指标: '+esc(g.metric)+' · '+metricLabel+(g.owner_agent_id?' · 负责: '+esc(g.owner_agent_id):'')+'</div>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">' +
          '<span style="font-size:18px;font-weight:700;color:'+(pct>=100?'#22c55e':'var(--primary)')+'">'+pct.toFixed(0)+'%</span>' +
          '<button class="btn btn-ghost btn-xs" onclick="updateGoalProgressPrompt(\''+projId+'\',\''+g.id+'\',\''+g.metric+'\')">更新进度</button>' +
          '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="deleteGoal(\''+projId+'\',\''+g.id+'\')">删除</button>' +
        '</div>' +
      '</div>' +
      '<div style="margin-top:10px">'+_pctBar(pct)+'</div>' +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:13px;padding:20px;text-align:center">暂无目标。点击"新建目标"来为本项目设立可度量目标。</div>';
  return '<div style="max-width:900px">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:16px;font-weight:700">项目目标</div>' +
      '<button class="btn btn-primary btn-sm" onclick="showAddGoalModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建目标</button>' +
    '</div>' + rows +
  '</div>';
}

function _renderProjectMilestonesTab(projId, milestones) {
  var rows = (milestones||[]).map(function(m){
    var statusIcon = m.status==='confirmed'?'✅':m.status==='completed'?'☑️':m.status==='rejected'?'❌':m.status==='in_progress'?'⏳':'○';
    var actions = '';
    if (m.status === 'completed') {
      actions = '<div style="display:flex;gap:6px;margin-top:8px">' +
        '<button class="btn btn-primary btn-xs" onclick="confirmMilestone(\''+projId+'\',\''+m.id+'\')">确认</button>' +
        '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="rejectMilestone(\''+projId+'\',\''+m.id+'\')">驳回</button>' +
      '</div>';
    }
    return '<div style="background:var(--surface);border-radius:10px;padding:14px 16px;border:1px solid rgba(255,255,255,0.06);margin-bottom:10px">' +
      '<div style="font-weight:700;font-size:14px">'+statusIcon+' '+esc(m.name||'')+'</div>' +
      '<div style="font-size:11px;color:var(--text3);margin-top:4px">负责: '+esc(m.responsible_agent_id||'—')+' · 到期: '+esc(m.due_date||'—')+' · 状态: '+esc(m.status||'pending')+'</div>' +
      (m.rejected_reason?'<div style="color:var(--error);font-size:11px;margin-top:4px">驳回原因: '+esc(m.rejected_reason)+'</div>':'') +
      actions +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:13px;padding:20px;text-align:center">暂无里程碑</div>';
  return '<div style="max-width:900px">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:16px;font-weight:700">里程碑</div>' +
      '<button class="btn btn-primary btn-sm" onclick="showProjectMilestoneModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建里程碑</button>' +
    '</div>' + rows +
  '</div>';
}

function _renderProjectDeliverables(projId, data) {
  data = data || {};
  var agents = data.agents || [];
  var unassigned = data.unassigned_deliverables || [];
  var statusBadge = function(s){
    var map = {draft:['草稿','var(--text3)'], submitted:['待审阅','#f59e0b'], approved:['已通过','#22c55e'], rejected:['已驳回','#ef4444'], archived:['归档','var(--text3)']};
    var x = map[s]||[s,'var(--text3)'];
    return '<span style="font-size:10px;background:rgba(255,255,255,0.08);color:'+x[1]+';padding:2px 8px;border-radius:10px;font-weight:600">'+x[0]+'</span>';
  };
  var fmtSize = function(n){
    if (!n && n !== 0) return '';
    if (n < 1024) return n+'B';
    if (n < 1024*1024) return (n/1024).toFixed(1)+'KB';
    if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1)+'MB';
    return (n/1024/1024/1024).toFixed(2)+'GB';
  };
  var fmtTime = function(ts){
    if (!ts) return '';
    try { var d = new Date(ts*1000); return d.toLocaleString(); } catch(e){ return ''; }
  };
  var renderDeliverableCard = function(dv){
    var actions = '';
    if (dv.status === 'submitted') {
      actions = '<button class="btn btn-primary btn-xs" onclick="reviewDeliverable(\''+projId+'\',\''+dv.id+'\',true)">通过</button>' +
                '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="reviewDeliverable(\''+projId+'\',\''+dv.id+'\',false)">驳回</button>';
    } else if (dv.status === 'draft') {
      actions = '<button class="btn btn-primary btn-xs" onclick="submitDeliverable(\''+projId+'\',\''+dv.id+'\')">提交审阅</button>';
    }
    var preview = dv.content_text ? '<div style="font-size:12px;color:var(--text2);margin-top:6px;max-height:72px;overflow:hidden;line-height:1.5">'+esc(dv.content_text.slice(0,300))+(dv.content_text.length>300?'…':'')+'</div>' : '';
    var link = dv.url ? '<a href="'+esc(dv.url)+'" target="_blank" style="font-size:11px;color:var(--primary)">'+esc(dv.url)+'</a>' : (dv.file_path?'<span style="font-size:11px;color:var(--text3)">📎 '+esc(dv.file_path)+'</span>':'');
    var reviewComment = dv.review_comment ? '<div style="margin-top:6px;font-size:11px;color:var(--text3);padding:6px 8px;background:rgba(255,255,255,0.03);border-left:2px solid '+(dv.status==='approved'?'#22c55e':'#ef4444')+';border-radius:3px">审阅意见: '+esc(dv.review_comment)+'</div>' : '';
    return '<div style="background:var(--surface);border-radius:10px;padding:12px 14px;border:1px solid rgba(255,255,255,0.06);margin-bottom:8px">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="display:flex;align-items:center;gap:8px"><span style="font-weight:700;font-size:13px">'+esc(dv.title)+'</span>'+statusBadge(dv.status)+'<span style="font-size:10px;color:var(--text3)">v'+dv.version+'</span></div>' +
          '<div style="font-size:11px;color:var(--text3);margin-top:3px">'+esc(dv.kind||'')+'</div>' +
          preview + (link?'<div style="margin-top:6px">'+link+'</div>':'') + reviewComment +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">'+actions+
          '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="deleteDeliverable(\''+projId+'\',\''+dv.id+'\')">删除</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  };
  var renderFileRow = function(f){
    var icon = f.is_remote ? '🔗' : '📄';
    var meta = [];
    if (f.size) meta.push(fmtSize(f.size));
    if (f.mtime) meta.push(fmtTime(f.mtime));
    if (f.kind) meta.push(esc(f.kind));
    var name = f.url
      ? '<a href="'+esc(f.url)+'" target="_blank" style="color:var(--primary);text-decoration:none;font-weight:600;font-size:13px">'+esc(f.name||f.rel_path||f.path||'(unnamed)')+'</a>'
      : '<span style="font-size:13px;font-weight:600">'+esc(f.name||'(unnamed)')+'</span>';
    var rel = f.rel_path && f.rel_path !== f.name ? '<div style="font-size:10px;color:var(--text3);margin-top:2px">'+esc(f.rel_path)+'</div>' : '';
    return '<div style="display:flex;align-items:flex-start;gap:10px;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.04)">' +
      '<div style="font-size:14px;line-height:1">'+icon+'</div>' +
      '<div style="flex:1;min-width:0">'+name+rel+
        (meta.length?'<div style="font-size:10px;color:var(--text3);margin-top:2px">'+meta.join(' · ')+'</div>':'') +
      '</div>' +
    '</div>';
  };
  var agentSections = (agents||[]).map(function(ag){
    var explicit = (ag.explicit_deliverables||[]).map(renderDeliverableCard).join('');
    var files = (ag.files||[]).map(renderFileRow).join('');
    var emptyHint = (!explicit && !files) ? '<div style="color:var(--text3);font-size:12px;padding:14px;text-align:center">该 Agent 暂无交付物</div>' : '';
    var dirHint = ag.deliverable_dir ? '<div style="font-size:10px;color:var(--text3);margin-top:2px;font-family:monospace">📁 '+esc(ag.deliverable_dir)+'</div>' : '';
    return '<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:12px;padding:14px;margin-bottom:14px">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:10px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:14px;font-weight:700">👤 '+esc(ag.agent_name||ag.agent_id)+(ag.role?' <span style="font-size:11px;color:var(--text3);font-weight:400">('+esc(ag.role)+')</span>':'')+'</div>' +
          dirHint +
        '</div>' +
        '<div style="font-size:10px;color:var(--text3);white-space:nowrap">📄 '+(ag.file_count||0)+' · 📋 '+(ag.explicit_count||0)+'</div>' +
      '</div>' +
      (explicit ? '<div style="margin-bottom:8px">'+explicit+'</div>' : '') +
      (files ? '<div style="background:var(--surface);border-radius:8px;overflow:hidden">'+files+'</div>' : '') +
      emptyHint +
    '</div>';
  }).join('');
  var unassignedSection = '';
  if (unassigned && unassigned.length) {
    unassignedSection = '<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:12px;padding:14px;margin-bottom:14px">' +
      '<div style="font-size:14px;font-weight:700;margin-bottom:10px">❓ 未指派作者</div>' +
      unassigned.map(renderDeliverableCard).join('') +
    '</div>';
  }
  var body = (agentSections || unassignedSection)
    ? (agentSections + unassignedSection)
    : '<div style="color:var(--text3);font-size:13px;padding:20px;text-align:center">暂无交付件</div>';
  return '<div style="max-width:900px">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:16px;font-weight:700">交付件 (按 Agent 分组)</div>' +
      '<button class="btn btn-primary btn-sm" onclick="showAddDeliverableModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建交付件</button>' +
    '</div>' + body +
  '</div>';
}

function _renderProjectIssues(projId, items) {
  var sevColor = function(s){ return s==='critical'?'#ef4444':s==='high'?'#f59e0b':s==='medium'?'var(--primary)':'var(--text3)'; };
  var rows = (items||[]).map(function(iss){
    var resolved = (iss.status==='resolved'||iss.status==='wontfix');
    var actions = resolved ? '' :
      '<button class="btn btn-primary btn-xs" onclick="resolveIssuePrompt(\''+projId+'\',\''+iss.id+'\')">标记解决</button>';
    var resLine = iss.resolution ? '<div style="margin-top:6px;font-size:11px;color:#22c55e;padding:6px 8px;background:rgba(34,197,94,0.08);border-radius:3px">✓ '+esc(iss.resolution)+'</div>' : '';
    return '<div style="background:var(--surface);border-radius:10px;padding:14px 16px;border:1px solid rgba(255,255,255,0.06);margin-bottom:10px;border-left:3px solid '+sevColor(iss.severity)+'">' +
      '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-weight:700;font-size:14px">'+esc(iss.title)+(resolved?' <span style="font-size:10px;color:var(--text3)">['+esc(iss.status)+']</span>':'')+'</div>' +
          (iss.description?'<div style="font-size:12px;color:var(--text2);margin-top:4px">'+esc(iss.description)+'</div>':'') +
          '<div style="font-size:11px;color:var(--text3);margin-top:6px">严重度: '+esc(iss.severity)+' · 状态: '+esc(iss.status)+(iss.assigned_to?' · → '+esc(iss.assigned_to):'')+'</div>' +
          resLine +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">'+actions+
          '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="deleteIssue(\''+projId+'\',\''+iss.id+'\')">删除</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('') || '<div style="color:var(--text3);font-size:13px;padding:20px;text-align:center">暂无问题记录</div>';
  return '<div style="max-width:900px">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:16px;font-weight:700">问题 / 风险</div>' +
      '<button class="btn btn-primary btn-sm" onclick="showAddIssueModal(\''+projId+'\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建问题</button>' +
    '</div>' + rows +
  '</div>';
}

// ── Goal CRUD helpers ──
async function _fetchProjectMembers(projId) {
  try {
    var proj = await api('GET', '/api/portal/projects/'+projId);
    var members = (proj && proj.members) || [];
    // Enrich with agent name/role from agents API
    try {
      var agData = await api('GET', '/api/portal/agents');
      var agMap = {};
      ((agData && agData.agents) || []).forEach(function(a){ agMap[a.id] = a; });
      members.forEach(function(m){
        var ag = agMap[m.agent_id];
        if (ag) { m.agent_name = ag.name || m.agent_id; m.role = ag.role || ''; }
      });
    } catch(e) {}
    return members;
  } catch(e) { return []; }
}
function _memberOptions(members, selected) {
  var html = '<option value="">-- 不指派 --</option>';
  html += '<option value="__user__"'+(selected==='__user__'?' selected':'')+'>用户 (User)</option>';
  (members||[]).forEach(function(m){
    var aid = m.agent_id || '';
    var label = (m.agent_name || aid) + (m.role ? ' ('+m.role+')' : '');
    html += '<option value="'+esc(aid)+'"'+(selected===aid?' selected':'')+'>'+esc(label)+'</option>';
  });
  return html;
}
var _modalInputStyle = 'width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px;box-sizing:border-box';

async function showAddGoalModal(projId) {
  var members = await _fetchProjectMembers(projId);
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:520px;width:94%">'
    + '<h3 style="margin:0 0 14px">新建目标</h3>'
    + '<div style="display:flex;flex-direction:column;gap:10px">'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">目标名称 *</label>'
    + '<input id="goal-name" style="'+_modalInputStyle+'" placeholder="例如：完成核心功能开发"></div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">描述</label>'
    + '<textarea id="goal-desc" rows="2" style="'+_modalInputStyle+'" placeholder="可选描述"></textarea></div>'
    + '<div style="display:flex;gap:10px">'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">度量类型</label>'
    + '<select id="goal-metric" style="'+_modalInputStyle+'" onchange="_goalMetricChanged()">'
    + '<option value="count">计数 (count)</option><option value="percent">百分比 (percent)</option>'
    + '<option value="boolean">是/否 (boolean)</option><option value="text">文本 (text)</option></select></div>'
    + '<div style="flex:1" id="goal-target-wrap"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">目标值</label>'
    + '<input id="goal-target" type="number" value="100" style="'+_modalInputStyle+'"></div>'
    + '</div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">负责人</label>'
    + '<select id="goal-owner" style="'+_modalInputStyle+'">'+_memberOptions(members,'__user__')+'</select></div>'
    + '</div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="_submitGoal(\''+projId+'\',this)">创建</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var el = document.getElementById('goal-name'); if (el) el.focus(); }, 0);
}
function _goalMetricChanged() {
  var m = document.getElementById('goal-metric').value;
  var wrap = document.getElementById('goal-target-wrap');
  if (m === 'boolean') {
    wrap.innerHTML = '<label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">完成状态</label><div style="font-size:12px;color:var(--text3)">创建后通过"更新进度"标记</div>';
  } else if (m === 'text') {
    wrap.innerHTML = '<label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">目标描述</label><input id="goal-target-text" style="'+_modalInputStyle+'" placeholder="目标文本">';
  } else {
    wrap.innerHTML = '<label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">目标值</label><input id="goal-target" type="number" value="100" style="'+_modalInputStyle+'">';
  }
}
async function _submitGoal(projId, btn) {
  var name = (document.getElementById('goal-name').value||'').trim();
  if (!name) { alert('请输入目标名称'); return; }
  var metric = document.getElementById('goal-metric').value;
  var targetV = 0, targetT = '';
  if (metric === 'count' || metric === 'percent') {
    var el = document.getElementById('goal-target');
    targetV = el ? parseFloat(el.value||'0') : 0;
  } else if (metric === 'text') {
    var el2 = document.getElementById('goal-target-text');
    targetT = el2 ? el2.value : '';
  }
  var owner = document.getElementById('goal-owner').value;
  if (owner === '__user__') owner = '';
  btn.disabled = true;
  await api('POST', '/api/portal/projects/'+projId+'/goals', {
    name: name,
    description: (document.getElementById('goal-desc').value||'').trim(),
    metric: metric, target_value: targetV, target_text: targetT,
    owner_agent_id: owner,
  });
  btn.closest('div[style*=fixed]').remove();
  loadProjectTabContent(projId, 'goals');
}
function updateGoalProgressPrompt(projId, goalId, metric) {
  if (metric === 'boolean') {
    var ok = confirm('标记为已达成？');
    api('POST', '/api/portal/projects/'+projId+'/goals/'+goalId+'/progress', {done: ok})
      .then(function(){ loadProjectTabContent(projId, 'goals'); });
  } else {
    var v = parseFloat(prompt('当前进度值:', '0') || 'NaN');
    if (isNaN(v)) return;
    api('POST', '/api/portal/projects/'+projId+'/goals/'+goalId+'/progress', {current_value: v})
      .then(function(){ loadProjectTabContent(projId, 'goals'); });
  }
}
function deleteGoal(projId, goalId) {
  if (!confirm('删除此目标？')) return;
  api('POST', '/api/portal/projects/'+projId+'/goals/'+goalId+'/delete', {})
    .then(function(){ loadProjectTabContent(projId, 'goals'); });
}

// ── Deliverable CRUD helpers ──
async function showAddDeliverableModal(projId) {
  var members = await _fetchProjectMembers(projId);
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:520px;width:94%">'
    + '<h3 style="margin:0 0 14px">新建交付件</h3>'
    + '<div style="display:flex;flex-direction:column;gap:10px">'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">标题 *</label>'
    + '<input id="dv-title" style="'+_modalInputStyle+'" placeholder="交付件标题"></div>'
    + '<div style="display:flex;gap:10px">'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">类型</label>'
    + '<select id="dv-kind" style="'+_modalInputStyle+'">'
    + '<option value="document">文档 (document)</option><option value="code">代码 (code)</option>'
    + '<option value="analysis">分析 (analysis)</option><option value="url">链接 (url)</option>'
    + '<option value="media">媒体 (media)</option><option value="other">其他 (other)</option></select></div>'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">作者 Agent *</label>'
    + '<select id="dv-author" style="'+_modalInputStyle+'">'+_memberOptions(members,'')+'</select></div>'
    + '</div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">内容摘要</label>'
    + '<textarea id="dv-content" rows="3" style="'+_modalInputStyle+'" placeholder="交付件摘要或说明"></textarea></div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">URL / 文件路径</label>'
    + '<input id="dv-url" style="'+_modalInputStyle+'" placeholder="可选：链接地址或文件路径"></div>'
    + '</div>'
    + '<div style="font-size:11px;color:var(--text3);margin-top:8px">交付件创建后为"草稿"状态。由作者 Agent 提交后进入"待审阅"，用户审核通过/驳回。</div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="_submitDeliverable(\''+projId+'\',this)">创建</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var el = document.getElementById('dv-title'); if (el) el.focus(); }, 0);
}
async function _submitDeliverable(projId, btn) {
  var title = (document.getElementById('dv-title').value||'').trim();
  if (!title) { alert('请输入标题'); return; }
  var author = document.getElementById('dv-author').value;
  if (author === '__user__') author = '';
  var urlVal = (document.getElementById('dv-url').value||'').trim();
  btn.disabled = true;
  await api('POST', '/api/portal/projects/'+projId+'/deliverables', {
    title: title,
    kind: document.getElementById('dv-kind').value,
    author_agent_id: author,
    content_text: (document.getElementById('dv-content').value||'').trim(),
    url: urlVal,
    file_path: urlVal.startsWith('http') ? '' : urlVal,
  });
  btn.closest('div[style*=fixed]').remove();
  loadProjectTabContent(projId, 'deliverables');
}
function submitDeliverable(projId, dvId) {
  api('POST', '/api/portal/projects/'+projId+'/deliverables/'+dvId+'/submit', {})
    .then(function(){ loadProjectTabContent(projId, 'deliverables'); });
}
function reviewDeliverable(projId, dvId, approved) {
  var label = approved ? '通过' : '驳回';
  var color = approved ? '#22c55e' : '#ef4444';
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:480px;width:94%">'
    + '<h3 style="margin:0 0 12px;color:'+color+'">审阅: '+label+'交付件</h3>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">审阅意见</label>'
    + '<textarea id="review-comment" rows="3" style="'+_modalInputStyle+'" placeholder="'+(approved?'可选：通过意见':'请说明驳回原因，Agent 将收到通知并修订')+'"></textarea></div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" style="background:'+color+'" onclick="_submitReview(\''+projId+'\',\''+dvId+'\','+approved+',this)">确认'+label+'</button>'
    + '</div></div>';
  document.body.appendChild(modal);
}
async function _submitReview(projId, dvId, approved, btn) {
  btn.disabled = true;
  await api('POST', '/api/portal/projects/'+projId+'/deliverables/'+dvId+'/review', {
    approved: approved,
    comment: (document.getElementById('review-comment').value||'').trim(),
  });
  btn.closest('div[style*=fixed]').remove();
  var active = (window._projectDetailTab && window._projectDetailTab[projId]) || 'deliverables';
  loadProjectTabContent(projId, active);
}
function deleteDeliverable(projId, dvId) {
  if (!confirm('删除此交付件？')) return;
  api('POST', '/api/portal/projects/'+projId+'/deliverables/'+dvId+'/delete', {})
    .then(function(){ loadProjectTabContent(projId, 'deliverables'); });
}

// ── Issue CRUD helpers ──
async function showAddIssueModal(projId) {
  var members = await _fetchProjectMembers(projId);
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:520px;width:94%">'
    + '<h3 style="margin:0 0 14px">新建问题</h3>'
    + '<div style="display:flex;flex-direction:column;gap:10px">'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">问题标题 *</label>'
    + '<input id="iss-title" style="'+_modalInputStyle+'" placeholder="简要描述问题"></div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">详细描述</label>'
    + '<textarea id="iss-desc" rows="3" style="'+_modalInputStyle+'" placeholder="问题的详细描述、复现步骤等"></textarea></div>'
    + '<div style="display:flex;gap:10px">'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">严重度</label>'
    + '<select id="iss-severity" style="'+_modalInputStyle+'">'
    + '<option value="low">低 (low)</option><option value="medium" selected>中 (medium)</option>'
    + '<option value="high">高 (high)</option><option value="critical">严重 (critical)</option></select></div>'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">报告人</label>'
    + '<select id="iss-reporter" style="'+_modalInputStyle+'">'
    + '<option value="__user__" selected>用户 (User)</option>'
    + (members||[]).map(function(m){ var aid=m.agent_id||''; var label=(m.agent_name||m.name||aid)+(m.role?' ('+m.role+')':''); return '<option value="'+esc(aid)+'">'+esc(label)+'</option>'; }).join('')
    + '</select></div>'
    + '</div>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">指派责任人 Agent</label>'
    + '<select id="iss-assigned" style="'+_modalInputStyle+'">'+_memberOptions(members,'')+'</select>'
    + '<div style="font-size:10px;color:var(--text3);margin-top:2px">指派后该 Agent 将负责修复此问题</div></div>'
    + '</div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="_submitIssue(\''+projId+'\',this)">创建</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var el = document.getElementById('iss-title'); if (el) el.focus(); }, 0);
}
async function _submitIssue(projId, btn) {
  var title = (document.getElementById('iss-title').value||'').trim();
  if (!title) { alert('请输入问题标题'); return; }
  var reporter = document.getElementById('iss-reporter').value;
  if (reporter === '__user__') reporter = 'user';
  var assigned = document.getElementById('iss-assigned').value;
  if (assigned === '__user__') assigned = '';
  btn.disabled = true;
  await api('POST', '/api/portal/projects/'+projId+'/issues', {
    title: title,
    description: (document.getElementById('iss-desc').value||'').trim(),
    severity: document.getElementById('iss-severity').value,
    reporter: reporter,
    assigned_to: assigned,
  });
  btn.closest('div[style*=fixed]').remove();
  loadProjectTabContent(projId, 'issues');
}
function resolveIssuePrompt(projId, issId) {
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:480px;width:94%">'
    + '<h3 style="margin:0 0 12px">标记问题已解决</h3>'
    + '<div><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">解决方案描述</label>'
    + '<textarea id="resolve-text" rows="3" style="'+_modalInputStyle+'" placeholder="描述解决方案"></textarea></div>'
    + '<div style="display:flex;gap:10px;margin-top:10px">'
    + '<div style="flex:1"><label style="font-size:12px;color:var(--text2);display:block;margin-bottom:4px">状态</label>'
    + '<select id="resolve-status" style="'+_modalInputStyle+'">'
    + '<option value="resolved">已解决 (resolved)</option><option value="wontfix">不修复 (wontfix)</option></select></div></div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="_submitResolve(\''+projId+'\',\''+issId+'\',this)">确认</button>'
    + '</div></div>';
  document.body.appendChild(modal);
}
async function _submitResolve(projId, issId, btn) {
  btn.disabled = true;
  await api('POST', '/api/portal/projects/'+projId+'/issues/'+issId+'/resolve', {
    resolution: (document.getElementById('resolve-text').value||'').trim(),
    status: document.getElementById('resolve-status').value,
  });
  btn.closest('div[style*=fixed]').remove();
  loadProjectTabContent(projId, 'issues');
}
function deleteIssue(projId, issId) {
  if (!confirm('删除此问题？')) return;
  api('POST', '/api/portal/projects/'+projId+'/issues/'+issId+'/delete', {})
    .then(function(){ loadProjectTabContent(projId, 'issues'); });
}

/* ── Rich content renderer for project chat ── */
function _renderRichContent(text) {
  var s = esc(text);
  // ── Markdown tables: | col | col | → <table>
  var lines = s.split('\n');
  var out = [], i = 0;
  while (i < lines.length) {
    // Detect table: line starts with |
    if (/^\|.+\|$/.test(lines[i].trim())) {
      var tableLines = [];
      while (i < lines.length && /^\|.+\|$/.test(lines[i].trim())) {
        tableLines.push(lines[i].trim());
        i++;
      }
      if (tableLines.length >= 2) {
        var thtml = '<div style="overflow-x:auto;margin:8px 0"><table style="border-collapse:collapse;font-size:12px;width:100%">';
        tableLines.forEach(function(row, ri) {
          // Skip separator row (|---|---|)
          if (/^\|[\s\-:]+\|$/.test(row)) return;
          var cells = row.split('|').filter(function(c,ci){ return ci > 0 && ci < row.split('|').length - 1; });
          var tag = ri === 0 ? 'th' : 'td';
          var bgStyle = ri === 0 ? 'background:rgba(203,201,255,0.1);font-weight:700' : '';
          thtml += '<tr>' + cells.map(function(c){
            return '<'+tag+' style="padding:6px 10px;border:1px solid rgba(255,255,255,0.1);text-align:left;'+bgStyle+'">'+c.trim()+'</'+tag+'>';
          }).join('') + '</tr>';
        });
        thtml += '</table></div>';
        out.push(thtml);
      } else {
        tableLines.forEach(function(l){ out.push(l); });
      }
      continue;
    }
    out.push(lines[i]);
    i++;
  }
  s = out.join('\n');
  // ── Code blocks: ```...``` → <pre><code>
  s = s.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, lang, code) {
    return '<pre style="background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:10px;margin:8px 0;overflow-x:auto;font-size:12px;font-family:monospace"><code>' + code + '</code></pre>';
  });
  // ── Inline code: `...`
  s = s.replace(/`([^`]+)`/g, '<code style="background:rgba(0,0,0,0.25);padding:1px 5px;border-radius:3px;font-size:12px;font-family:monospace">$1</code>');
  // ── Bold: **text** or __text__
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__(.+?)__/g, '<strong>$1</strong>');
  // ── Italic: *text* (not preceded by *)
  s = s.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  // ── Headings: # ## ###
  s = s.replace(/^### (.+)$/gm, '<div style="font-size:14px;font-weight:700;margin:8px 0 4px">$1</div>');
  s = s.replace(/^## (.+)$/gm, '<div style="font-size:15px;font-weight:700;margin:10px 0 4px">$1</div>');
  s = s.replace(/^# (.+)$/gm, '<div style="font-size:16px;font-weight:700;margin:12px 0 6px">$1</div>');
  // ── Bullet lists: - item or * item
  s = s.replace(/^[\-\*] (.+)$/gm, '<div style="padding-left:12px;position:relative"><span style="position:absolute;left:0">•</span> $1</div>');
  // ── Numbered lists: 1. item
  s = s.replace(/^(\d+)\. (.+)$/gm, '<div style="padding-left:16px;position:relative"><span style="position:absolute;left:0;color:var(--primary);font-weight:600">$1.</span> $2</div>');
  // ── Images: ![alt](url) → <img>, but DROP non-image extensions.
  // Matches the _renderSimpleMarkdown rule used in agent chat: a stray
  // ![foo.mp4](...) reference is silently dropped — the corresponding
  // FileCard will appear via the per-message `refs` enrichment, not as
  // a permanently broken <img> tag.
  s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function(_m, alt, src){
    var rawSrc = String(src || '').trim();
    var lower = rawSrc.toLowerCase().split('?')[0].split('#')[0];
    var imageExts = ['.png','.jpg','.jpeg','.gif','.svg','.webp','.bmp','.avif','.ico'];
    var isImage = false;
    for (var i = 0; i < imageExts.length; i++) {
      if (lower.endsWith(imageExts[i])) { isImage = true; break; }
    }
    if (!isImage && /^data:image\//i.test(rawSrc)) isImage = true;
    if (!isImage) return '';
    var safeAlt = String(alt || '').replace(/"/g, '&quot;');
    return '<div style="margin:8px 0"><img src="' + rawSrc + '" alt="' + safeAlt + '" style="max-width:100%;border-radius:8px;border:1px solid rgba(255,255,255,0.1)" onerror="this.style.display=\'none\'" /><div style="font-size:10px;color:var(--text3);margin-top:2px">' + safeAlt + '</div></div>';
  });
  // ── Links: [text](url)
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:var(--primary);text-decoration:underline">$1</a>');
  // ── Attachments: 📎filename.ext or [attachment:filename]
  s = s.replace(/📎(\S+)/g, '<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(203,201,255,0.1);padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer"><span class="material-symbols-outlined" style="font-size:14px">attach_file</span>$1</span>');
  s = s.replace(/\[attachment:([^\]]+)\]/g, '<span style="display:inline-flex;align-items:center;gap:4px;background:rgba(203,201,255,0.1);padding:3px 8px;border-radius:4px;font-size:11px;cursor:pointer"><span class="material-symbols-outlined" style="font-size:14px">attach_file</span>$1</span>');
  // ── Audio: [audio:url] or 🎤 voice message
  s = s.replace(/\[audio:([^\]]+)\]/g, '<div style="margin:6px 0"><audio controls style="width:100%;max-width:300px;height:32px"><source src="$1"></audio></div>');
  // ── Horizontal rule: ---
  s = s.replace(/^---$/gm, '<hr style="border:none;border-top:1px solid rgba(255,255,255,0.1);margin:8px 0">');
  // ── Emojis: checkboxes ✅ 🔴 🟢
  // ── @mentions highlight
  s = s.replace(/@(\S+)/g, '<span style="color:var(--primary);font-weight:600;background:rgba(203,201,255,0.08);padding:0 3px;border-radius:3px">@$1</span>');
  // ── Newlines → <br> (for remaining plain text)
  s = s.replace(/\n/g, '<br>');
  return s;
}

async function loadProjectChat(projId) {
  try {
    var data = await api('GET', '/api/portal/projects/'+projId+'/chat?limit=200');
    var el = document.getElementById('project-chat-msgs-'+projId);
    if (!el) return;
    el.innerHTML = '';
    (data.messages||[]).forEach(function(m) {
      var isUser = m.sender === 'user';
      var div = document.createElement('div');
      div.style.cssText = 'max-width:85%;padding:12px 16px;border-radius:12px;font-size:13px;line-height:1.6;word-wrap:break-word;overflow-wrap:break-word;' +
        (isUser ? 'align-self:flex-end;background:linear-gradient(135deg,#1a2332,#243447);color:#e0e6ed;border:1px solid rgba(255,255,255,0.08);border-bottom-right-radius:4px' :
                  'align-self:flex-start;background:var(--surface);border:1px solid rgba(255,255,255,0.05);border-bottom-left-radius:4px');
      // 时间戳格式化
      var timeStr = '';
      if (m.timestamp) {
        var d = new Date(m.timestamp * 1000);
        timeStr = (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0');
      }
      var header = '';
      if (!isUser) {
        header = '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">' +
          '<span style="font-size:10px;font-weight:700;color:var(--primary)">'+esc(m.sender_name||m.sender)+'</span>' +
          (timeStr ? '<span style="font-size:9px;color:var(--text3);opacity:0.6">'+timeStr+'</span>' : '') +
        '</div>';
      } else if (timeStr) {
        header = '<div style="text-align:right;margin-bottom:4px"><span style="font-size:9px;color:var(--text3);opacity:0.5">'+timeStr+'</span></div>';
      }
      var badge = m.msg_type === 'task_update' ? '<span style="font-size:9px;background:rgba(210,153,34,0.15);color:var(--warning);padding:1px 5px;border-radius:3px;margin-left:4px">TASK</span>' : '';
      div.innerHTML = header + badge + '<div class="rich-msg-body">' + _renderRichContent(m.content) + '</div>';
      // Per-message FileCards: backend has extracted local-path / URL
      // file references and attached them as `m.refs`. Reuse the same
      // _appendFileCards helper the agent chat uses, so the visual
      // shape and dedup behaviour stay consistent across surfaces.
      try {
        if (m.refs && m.refs.length) {
          // _appendFileCards expects a "msgDiv" with a child container
          // selectable as `.chat-file-cards`. Our project bubble doesn't
          // use the same selector, so create a wrapper that does.
          var cardHost = document.createElement('div');
          cardHost.className = 'chat-msg-content';
          div.appendChild(cardHost);
          _appendFileCards(cardHost, m.refs);
        }
      } catch(e) { console.log('[projectChat] file card attach failed', e); }
      el.appendChild(div);
    });
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

// ── Chat attachments (P1 #3) ──
var _projAttachments = {};  // { [projId]: [{name, mime, size, data_base64, preview_url}] }

function _projAttachList(projId) {
  if (!_projAttachments[projId]) _projAttachments[projId] = [];
  return _projAttachments[projId];
}

function _renderProjAttachPreview(projId) {
  var box = document.getElementById('proj-attach-preview-'+projId);
  if (!box) return;
  var list = _projAttachList(projId);
  if (list.length === 0) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = list.map(function(a, idx) {
    var thumb = '';
    if (a.preview_url) {
      thumb = '<img src="'+a.preview_url+'" style="width:36px;height:36px;object-fit:cover;border-radius:4px">';
    } else {
      thumb = '<span class="material-symbols-outlined" style="font-size:20px;color:var(--text3)">draft</span>';
    }
    var sizeKb = Math.max(1, Math.round((a.size||0)/1024));
    return '<div style="display:inline-flex;align-items:center;gap:6px;background:var(--surface);border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:4px 8px;font-size:11px;color:var(--text)">' +
             thumb +
             '<div style="display:flex;flex-direction:column;max-width:140px">' +
               '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(a.name)+'</span>' +
               '<span style="color:var(--text3);font-size:10px">'+sizeKb+' KB</span>' +
             '</div>' +
             '<button onclick="removeProjAttach(\''+projId+'\','+idx+')" title="Remove" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:2px"><span class="material-symbols-outlined" style="font-size:14px">close</span></button>' +
           '</div>';
  }).join('');
}

function removeProjAttach(projId, idx) {
  var list = _projAttachList(projId);
  if (idx >= 0 && idx < list.length) {
    list.splice(idx, 1);
    _renderProjAttachPreview(projId);
  }
}

function handleProjAttach(projId, fileInput) {
  if (!fileInput || !fileInput.files || fileInput.files.length === 0) return;
  var list = _projAttachList(projId);
  var MAX = 10;
  var MAX_SIZE = 10 * 1024 * 1024;  // 10 MB per file
  var remaining = MAX - list.length;
  var files = Array.prototype.slice.call(fileInput.files, 0, Math.max(0, remaining));
  files.forEach(function(f) {
    if (f.size > MAX_SIZE) {
      alert('File "'+f.name+'" exceeds 10 MB and was skipped.');
      return;
    }
    var reader = new FileReader();
    reader.onload = function(e) {
      var dataUrl = e.target.result || '';
      var b64 = dataUrl.indexOf(',') >= 0 ? dataUrl.split(',')[1] : dataUrl;
      var isImage = (f.type || '').indexOf('image/') === 0;
      list.push({
        name: f.name,
        mime: f.type || 'application/octet-stream',
        size: f.size,
        data_base64: b64,
        preview_url: isImage ? dataUrl : '',
      });
      _renderProjAttachPreview(projId);
    };
    reader.readAsDataURL(f);
  });
  fileInput.value = '';  // allow re-picking the same file
}

var _projectChatPoll = null;
async function sendProjectMsg(projId) {
  var input = document.getElementById('project-chat-input-'+projId);
  if (!input) return;
  var text = input.value.trim();
  var attachments = _projAttachList(projId).slice();
  if (!text && attachments.length === 0) return;
  input.value = '';
  // Clear pending attachments immediately (we captured a snapshot above)
  _projAttachments[projId] = [];
  _renderProjAttachPreview(projId);
  // Add user bubble immediately
  var el = document.getElementById('project-chat-msgs-'+projId);
  if (el) {
    var div = document.createElement('div');
    div.style.cssText = 'max-width:80%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;align-self:flex-end;background:linear-gradient(135deg,#1a2332,#243447);color:#e0e6ed;border:1px solid rgba(255,255,255,0.08);border-bottom-right-radius:4px';
    div.textContent = text;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }
  // Send to backend
  try {
    // Parse @mentions to extract target agents
    var mentionedAgents = [];
    var mentionRegex = /@(\S+)/g;
    var match;
    while ((match = mentionRegex.exec(text)) !== null) {
      var mentioned = match[1];
      // Match against agent names or role-name patterns
      var found = agents.find(function(a) {
        return a.name === mentioned || (a.role+'-'+a.name) === mentioned || a.id === mentioned;
      });
      if (found && mentionedAgents.indexOf(found.id) < 0) mentionedAgents.push(found.id);
    }
    var chatBody = {content: text};
    if (mentionedAgents.length > 0) chatBody.target_agents = mentionedAgents;
    if (attachments.length > 0) {
      chatBody.attachments = attachments.map(function(a){
        return {name: a.name, mime: a.mime, size: a.size, data_base64: a.data_base64};
      });
    }
    await api('POST', '/api/portal/projects/'+projId+'/chat', chatBody);
    // Poll for new messages periodically while agents respond
    if (_projectChatPoll) clearInterval(_projectChatPoll);
    var pollCount = 0;
    _projectChatPoll = setInterval(function() {
      loadProjectChat(projId);
      pollCount++;
      if (pollCount > 30) clearInterval(_projectChatPoll);  // Stop after 30 polls (~1min)
    }, 2000);
  } catch(e) { alert('Send failed: '+e.message); }
}

async function editProject(projId, currentName, currentDesc) {
  // 获取项目详情和可用 workflow 模板
  var proj = await api('GET', '/api/portal/projects/'+projId);
  var tmplData = [];
  try {
    var td = await api('GET', '/api/portal/workflows');
    // 只取模板（status=template）
    tmplData = (td.workflows || []).filter(function(w){ return w.status === 'template'; });
  } catch(e) {}

  var currentWfId = (proj.workflow_binding && proj.workflow_binding.workflow_id) || '';
  var currentAssignments = (proj.workflow_binding && proj.workflow_binding.step_assignments) || [];

  // 构建模态弹窗
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.onclick = function(e){ if(e.target===overlay) overlay.remove(); };

  var wfOptions = '<option value="">— 不绑定 Workflow —</option>' +
    tmplData.map(function(t){ return '<option value="'+t.id+'"'+(t.id===currentWfId?' selected':'')+'>'+esc(t.name)+' ('+((t.steps||[]).length)+' steps)</option>'; }).join('');

  var box = document.createElement('div');
  box.style.cssText = 'background:var(--bg);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:24px;width:520px;max-height:80vh;overflow-y:auto;color:var(--text)';
  box.innerHTML =
    '<div style="font-size:16px;font-weight:700;margin-bottom:16px">编辑项目</div>' +
    '<label style="font-size:12px;color:var(--text3)">项目名称</label>' +
    '<input id="ep-name" type="text" value="'+esc(proj.name).replace(/"/g,'&quot;')+'" style="width:100%;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:8px 10px;color:var(--text);font-size:13px;margin:4px 0 12px">' +
    '<label style="font-size:12px;color:var(--text3)">项目描述</label>' +
    '<textarea id="ep-desc" rows="2" style="width:100%;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:8px 10px;color:var(--text);font-size:13px;margin:4px 0 12px;resize:vertical">'+esc(proj.description||'')+'</textarea>' +
    '<label style="font-size:12px;color:var(--text3)">绑定 Workflow</label>' +
    '<select id="ep-workflow" style="width:100%;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:8px 10px;color:var(--text);font-size:13px;margin:4px 0 12px">'+wfOptions+'</select>' +
    '<div id="ep-step-assignments" style="margin-bottom:16px"></div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
      '<button class="btn btn-ghost btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>' +
      '<button class="btn btn-primary btn-sm" id="ep-save-btn">保存</button>' +
    '</div>';

  overlay.appendChild(box);
  document.body.appendChild(overlay);

  // 当 workflow 选择变化时渲染 step → agent 分配表
  var wfSelect = document.getElementById('ep-workflow');
  function renderStepAssignments() {
    var container = document.getElementById('ep-step-assignments');
    var selWfId = wfSelect.value;
    if (!selWfId) { container.innerHTML = ''; return; }
    var tmpl = tmplData.find(function(t){ return t.id === selWfId; });
    if (!tmpl) { container.innerHTML = ''; return; }
    var steps = tmpl.steps || [];
    var agentOpts = '<option value="">— 未分配 —</option>' +
      agents.map(function(a){ return '<option value="'+a.id+'">'+esc(a.name)+'</option>'; }).join('');
    container.innerHTML = '<div style="font-size:12px;color:var(--text3);margin-bottom:6px">步骤 → Agent 分配（☑ = 该步骤启动前需人工批准）</div>' +
      steps.map(function(s, i) {
        // 尝试回填当前分配
        var curAgent = '';
        var curApproval = false;
        if (selWfId === currentWfId) {
          var ca = currentAssignments.find(function(a){ return a.step_index === i; });
          if (ca) { curAgent = ca.agent_id; curApproval = !!ca.require_approval; }
        }
        var opts = agentOpts.replace('value="'+curAgent+'"', 'value="'+curAgent+'" selected');
        return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
          '<span style="font-size:11px;color:var(--text);min-width:120px;font-weight:600">Step '+(i+1)+': '+esc(s.name||'')+'</span>' +
          '<select class="ep-agent-sel" data-step="'+i+'" style="flex:1;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 8px;color:var(--text);font-size:12px">'+opts+'</select>' +
          '<label title="启动前需人工批准" style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text3);cursor:pointer;white-space:nowrap">' +
            '<input type="checkbox" class="ep-approval-chk" data-step="'+i+'"'+(curApproval?' checked':'')+' style="cursor:pointer">审核' +
          '</label>' +
        '</div>';
      }).join('');
  }
  wfSelect.onchange = renderStepAssignments;
  renderStepAssignments();

  // 保存
  document.getElementById('ep-save-btn').onclick = async function() {
    var name = document.getElementById('ep-name').value.trim();
    var desc = document.getElementById('ep-desc').value.trim();
    var wfId = wfSelect.value;
    var stepAssignments = [];
    var approvalMap = {};
    document.querySelectorAll('.ep-approval-chk').forEach(function(chk){
      approvalMap[parseInt(chk.getAttribute('data-step'))] = chk.checked;
    });
    document.querySelectorAll('.ep-agent-sel').forEach(function(sel){
      var idx = parseInt(sel.getAttribute('data-step'));
      var aid = sel.value;
      if (aid) stepAssignments.push({
        step_index: idx, agent_id: aid,
        require_approval: !!approvalMap[idx],
      });
    });
    var payload = {action:'update', project_id:projId, name:name, description:desc};
    // 只有选择了 workflow 或者显式清除时才传
    if (wfId !== currentWfId || stepAssignments.length > 0) {
      payload.workflow_id = wfId;
      payload.step_assignments = stepAssignments;
    }
    await api('POST', '/api/portal/projects', payload);
    overlay.remove();
    renderCurrentView();
  };
}

async function toggleWfStep(projId, taskId, newStatus) {
  try {
    await api('POST', '/api/portal/projects/'+projId+'/task-update', {task_id: taskId, status: newStatus});
    renderProjectDetail(projId);
  } catch(e) { alert('更新失败: '+e.message); }
}

async function approveWfStep(projId, taskId) {
  if (!confirm('确认批准该步骤启动？\n批准后将立即唤醒负责 Agent 开始执行。')) return;
  try {
    await api('POST', '/api/portal/projects/'+projId+'/tasks/'+taskId+'/approve-step', {});
    renderProjectDetail(projId);
  } catch(e) { alert('批准失败: '+e.message); }
}

async function deleteProject(projId) {
  if (!confirm('确定要删除这个项目吗？所有聊天记录和任务将丢失。')) return;
  await api('POST', '/api/portal/projects', {action:'delete', project_id:projId});
  currentView = 'projects';
  renderCurrentView();
}

async function deleteProjectTask(projId, taskId) {
  if (!confirm('确定要删除这个任务吗？')) return;
  await api('POST', '/api/portal/projects/'+projId+'/task-update', {task_id:taskId, status:'deleted'});
  renderProjectDetail(projId);
}

// ── Task step editor (with manual_review checkbox) ──
async function editTaskSteps(projId, taskId) {
  // Fetch current task to load existing steps
  var proj = null;
  try {
    proj = await api('GET', '/api/portal/projects/'+projId);
  } catch(e) { alert('Load project failed: '+e.message); return; }
  var task = (proj.tasks||[]).find(function(t){ return t.id === taskId; });
  if (!task) { alert('Task not found'); return; }
  var existing = (task.steps||[]).map(function(s){
    return { name: s.name, manual_review: !!s.manual_review, _locked: s.status === 'done' || s.status === 'awaiting_review' };
  });

  // Build modal
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';
  var modal = document.createElement('div');
  modal.style.cssText = 'background:var(--surface);border:1px solid var(--border-light);border-radius:12px;padding:20px;width:min(560px,90vw);max-height:80vh;overflow-y:auto;font-family:inherit';
  function rowHtml(idx, name, mr, locked) {
    var lockNote = locked ? '<span style="font-size:10px;color:var(--text3);margin-left:6px">(已完成/审核中，不可修改)</span>' : '';
    return '<div data-row="'+idx+'" style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding:8px;background:var(--surface2);border-radius:6px">' +
      '<span style="color:var(--text3);font-size:12px;width:18px">'+(idx+1)+'.</span>' +
      '<input type="text" data-step-name value="'+esc(name).replace(/"/g,'&quot;')+'" '+(locked?'readonly':'')+' style="flex:1;background:var(--surface);color:var(--text);border:1px solid rgba(255,255,255,0.1);border-radius:4px;padding:6px 8px;font-size:13px" placeholder="步骤名称">' +
      '<label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text2);white-space:nowrap;cursor:pointer">' +
        '<input type="checkbox" data-step-review '+(mr?'checked':'')+' '+(locked?'disabled':'')+' style="cursor:pointer">人工审核' +
      '</label>' +
      (locked ? '' : '<button onclick="this.closest(\'[data-row]\').remove()" style="background:transparent;color:#ef4444;border:none;cursor:pointer;font-size:14px">✕</button>') +
      lockNote +
    '</div>';
  }
  var html = '<div style="font-size:16px;font-weight:700;margin-bottom:6px">编辑任务步骤</div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:14px">勾选「人工审核」后，agent 完成该 step 时只会产出草稿，必须由你点击「通过」才能继续后续步骤。</div>' +
    '<div id="step-rows-container">' +
      (existing.length === 0 ? '' : existing.map(function(s, i){ return rowHtml(i, s.name, s.manual_review, s._locked); }).join('')) +
    '</div>' +
    '<button id="add-step-btn" style="background:transparent;color:var(--primary);border:1px dashed var(--primary);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px;margin-bottom:12px">+ 添加步骤</button>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">' +
      '<button id="cancel-step-btn" class="btn btn-ghost" style="font-size:12px">取消</button>' +
      '<button id="save-step-btn" class="btn btn-primary" style="font-size:12px">保存</button>' +
    '</div>';
  modal.innerHTML = html;
  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  function nextIdx() {
    return modal.querySelectorAll('[data-row]').length;
  }
  modal.querySelector('#add-step-btn').onclick = function(){
    var i = nextIdx();
    var tmp = document.createElement('div');
    tmp.innerHTML = rowHtml(i, '', false, false);
    modal.querySelector('#step-rows-container').appendChild(tmp.firstChild);
  };
  modal.querySelector('#cancel-step-btn').onclick = function(){ document.body.removeChild(overlay); };
  modal.querySelector('#save-step-btn').onclick = async function(){
    var rows = modal.querySelectorAll('[data-row]');
    var steps = [];
    for (var i = 0; i < rows.length; i++) {
      var nameEl = rows[i].querySelector('[data-step-name]');
      var mrEl = rows[i].querySelector('[data-step-review]');
      var n = (nameEl.value || '').trim();
      if (!n) continue;
      steps.push({ name: n, manual_review: !!mrEl.checked });
    }
    if (steps.length === 0) {
      if (!confirm('未填写任何步骤，将清空当前任务的所有 step（task 退化为单次执行）。继续？')) return;
    }
    try {
      await api('POST', '/api/portal/projects/'+projId+'/task-steps', {
        task_id: taskId, steps: steps,
      });
      document.body.removeChild(overlay);
      renderProjectDetail(projId);
    } catch(e) { alert('保存失败: '+e.message); }
  };
}

// ── Manual review action: approve / reject an awaiting_review step ──
async function reviewStep(projId, taskId, stepId, action) {
  var body = { task_id: taskId, step_id: stepId, action: action };
  if (action === 'reject') {
    var reason = prompt('驳回原因（可选，会作为提示传给 agent 重新执行）:', '');
    if (reason === null) return;
    body.reason = reason;
  } else {
    var override = prompt('确认通过这个 step。\n如需修改 agent 的草稿结果，请在下方编辑（留空保持原样）:', '');
    if (override === null) return;
    if (override) body.result = override;
  }
  try {
    await api('POST', '/api/portal/projects/'+projId+'/task-step-review', body);
    // Refresh whichever view we're in
    if (currentView === 'dashboard') {
      renderDashboard();
    } else {
      renderProjectDetail(projId);
    }
  } catch(e) { alert('审核失败: '+e.message); }
}

async function pauseProject(projId) {
  var reason = prompt('暂停原因（可选）:', '') || '';
  await api('POST', '/api/portal/projects/'+projId+'/pause', {reason:reason});
  renderProjectDetail(projId);
}

async function resumeProject(projId) {
  var r = await api('POST', '/api/portal/projects/'+projId+'/resume', {});
  if (r && r.replayed) {
    console.log('恢复后已回放', r.replayed, '条暂停期间消息');
  }
  renderProjectDetail(projId);
}

async function changeProjectStatus(projId, newStatus) {
  console.log('[changeProjectStatus]', projId, '->', newStatus);
  var labelMap = {
    planning: '未开始', active: '进行中', suspended: '挂起',
    cancelled: '停止', completed: '结束', archived: '归档',
  };
  var label = labelMap[newStatus] || newStatus;
  var needReason = (newStatus === 'cancelled' || newStatus === 'suspended' || newStatus === 'completed');
  var reason = '';
  if (needReason) {
    var r1 = prompt('请输入『' + label + '』的原因（可留空，回车即可）:', '');
    if (r1 === null) return;  // user cancelled
    reason = r1;
  } else {
    if (!confirm('确认将项目状态变更为『' + label + '』?')) return;
  }
  try {
    var r = await api('POST', '/api/portal/projects/'+projId+'/status', {status:newStatus, reason:reason});
    console.log('[changeProjectStatus] response', r);
    if (!r) {
      alert('变更失败: 空响应，请检查服务器日志');
      return;
    }
    if (r.error) {
      alert('变更失败: ' + r.error);
      return;
    }
    if (currentView === 'project_detail') {
      renderProjectDetail(projId);
    } else {
      renderProjects();
    }
  } catch(e) {
    console.error('[changeProjectStatus] error', e);
    alert('变更失败: ' + e);
  }
}

async function confirmMilestone(projId, msId) {
  await api('POST', '/api/portal/projects/'+projId+'/milestones/'+msId+'/confirm', {});
  renderProjectDetail(projId);
}

async function rejectMilestone(projId, msId) {
  var reason = prompt('驳回原因:', '') || '';
  await api('POST', '/api/portal/projects/'+projId+'/milestones/'+msId+'/reject', {reason:reason});
  renderProjectDetail(projId);
}

// ============ Workflows ============
var _wfCatalogFilter = '';

async function renderWorkflows() {
  var c = document.getElementById('content');
  c.style.padding = '24px';
  try {
    var data = await api('GET', '/api/portal/workflows');
    var wfs = data.workflows || [];
    // Also load catalog
    var catalogData = {};
    try { catalogData = await api('GET', '/api/portal/workflow-catalog'); } catch(e) {}
    var catalog = catalogData.catalog || [];
    var categories = catalogData.categories || {};

    var html = '<div style="max-width:1200px;margin:0 auto">';

    // ── Header ──
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">';
    html += '<div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:24px;font-weight:800;margin:0">Workflows</h2>';
    html += '<p style="font-size:12px;color:var(--text3);margin-top:4px">选择模板快速创建，或自定义工作流流程</p></div>';
    html += '<button class="btn btn-primary btn-sm" onclick="showCreateWorkflowModal()"><span class="material-symbols-outlined" style="font-size:14px">add</span> 自定义创建</button>';
    html += '</div>';

    // ── My Workflows (已创建的) ──
    if (wfs.length) {
      html += '<div style="margin-bottom:28px">';
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px"><span class="material-symbols-outlined" style="font-size:20px;color:var(--primary)">account_tree</span><span style="font-size:16px;font-weight:700">我的工作流</span><span style="font-size:11px;color:var(--text3);background:var(--surface3);padding:2px 8px;border-radius:10px">' + wfs.length + '</span></div>';
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px">';
      wfs.forEach(function(wf) {
        var stepsArr = wf.steps || [];
        var stepsPreview = stepsArr.slice(0,5).map(function(s, i) {
          return '<span style="padding:2px 8px;border-radius:4px;background:var(--surface3);font-size:10px;color:var(--text2);white-space:nowrap">' + (i+1) + '. ' + esc(s.name||'Step '+(i+1)) + '</span>';
        }).join('<span style="color:var(--text3);font-size:10px;margin:0 2px">&rarr;</span>');
        if (stepsArr.length > 5) stepsPreview += '<span style="font-size:10px;color:var(--text3);margin-left:4px">+' + (stepsArr.length - 5) + ' more</span>';

        html += '<div class="card" style="background:var(--surface);border-radius:12px;padding:18px;border:1px solid rgba(255,255,255,0.06)">' +
          '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">' +
            '<div style="display:flex;align-items:center;gap:8px"><span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">account_tree</span><span style="font-size:15px;font-weight:700">' + esc(wf.name) + '</span></div>' +
            '<span style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(203,201,255,0.1);color:var(--primary);font-weight:700">TEMPLATE</span>' +
          '</div>' +
          '<div style="font-size:12px;color:var(--text3);margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">' + esc(wf.description||'No description') + '</div>' +
          '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin-bottom:12px">' + (stepsPreview || '<span style="font-size:11px;color:var(--text3)">No steps</span>') + '</div>' +
          '<div style="display:flex;align-items:center;justify-content:space-between">' +
            '<span style="font-size:11px;color:var(--text3)">' + stepsArr.length + ' steps</span>' +
            '<div style="display:flex;gap:6px">' +
              '<button class="btn btn-sm" style="font-size:10px" onclick="editWorkflow(\'' + wf.id + '\')"><span class="material-symbols-outlined" style="font-size:12px">edit</span></button>' +
              '<button class="btn btn-sm" style="font-size:10px;color:var(--error)" onclick="deleteWorkflow(\'' + wf.id + '\')"><span class="material-symbols-outlined" style="font-size:12px">delete</span></button>' +
            '</div>' +
          '</div>' +
        '</div>';
      });
      html += '</div></div>';
    }

    // ── Catalog Section ──
    if (catalog.length) {
      html += '<div style="margin-bottom:20px">';
      html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">';
      html += '<div style="display:flex;align-items:center;gap:8px"><span class="material-symbols-outlined" style="font-size:20px;color:#FF9800">auto_awesome</span><span style="font-size:16px;font-weight:700">模板市场</span><span style="font-size:11px;color:var(--text3)">Template Catalog</span></div>';
      html += '</div>';

      // Category filter tabs
      var catNames = Object.keys(categories);
      html += '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px">';
      html += '<button class="btn btn-sm" id="wf-cat-all" style="font-size:11px;' + (!_wfCatalogFilter ? 'background:var(--primary);color:#282572;font-weight:700' : '') + '" onclick="filterWfCatalog(\'\')">全部 (' + catalog.length + ')</button>';
      catNames.forEach(function(cat) {
        var cnt = categories[cat].length;
        var active = _wfCatalogFilter === cat;
        html += '<button class="btn btn-sm" style="font-size:11px;' + (active ? 'background:var(--primary);color:#282572;font-weight:700' : '') + '" onclick="filterWfCatalog(\'' + esc(cat) + '\')">' + esc(cat) + ' (' + cnt + ')</button>';
      });
      html += '</div>';

      // Catalog cards
      html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px" id="wf-catalog-grid">';
      catalog.forEach(function(t) {
        var hidden = _wfCatalogFilter && t.category !== _wfCatalogFilter;
        var tags = (t.tags || []).map(function(tag) {
          return '<span style="font-size:9px;padding:2px 6px;border-radius:4px;background:rgba(255,152,0,0.1);color:#FF9800">' + esc(tag) + '</span>';
        }).join('');
        html += '<div class="card wf-catalog-card" data-category="' + esc(t.category||'') + '" style="background:var(--surface);border-radius:12px;padding:18px;border:1px solid rgba(255,255,255,0.06);cursor:pointer;transition:border-color 0.15s,transform 0.15s;' + (hidden ? 'display:none' : '') + '" onmouseenter="this.style.borderColor=\'#FF9800\';this.style.transform=\'translateY(-2px)\'" onmouseleave="this.style.borderColor=\'rgba(255,255,255,0.06)\';this.style.transform=\'none\'" onclick="useWfCatalog(\'' + t.id + '\')">';
        html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">';
        html += '<div style="display:flex;align-items:center;gap:8px"><span style="font-size:20px">' + (t.icon||'📋') + '</span><span style="font-size:14px;font-weight:700">' + esc(t.name) + '</span></div>';
        html += '<span style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(255,152,0,0.1);color:#FF9800;font-weight:600">' + t.step_count + ' 步</span>';
        html += '</div>';
        html += '<div style="font-size:12px;color:var(--text3);margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">' + esc(t.description) + '</div>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:4px">' + tags + '</div>';
        html += '</div>';
      });
      html += '</div></div>';
    }

    html += '</div>';
    c.innerHTML = html;
  } catch(e) { c.innerHTML = '<div style="color:var(--error)">Error loading workflows: ' + esc(String(e)) + '</div>'; }
}

function filterWfCatalog(cat) {
  _wfCatalogFilter = cat;
  var cards = document.querySelectorAll('.wf-catalog-card');
  cards.forEach(function(card) {
    if (!cat || card.getAttribute('data-category') === cat) {
      card.style.display = '';
    } else {
      card.style.display = 'none';
    }
  });
  // Update button active state
  var btns = document.querySelectorAll('#wf-cat-all');
  // Re-render is simpler — just call renderWorkflows again
  renderWorkflows();
}

async function useWfCatalog(catalogId) {
  if (!confirm('从模板市场创建工作流？')) return;
  try {
    var data = await api('POST', '/api/portal/workflows', {
      action: 'create_from_catalog',
      catalog_id: catalogId
    });
    if (data && !data.error) {
      renderWorkflows();
    } else {
      alert((data && data.error) || 'Failed to create from catalog');
    }
  } catch(e) { alert('Error: ' + e); }
}

// ============ Self-Improvement (自我改进) ============
function _siFormatTokens(n) {
  if (!n || n === 0) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return '' + n;
}

function renderSelfImprovement() {
  var c = document.getElementById('content');
  if (!c) { console.error('renderSelfImprovement: content element not found'); return; }
  c.style.padding = '24px';

  var siCount = agents.filter(function(a){ return a.self_improvement && a.self_improvement.enabled; }).length;

  var html = '<div style="width:100%;padding:0">';

  // Header
  html += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">';
  html += '<span class="material-symbols-outlined" style="font-size:28px;color:var(--primary)">psychology</span>';
  html += '<div><h2 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:22px;font-weight:800;margin:0;line-height:1.2">Agent Self-Improvement</h2>';
  html += '<div style="color:var(--text3);font-size:12px;margin-top:2px">复盘固化 + 主动学习 → 经验库 → 检索调用 → 迭代进化</div>';
  html += '</div></div>';

  // ── Row 1: closed-loop KPI cards ──
  // Goal → Plan → Completion → Conversion
  html += '<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:18px">';
  html += '<div class="card" style="padding:14px 16px;text-align:center"><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Plans</div><div id="si-metric-plans" style="font-size:26px;font-weight:800;color:var(--primary);line-height:1">-</div><div style="font-size:10px;color:var(--text3);margin-top:4px">学习计划总数</div></div>';
  html += '<div class="card" style="padding:14px 16px;text-align:center"><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">In Progress</div><div id="si-metric-running" style="font-size:26px;font-weight:800;color:#FF9800;line-height:1">-</div><div style="font-size:10px;color:var(--text3);margin-top:4px">排队 / 进行中</div></div>';
  html += '<div class="card" style="padding:14px 16px;text-align:center"><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Completion</div><div id="si-metric-completion" style="font-size:26px;font-weight:800;color:#4CAF50;line-height:1">-</div><div style="font-size:10px;color:var(--text3);margin-top:4px">完成率</div></div>';
  html += '<div class="card" style="padding:14px 16px;text-align:center"><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Conversion</div><div id="si-metric-conversion" style="font-size:26px;font-weight:800;color:#2196F3;line-height:1">-</div><div style="font-size:10px;color:var(--text3);margin-top:4px">经验转化率</div></div>';
  html += '<div class="card" style="padding:14px 16px;text-align:center"><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Experiences</div><div id="si-metric-exp" style="font-size:26px;font-weight:800;color:var(--primary);line-height:1">-</div><div style="font-size:10px;color:var(--text3);margin-top:4px">已沉淀经验</div></div>';
  html += '</div>';
  // Secondary line: agents with SI + roles
  html += '<div style="display:flex;gap:16px;margin-bottom:18px;font-size:11px;color:var(--text3)">';
  html += '<span>自改进启用: <b style="color:var(--text1)">'+siCount+'/'+agents.length+'</b></span>';
  html += '<span>有经验的角色: <b id="si-metric-roles" style="color:var(--text1)">-</b></span>';
  html += '</div>';

  // ── Row 2: Learning Plan Board (主动学习任务看板) ──
  html += '<div class="card" style="padding:16px;margin-bottom:18px">';
  html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:12px"><span class="material-symbols-outlined" style="font-size:18px;color:#4CAF50">task_alt</span><span style="font-weight:700;font-size:14px">学习计划看板</span><span style="font-size:11px;color:var(--text3)">Learning Plan Board</span></div>';
  html += '<div id="si-plan-board" style="max-height:600px;overflow-y:auto"><div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Loading...</div></div>';
  html += '</div>';

  // ── Row 3: Retrospective Insights (复盘洞察) ──
  html += '<div class="card" style="padding:16px;margin-bottom:18px">';
  html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:12px"><span class="material-symbols-outlined" style="font-size:18px;color:#FF9800">lightbulb</span><span style="font-weight:700;font-size:14px">复盘洞察</span><span style="font-size:11px;color:var(--text3)">Retrospective Insights — 最新为准，冲突时覆盖历史</span></div>';
  html += '<div id="si-insights" style="max-height:280px;overflow-y:auto"><div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">Loading...</div></div>';
  html += '</div>';

  // ── Row 4: Retrospective + Active Learning triggers (2 columns) ──
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px">';

  // Col 1: Retrospective
  html += '<div class="card" style="padding:16px;display:flex;flex-direction:column">';
  html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px"><span class="material-symbols-outlined" style="font-size:18px;color:#FF9800">replay</span><span style="font-weight:700;font-size:14px">自我复盘</span></div>';
  html += '<div class="form-group" style="margin-bottom:8px"><label style="font-size:11px;margin-bottom:2px">Agent</label>';
  html += '<select id="si-retro-agent" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1)" onchange="siLoadAgentStatus(\'retro\')">';
  html += '<option value="">-- Select --</option>';
  agents.forEach(function(a) { html += '<option value="'+a.id+'">'+esc(a.name)+' ('+a.role+')</option>'; });
  html += '</select></div>';
  html += '<div class="form-group" style="margin-bottom:8px"><label style="font-size:11px;margin-bottom:2px">Task Summary (可选)</label>';
  html += '<textarea id="si-retro-summary" rows="2" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1);resize:vertical;box-sizing:border-box" placeholder="描述要复盘的任务..."></textarea></div>';
  html += '<div id="si-retro-status" style="font-size:11px;color:var(--text3);margin-bottom:6px;min-height:16px"></div>';
  html += '<button class="btn btn-primary" id="si-retro-btn" style="width:100%;padding:8px;font-size:13px;margin-top:auto" onclick="siTriggerRetro()"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">replay</span> Run Retrospective</button>';
  html += '</div>';

  // Col 2: Active Learning
  html += '<div class="card" style="padding:16px;display:flex;flex-direction:column">';
  html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:10px"><span class="material-symbols-outlined" style="font-size:18px;color:#4CAF50">school</span><span style="font-weight:700;font-size:14px">主动学习</span></div>';
  html += '<div class="form-group" style="margin-bottom:8px"><label style="font-size:11px;margin-bottom:2px">Agent</label>';
  html += '<select id="si-learn-agent" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1)" onchange="siLoadAgentStatus(\'learn\')">';
  html += '<option value="">-- Select --</option>';
  agents.forEach(function(a) { html += '<option value="'+a.id+'">'+esc(a.name)+' ('+a.role+')</option>'; });
  html += '</select></div>';
  html += '<div class="form-group" style="margin-bottom:8px"><label style="font-size:11px;margin-bottom:2px">Learning Goal (学习目标)</label>';
  html += '<input id="si-learn-goal" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1);box-sizing:border-box" placeholder="e.g. 学习自动化测试最佳实践"></div>';
  html += '<div class="form-group" style="margin-bottom:8px"><label style="font-size:11px;margin-bottom:2px">Knowledge Gap (可选)</label>';
  html += '<input id="si-learn-gap" style="width:100%;padding:6px 8px;font-size:12px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1);box-sizing:border-box" placeholder="e.g. 不了解pytest fixture用法"></div>';
  html += '<div id="si-learn-status" style="font-size:11px;color:var(--text3);margin-bottom:6px;min-height:16px"></div>';
  html += '<button class="btn btn-primary" id="si-learn-btn" style="width:100%;padding:8px;font-size:13px;margin-top:auto" onclick="siTriggerLearn()"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">school</span> Run Active Learning</button>';
  html += '</div>';
  html += '</div>';

  // ── Result display (hidden until triggered) ──
  html += '<div id="si-result" class="card" style="padding:16px;display:none;margin-bottom:18px">';
  html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">';
  html += '<span style="font-weight:700;font-size:14px" id="si-result-title">Result</span>';
  html += '<button class="btn btn-sm btn-ghost" style="padding:2px 6px" onclick="document.getElementById(\'si-result\').style.display=\'none\'"><span class="material-symbols-outlined" style="font-size:14px">close</span></button>';
  html += '</div>';
  html += '<div id="si-result-content"></div>';
  html += '</div>';

  // ── Row 5: Experience Library Browser ──
  html += '<div class="card" style="padding:16px;margin-bottom:18px">';
  html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">';
  html += '<div style="display:flex;align-items:center;gap:6px"><span class="material-symbols-outlined" style="font-size:18px;color:#2196F3">library_books</span><span style="font-weight:700;font-size:14px">Global Experience Library</span><span style="font-size:11px;color:var(--text3)">全局经验库</span></div>';
  html += '<div style="display:flex;gap:6px;align-items:center">';
  html += '<select id="si-lib-role" style="padding:5px 8px;font-size:11px;border:1px solid var(--border-light);border-radius:6px;background:var(--bg);color:var(--text1)" onchange="siLoadExperiences()">';
  html += '<option value="">-- Select Role --</option>';
  var knownRoles = ["ceo","cto","coder","reviewer","researcher","architect","devops","designer","pm","tester","data","general"];
  knownRoles.forEach(function(r) { html += '<option value="'+r+'">'+r.toUpperCase()+'</option>'; });
  html += '</select>';
  html += '<button class="btn btn-sm" style="padding:4px 8px" onclick="siLoadExperiences()"><span class="material-symbols-outlined" style="font-size:14px">refresh</span></button>';
  html += '</div></div>';
  html += '<div id="si-exp-list" style="max-height:400px;overflow-y:auto">';
  html += '<div style="color:var(--text3);font-size:12px;text-align:center;padding:30px">Select a role to browse experiences</div>';
  html += '</div>';
  html += '</div>';

  html += '</div>';
  c.innerHTML = html;

  // Load initial data
  siLoadStats();
  siLoadHistory();
  siLoadPlanBoard();
  siLoadInsights();
}

async function siLoadStats() {
  try {
    var data = await api('GET', '/api/portal/experience/stats');
    var el = document.getElementById('si-stats-content');
    var metricEl = document.getElementById('si-metric-exp');
    if (!data) return;
    var roles = data.roles || {};
    var total = data.total_experiences || 0;
    if (metricEl) metricEl.textContent = '' + total;
    var rolesMetricEl = document.getElementById('si-metric-roles');
    if (rolesMetricEl) rolesMetricEl.textContent = '' + Object.keys(roles).length;
    if (!el) return;
    var html = '';
    var roleKeys = Object.keys(roles).sort();
    if (roleKeys.length > 0) {
      html += '<div style="display:flex;flex-wrap:wrap;gap:6px">';
      roleKeys.forEach(function(r) {
        var info = roles[r];
        html += '<div style="background:var(--surface2);padding:4px 10px;border-radius:12px;font-size:11px;display:inline-flex;align-items:center;gap:4px">';
        html += '<span style="font-weight:700;text-transform:uppercase">'+r+'</span>';
        html += '<span style="color:var(--text2)">'+info.total+'</span>';
        if (info.core_count) html += '<span style="color:var(--primary);font-size:10px">'+info.core_count+' core</span>';
        html += '</div>';
      });
      html += '</div>';
    } else {
      html += '<span style="color:var(--text3)">No experiences yet. Enable self-improvement on an agent to get started.</span>';
    }
    el.innerHTML = html;
  } catch(e) { console.error('siLoadStats error:', e); }
}

function siLoadAgentStatus(type) {
  var agentId = document.getElementById('si-'+type+'-agent').value;
  var statusEl = document.getElementById('si-'+type+'-status');
  if (!agentId || !statusEl) { if(statusEl) statusEl.innerHTML = ''; return; }
  var agent = agents.find(function(a) { return a.id === agentId; });
  if (!agent) return;
  var si = agent.self_improvement;
  if (si && si.enabled) {
    statusEl.innerHTML = '<span style="color:var(--success)">✓ Self-improvement enabled</span> | ' +
      'Imported: ' + (si.imported_count||0) + ' | Retros: ' + (si.retrospective_count||0) +
      ' | Learnings: ' + (si.learning_count||0);
  } else {
    statusEl.innerHTML = '<span style="color:var(--text3)">Self-improvement not enabled for this agent</span>';
  }
}

async function siTriggerRetro() {
  var agentId = document.getElementById('si-retro-agent').value;
  if (!agentId) { alert('Please select an agent'); return; }
  var summary = document.getElementById('si-retro-summary').value.trim();

  var btn = document.getElementById('si-retro-btn');
  if (!btn) return;
  btn.disabled = true; btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;animation:spin 1s linear infinite">sync</span> Running...';
  document.getElementById('si-retro-status').innerHTML = '<span style="color:var(--primary)">正在调用 LLM 进行复盘分析...</span>';

  try {
    var result = await api('POST', '/api/portal/experience/retrospective', {
      agent_id: agentId, task_summary: summary
    });
    if (!result) {
      document.getElementById('si-retro-status').innerHTML = '<span style="color:var(--error)">Error: 服务端返回空 — 请检查 Agent 的 LLM 配置 (provider/model)</span>';
    } else if (result.error) {
      document.getElementById('si-retro-status').innerHTML = '<span style="color:var(--error)">Error: '+esc(result.error)+'</span>';
    } else {
      document.getElementById('si-retro-status').innerHTML = '<span style="color:var(--success)">✓ 复盘完成</span>';
      siShowResult('Retrospective Result (复盘结果)', result);
      siLoadStats();
      siLoadHistory();
      siLoadPlanBoard();
      siLoadInsights();
      await refresh();
    }
  } catch(e) {
    document.getElementById('si-retro-status').innerHTML = '<span style="color:var(--error)">Error: '+esc(e.message)+'</span>';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">replay</span> Run Retrospective';
  }
}

async function siTriggerLearn() {
  var agentId = document.getElementById('si-learn-agent').value;
  if (!agentId) { alert('Please select an agent'); return; }
  var goal = document.getElementById('si-learn-goal').value.trim();
  var gap = document.getElementById('si-learn-gap').value.trim();

  var btn = document.getElementById('si-learn-btn');
  if (!btn) return;
  btn.disabled = true; btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;animation:spin 1s linear infinite">sync</span> Learning...';
  document.getElementById('si-learn-status').innerHTML = '<span style="color:var(--primary)">正在调用 LLM 进行主动学习...</span>';

  try {
    var result = await api('POST', '/api/portal/experience/learning', {
      agent_id: agentId, learning_goal: goal, knowledge_gap: gap
    });
    if (!result) {
      document.getElementById('si-learn-status').innerHTML = '<span style="color:var(--error)">Error: 服务端返回空 — 请检查 Agent 的 LLM 配置 (provider/model)</span>';
    } else if (result.error) {
      document.getElementById('si-learn-status').innerHTML = '<span style="color:var(--error)">Error: '+esc(result.error)+'</span>';
    } else {
      document.getElementById('si-learn-status').innerHTML = '<span style="color:var(--success)">✓ 学习完成</span>';
      siShowResult('Active Learning Result (主动学习结果)', result);
      siLoadStats();
      siLoadHistory();
      siLoadPlanBoard();
      siLoadInsights();
      await refresh();
    }
  } catch(e) {
    document.getElementById('si-learn-status').innerHTML = '<span style="color:var(--error)">Error: '+esc(e.message)+'</span>';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px">school</span> Run Active Learning';
  }
}

function siShowResult(title, result) {
  var container = document.getElementById('si-result');
  var titleEl = document.getElementById('si-result-title');
  var contentEl = document.getElementById('si-result-content');
  if (!container || !titleEl || !contentEl) return;

  titleEl.textContent = title;
  var html = '<div style="font-size:13px;line-height:1.7">';

  if (result.error) {
    html += '<div style="color:var(--error);padding:12px;background:rgba(244,67,54,0.1);border-radius:6px">'+esc(result.error)+'</div>';
  } else if (result.what_happened !== undefined) {
    // Retrospective result
    var fields = [
      {label:'发生了什么', key:'what_happened', icon:'description'},
      {label:'做得好的', key:'what_went_well', icon:'thumb_up', color:'var(--success)'},
      {label:'做得不好的', key:'what_went_wrong', icon:'thumb_down', color:'var(--error)'},
      {label:'根本原因', key:'root_cause', icon:'search'},
      {label:'改进方案', key:'improvement_plan', icon:'lightbulb', color:'var(--primary)'},
    ];
    fields.forEach(function(f) {
      if (result[f.key]) {
        html += '<div style="margin-bottom:12px"><div style="display:flex;align-items:center;gap:4px;font-weight:600;margin-bottom:4px">';
        html += '<span class="material-symbols-outlined" style="font-size:16px;color:'+(f.color||'var(--text2)')+'">'+f.icon+'</span> '+f.label+'</div>';
        html += '<div style="background:var(--surface2);padding:8px 12px;border-radius:6px;white-space:pre-wrap">'+esc(result[f.key])+'</div></div>';
      }
    });
    if (result.new_experiences && result.new_experiences.length > 0) {
      html += '<div style="margin-top:12px;padding:10px;background:rgba(76,175,80,0.1);border-radius:6px">';
      html += '<div style="font-weight:600;margin-bottom:6px;color:var(--success)">✨ New Experiences Generated ('+result.new_experiences.length+')</div>';
      result.new_experiences.forEach(function(e) {
        html += '<div style="font-size:12px;margin-bottom:4px">• <b>'+esc(e.id||'')+'</b>: '+esc(e.core_knowledge||e.scene||'')+'</div>';
      });
      html += '</div>';
    }
  } else if (result.learning_goal !== undefined) {
    // Learning result
    html += '<div style="margin-bottom:8px"><b>Learning Goal:</b> '+esc(result.learning_goal||'')+'</div>';
    html += '<div style="margin-bottom:8px"><b>Source:</b> '+esc(result.source_type||'')+' — '+esc(result.source_detail||'')+'</div>';
    if (result.key_findings) {
      html += '<div style="margin-bottom:8px"><b>Key Findings:</b></div>';
      html += '<div style="background:var(--surface2);padding:8px 12px;border-radius:6px;white-space:pre-wrap;margin-bottom:8px">'+esc(result.key_findings)+'</div>';
    }
    if (result.new_experiences && result.new_experiences.length > 0) {
      html += '<div style="padding:10px;background:rgba(76,175,80,0.1);border-radius:6px">';
      html += '<div style="font-weight:600;margin-bottom:6px;color:var(--success)">✨ New Experiences Generated ('+result.new_experiences.length+')</div>';
      result.new_experiences.forEach(function(e) {
        html += '<div style="font-size:12px;margin-bottom:4px">• <b>'+esc(e.id||'')+'</b>: '+esc(e.core_knowledge||e.scene||'')+'</div>';
      });
      html += '</div>';
    }
  } else {
    html += '<pre style="font-size:12px;max-height:300px;overflow:auto">'+esc(JSON.stringify(result,null,2))+'</pre>';
  }

  html += '</div>';
  contentEl.innerHTML = html;
  container.style.display = 'block';
  container.scrollIntoView({behavior:'smooth'});
}

var _siPlanFilter = 'all';

function siSetPlanFilter(state) {
  _siPlanFilter = state || 'all';
  siLoadPlanBoard();
}

function _siStateMeta(state) {
  if (state === 'queued')    return { label: '⏳ 待启动',  color: '#9E9E9E', bg: 'rgba(158,158,158,0.15)' };
  if (state === 'running')   return { label: '● 进行中',   color: '#FF9800', bg: 'rgba(255,152,0,0.15)' };
  if (state === 'completed') return { label: '✓ 已完成',  color: 'var(--success)', bg: 'rgba(76,175,80,0.15)' };
  return { label: state || '-', color: 'var(--text3)', bg: 'rgba(0,0,0,0)' };
}

async function siLoadPlanBoard() {
  var el = document.getElementById('si-plan-board');
  if (!el) return;
  try {
    var data = await api('GET', '/api/portal/experience/plans');
    var summary = (data && data.summary) || {};
    var plans = (data && data.plans) || [];

    // Update KPI metric cards with real closed-loop numbers
    var set = function(id, v){ var e = document.getElementById(id); if (e) e.textContent = v; };
    set('si-metric-plans', summary.total != null ? summary.total : '0');
    set('si-metric-running', (summary.queued || 0) + (summary.running || 0));
    var cr = summary.completion_rate != null ? Math.round(summary.completion_rate * 100) + '%' : '-';
    var kr = summary.conversion_rate != null ? Math.round(summary.conversion_rate * 100) + '%' : '-';
    set('si-metric-completion', cr);
    set('si-metric-conversion', kr);

    // Filter tab bar
    var tabBar = ''
      + '<div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap">'
      + _siTab('all',       '全部',    summary.total || 0)
      + _siTab('queued',    '待启动',  summary.queued || 0)
      + _siTab('running',   '进行中',  summary.running || 0)
      + _siTab('completed', '已完成',  summary.completed || 0)
      + '<div style="flex:1"></div>'
      + '<div style="font-size:11px;color:var(--text3);align-self:center">完成率 '+cr+' · 经验转化率 '+kr+' · 已沉淀 <b style="color:var(--text1)">'+(summary.experiences_produced||0)+'</b> 条</div>'
      + '</div>';

    var visible = plans.filter(function(p){
      return _siPlanFilter === 'all' || p.state === _siPlanFilter;
    });

    if (!visible.length) {
      el.innerHTML = tabBar + '<div style="color:var(--text3);font-size:12px;text-align:center;padding:30px">'
        + (plans.length === 0
            ? '暂无学习计划。使用下方「主动学习」面板设定一个学习目标开始第一个闭环。'
            : '当前过滤条件下没有计划。')
        + '</div>';
      return;
    }

    var html = tabBar;
    visible.forEach(function(p) {
      var meta = _siStateMeta(p.state);
      var expCount = (p.new_experiences || []).length;
      var tsSecs = p.completed_at || p.started_at || p.queued_at || p.created_at || 0;
      var tsStr = tsSecs ? new Date(tsSecs * 1000).toLocaleString() : '-';

      // Header row
      html += '<div class="card" style="padding:12px 16px;margin-bottom:10px;cursor:pointer;border-left:3px solid '+meta.color+'" onclick="var d=this.querySelector(\'.si-plan-detail\');if(d)d.style.display=d.style.display===\'none\'?\'block\':\'none\'">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px">';
      html += '<div style="display:flex;align-items:center;gap:8px;flex:1;min-width:0">';
      html += '<span style="background:'+meta.bg+';color:'+meta.color+';padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap">'+meta.label+'</span>';
      html += '<span style="font-weight:700;font-size:12px;color:var(--primary);white-space:nowrap">'+esc(p.agent_name || '-')+'</span>';
      html += '<span style="font-size:11px;color:var(--text3);white-space:nowrap">'+esc(p.role || '-')+'</span>';
      html += '<span style="color:var(--text2);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">'+esc(p.learning_goal || '(未设定目标)')+'</span>';
      html += '</div>';
      html += '<div style="display:flex;align-items:center;gap:10px;flex-shrink:0">';
      if (p.state === 'completed') {
        var convColor = expCount > 0 ? 'var(--success)' : 'var(--text3)';
        html += '<span style="font-size:11px;color:'+convColor+'">经验 '+expCount+' 条</span>';
      }
      html += '<span style="font-size:11px;color:var(--text3)">'+tsStr+'</span>';
      html += '<span class="material-symbols-outlined" style="font-size:16px;color:var(--text3)">expand_more</span>';
      html += '</div></div>';

      // Expandable detail
      html += '<div class="si-plan-detail" style="display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--border-light)">';

      // Goal block — always shown
      html += '<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">🎯 学习目标 (Goal)</div>';
      html += '<div style="font-size:12px;background:var(--surface2);padding:8px 12px;border-radius:6px">'+esc(p.learning_goal||'(未设定)')+'</div></div>';

      if (p.knowledge_gap) {
        html += '<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">❓ 知识缺口 (Gap)</div>';
        html += '<div style="font-size:12px;color:var(--text2)">'+esc(p.knowledge_gap)+'</div></div>';
      }

      if (p.state === 'queued') {
        html += '<div style="font-size:12px;color:var(--text3);padding:8px 0">等待 agent 空闲后自动启动。当前排在队列中。</div>';
      } else if (p.state === 'running') {
        html += '<div style="font-size:12px;color:#FF9800;padding:8px 0">● agent 正在执行学习任务…</div>';
      } else {
        // Completed — show closed-loop full body
        if (p.source_type || p.source_detail) {
          html += '<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">📚 学习来源 (Source)</div>';
          html += '<div style="font-size:12px;color:var(--text2)">'+esc(p.source_type||'-')+(p.source_detail ? ' · '+esc(p.source_detail) : '')+'</div></div>';
        }
        if (p.key_findings) {
          html += '<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">📋 关键发现 (Findings)</div>';
          html += '<div style="font-size:12px;background:var(--surface2);padding:8px 12px;border-radius:6px;white-space:pre-wrap;line-height:1.6">'+esc(p.key_findings)+'</div></div>';
        }
        if (p.applicable_scenes) {
          html += '<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">🎯 适用场景 (Scenes)</div>';
          html += '<div style="font-size:12px;color:var(--text2)">'+esc(p.applicable_scenes)+'</div></div>';
        }

        // Closed-loop: new experiences + conversion outcome
        html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">';
        html += '<div style="background:rgba(76,175,80,0.05);border:1px solid rgba(76,175,80,0.2);border-radius:8px;padding:10px">';
        html += '<div style="font-size:11px;font-weight:600;color:var(--success);margin-bottom:6px">✨ 转化为经验 ('+expCount+' 条)</div>';
        if (expCount > 0) {
          (p.new_experiences || []).forEach(function(e) {
            html += '<div style="font-size:11px;margin-bottom:4px;display:flex;gap:4px">';
            html += '<span style="color:var(--primary);font-weight:600;flex-shrink:0">'+esc(e.id||'')+'</span>';
            html += '<span style="color:var(--text2);overflow:hidden;text-overflow:ellipsis">'+esc(e.core_knowledge||e.scene||'')+'</span>';
            html += '</div>';
          });
        } else {
          html += '<div style="font-size:11px;color:var(--text3)">本次学习未产出可沉淀经验（目标过宽或来源不足），建议细化目标后重试。</div>';
        }
        html += '</div>';

        html += '<div style="background:rgba(33,150,243,0.05);border:1px solid rgba(33,150,243,0.2);border-radius:8px;padding:10px">';
        html += '<div style="font-size:11px;font-weight:600;color:#2196F3;margin-bottom:6px">📦 经验固化去向</div>';
        html += '<div style="font-size:11px;color:var(--text2);line-height:1.8">';
        html += '• <b>角色经验库:</b> ~/.tudou_claw/experience/'+esc(p.role||'general')+'<br>';
        html += '• <b>System Prompt:</b> 下次对话自动注入<br>';
        html += '• <b>语义记忆 (L3):</b> 检索时命中<br>';
        html += '• <b>复用范围:</b> 同角色其他 agent 可检索';
        html += '</div></div>';
        html += '</div>';
      }

      html += '</div>'; // detail end
      html += '</div>'; // card end
    });
    el.innerHTML = html;
  } catch(e) {
    console.error('siLoadPlanBoard error:', e);
    el.innerHTML = '<div style="color:var(--error);font-size:12px;padding:20px">Load error: '+e.message+'</div>';
  }
}

function _siTab(state, label, count) {
  var active = _siPlanFilter === state;
  var bg = active ? 'var(--primary)' : 'var(--surface2)';
  var col = active ? '#282572' : 'var(--text2)';
  var fw = active ? '700' : '500';
  return '<button class="btn btn-sm" style="font-size:11px;padding:4px 10px;background:'+bg+';color:'+col+';font-weight:'+fw+'" onclick="siSetPlanFilter(\''+state+'\')">'+label+' ('+count+')</button>';
}

async function siLoadInsights() {
  var el = document.getElementById('si-insights');
  if (!el) return;
  try {
    var data = await api('GET', '/api/portal/experience/insights');
    if (!data || !data.insights || data.insights.length === 0) {
      el.innerHTML = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:20px">暂无复盘洞察。使用「自我复盘」提炼经验。</div>';
      return;
    }
    var html = '';
    data.insights.forEach(function(ins, idx) {
      var ts = ins.created_at ? new Date(ins.created_at * 1000).toLocaleString() : '';
      var expCount = (ins.new_experiences && ins.new_experiences.length) || 0;
      html += '<div style="padding:10px 12px;border-radius:8px;background:var(--surface2);margin-bottom:8px'+(idx===0?';border-left:3px solid var(--primary)':'')+'">';
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
      html += '<span style="font-weight:700;font-size:12px">'+esc(ins.agent_name||'Agent')+' <span style="color:var(--text3);font-weight:400;font-size:11px">'+esc(ins.role||'')+'</span></span>';
      html += '<span style="font-size:10px;color:var(--text3)">'+ts+(idx===0?' <span style="color:var(--primary);font-weight:600">[最新]</span>':'')+'</span>';
      html += '</div>';
      if (ins.improvement_plan) {
        html += '<div style="font-size:12px;margin-bottom:4px"><span style="color:var(--primary)">改进方案:</span> '+esc(ins.improvement_plan).substring(0,150)+'</div>';
      }
      if (ins.root_cause) {
        html += '<div style="font-size:12px;margin-bottom:4px"><span style="color:#FF9800">根本原因:</span> '+esc(ins.root_cause).substring(0,120)+'</div>';
      }
      if (ins.what_went_well) {
        html += '<div style="font-size:11px;color:var(--success)">✓ '+esc(ins.what_went_well).substring(0,100)+'</div>';
      }
      if (ins.what_went_wrong) {
        html += '<div style="font-size:11px;color:var(--error)">✗ '+esc(ins.what_went_wrong).substring(0,100)+'</div>';
      }
      if (expCount > 0) {
        html += '<div style="font-size:11px;color:var(--success);margin-top:4px">+'+expCount+' 经验</div>';
      }
      html += '</div>';
    });
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:var(--error);font-size:12px;padding:10px">Error: '+esc(e.message)+'</div>';
  }
}

async function siLoadExperiences() {
  var role = document.getElementById('si-lib-role').value;
  var el = document.getElementById('si-exp-list');
  if (!el) return;
  if (!role) { el.innerHTML = '<div style="color:var(--text3);font-size:13px;text-align:center;padding:40px">Select a role to browse experiences</div>'; return; }

  el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text3)">Loading...</div>';
  try {
    var data = await api('GET', '/api/portal/experience/list?role='+encodeURIComponent(role));
    if (!data || !data.experiences || data.experiences.length === 0) {
      el.innerHTML = '<div style="color:var(--text3);font-size:13px;text-align:center;padding:40px">No experiences found for role: '+esc(role)+'</div>';
      return;
    }
    var html = '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<tr style="border-bottom:1px solid var(--border);text-align:left">';
    html += '<th style="padding:8px 6px;font-weight:600">ID</th>';
    html += '<th style="padding:8px 6px;font-weight:600">Type</th>';
    html += '<th style="padding:8px 6px;font-weight:600">Scene</th>';
    html += '<th style="padding:8px 6px;font-weight:600">Knowledge</th>';
    html += '<th style="padding:8px 6px;font-weight:600">Priority</th>';
    html += '<th style="padding:8px 6px;font-weight:600">Success Rate</th>';
    html += '</tr>';
    data.experiences.forEach(function(e) {
      var rate = (e.success_count + e.fail_count) > 0 ? Math.round(e.success_count / (e.success_count + e.fail_count) * 100) : '-';
      var pColor = e.priority === 'high' ? 'var(--error)' : e.priority === 'medium' ? 'var(--primary)' : 'var(--text3)';
      var typeIcon = e.exp_type === 'retrospective' ? '🔄' : '📚';
      html += '<tr style="border-bottom:1px solid var(--border-light)">';
      html += '<td style="padding:6px;font-family:monospace;font-weight:600">'+esc(e.id)+'</td>';
      html += '<td style="padding:6px">'+typeIcon+' '+esc(e.exp_type)+'</td>';
      html += '<td style="padding:6px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(e.scene)+'">'+esc(e.scene)+'</td>';
      html += '<td style="padding:6px;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(e.core_knowledge)+'">'+esc(e.core_knowledge)+'</td>';
      html += '<td style="padding:6px;color:'+pColor+';font-weight:600;text-transform:uppercase">'+esc(e.priority)+'</td>';
      html += '<td style="padding:6px">'+(rate === '-' ? '<span style="color:var(--text3)">new</span>' : rate+'%')+'</td>';
      html += '</tr>';
    });
    html += '</table>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:var(--error);padding:20px">Error loading experiences: '+esc(e.message)+'</div>';
  }
}

async function siLoadHistory() {
  try {
    var data = await api('GET', '/api/portal/experience/history');
    var el = document.getElementById('si-history');
    if (!el || !data) return;
    var items = data.history || [];
    if (items.length === 0) {
      el.innerHTML = '<div style="color:var(--text3);font-size:13px;text-align:center;padding:20px">No activity yet. Run a retrospective or learning session to get started!</div>';
      return;
    }
    var html = '';
    items.forEach(function(item) {
      var ts = item.created_at ? new Date(item.created_at * 1000).toLocaleString() : '';
      var icon = item.type === 'retrospective' ? 'replay' : 'school';
      var color = item.type === 'retrospective' ? '#FF9800' : '#4CAF50';
      html += '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light)">';
      html += '<span class="material-symbols-outlined" style="font-size:18px;color:'+color+'">'+icon+'</span>';
      html += '<div style="flex:1;min-width:0">';
      html += '<div style="font-size:13px;font-weight:600">'+esc(item.agent_name||'Agent')+' <span style="color:var(--text3);font-weight:400">— '+esc(item.type||'')+'</span></div>';
      html += '<div style="font-size:12px;color:var(--text3);margin-top:2px">'+esc(item.summary||'')+'</div>';
      if (item.new_count) html += '<div style="font-size:11px;color:var(--success);margin-top:2px">+'+item.new_count+' new experiences</div>';
      html += '</div>';
      html += '<div style="font-size:11px;color:var(--text3);white-space:nowrap">'+ts+'</div>';
      html += '</div>';
    });
    el.innerHTML = html;
  } catch(e) { console.error('siLoadHistory error:', e); }
}

// ---- Workflow Designer ----
var _wfSteps = [];  // [{name, agent_id, input_desc, output_desc, depends_on:[]}]

function showCreateWorkflowModal() {
  _wfSteps = [];
  var html = '<div style="max-width:700px">';
  html += '<h3 style="margin-bottom:4px">Create Workflow</h3>';
  html += '<p style="font-size:11px;color:var(--text3);margin:0 0 16px">Workflow defines abstract process steps. Agents are assigned when creating a project/task.</p>';
  html += '<div class="form-group"><label>Workflow Name</label><input id="wf-name" placeholder="e.g. Code Review Pipeline"></div>';
  html += '<div class="form-group"><label>Description</label><input id="wf-desc" placeholder="Describe the workflow..."></div>';
  html += '<div style="display:flex;align-items:center;justify-content:space-between;margin:16px 0 8px">';
  html += '<span style="font-size:13px;font-weight:700;color:var(--text)">Steps</span>';
  html += '</div>';
  html += '<div id="wf-nodes-container" style="position:relative;min-height:80px;margin-bottom:12px"></div>';
  html += '<div style="display:flex;justify-content:center;margin-bottom:16px"><div onclick="addWfStep()" style="width:48px;height:48px;border-radius:50%;background:var(--primary);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:transform 0.15s;box-shadow:0 2px 12px rgba(203,201,255,0.3)" onmouseenter="this.style.transform=\'scale(1.1)\'" onmouseleave="this.style.transform=\'scale(1)\'"><span class="material-symbols-outlined" style="font-size:28px;color:#282572">add</span></div></div>';
  html += '<div class="form-actions"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button>';
  html += '<button class="btn btn-primary" onclick="createWorkflowFromDesigner()">Create</button></div>';
  html += '</div>';
  showCustomModal(html);
  renderWfNodes();
}

function addWfStep() {
  _wfSteps.push({
    name: 'Step ' + (_wfSteps.length + 1),
    description: '',
    input_desc: '',
    output_desc: '',
    depends_on: _wfSteps.length > 0 ? [_wfSteps.length - 1] : []
  });
  renderWfNodes();
}

function removeWfStep(idx) {
  _wfSteps.splice(idx, 1);
  _wfSteps.forEach(function(s) {
    s.depends_on = (s.depends_on||[]).filter(function(d){ return d < _wfSteps.length; });
  });
  renderWfNodes();
  var countEl = document.getElementById('wf-step-count');
  if (countEl) countEl.textContent = _wfSteps.length;
}

function renderWfNodes() {
  var container = document.getElementById('wf-nodes-container');
  if (!container) return;
  if (_wfSteps.length === 0) {
    container.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text3);font-size:13px;border:2px dashed var(--border);border-radius:12px">Click <strong>+</strong> to add workflow steps</div>';
    return;
  }
  var html = '';
  _wfSteps.forEach(function(step, idx) {
    if (idx > 0) {
      html += '<div style="display:flex;justify-content:center;padding:4px 0"><div style="width:2px;height:20px;background:var(--primary);opacity:0.4"></div></div>';
      html += '<div style="display:flex;justify-content:center;padding:0 0 4px"><span class="material-symbols-outlined" style="font-size:16px;color:var(--primary);opacity:0.5">arrow_downward</span></div>';
    }
    html += '<div style="background:var(--surface);border:1px solid var(--border-light);border-radius:12px;padding:14px 16px;position:relative;transition:border-color 0.15s" onmouseenter="this.style.borderColor=\'var(--primary)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\'">';
    // Header: step number + editable name + remove button
    html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">';
    html += '<div style="display:flex;align-items:center;gap:8px"><span style="width:24px;height:24px;border-radius:50%;background:var(--primary);color:#282572;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800">'+(idx+1)+'</span>';
    html += '<input value="'+esc(step.name)+'" onchange="_wfSteps['+idx+'].name=this.value" style="background:none;border:none;color:var(--text);font-size:13px;font-weight:600;width:200px;outline:none;border-bottom:1px solid transparent" onfocus="this.style.borderBottomColor=\'var(--primary)\'" onblur="this.style.borderBottomColor=\'transparent\'">';
    html += '</div>';
    html += '<div style="display:flex;align-items:center;gap:2px">';
    if (idx > 0) html += '<button onclick="moveWfStep('+idx+',-1)" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:2px" title="Move up"><span class="material-symbols-outlined" style="font-size:14px">arrow_upward</span></button>';
    if (idx < _wfSteps.length-1) html += '<button onclick="moveWfStep('+idx+',1)" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:2px" title="Move down"><span class="material-symbols-outlined" style="font-size:14px">arrow_downward</span></button>';
    html += '<button onclick="removeWfStep('+idx+')" style="background:none;border:none;color:var(--error);cursor:pointer;opacity:0.5;font-size:16px;padding:2px" onmouseenter="this.style.opacity=1" onmouseleave="this.style.opacity=0.5">&times;</button>';
    html += '</div></div>';
    // Description
    html += '<div style="margin-bottom:8px"><div style="font-size:9px;text-transform:uppercase;color:var(--text3);letter-spacing:0.5px;margin-bottom:4px">Description</div>';
    html += '<input value="'+esc(step.description||'')+'" onchange="_wfSteps['+idx+'].description=this.value" placeholder="What this step does..." style="width:100%;padding:6px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);box-sizing:border-box"></div>';
    // Input + Output (2 columns)
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">';
    html += '<div><div style="font-size:9px;text-transform:uppercase;color:var(--text3);letter-spacing:0.5px;margin-bottom:4px">Input</div>';
    html += '<input value="'+esc(step.input_desc)+'" onchange="_wfSteps['+idx+'].input_desc=this.value" placeholder="What goes in..." style="width:100%;padding:6px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);box-sizing:border-box"></div>';
    html += '<div><div style="font-size:9px;text-transform:uppercase;color:var(--text3);letter-spacing:0.5px;margin-bottom:4px">Output</div>';
    html += '<input value="'+esc(step.output_desc)+'" onchange="_wfSteps['+idx+'].output_desc=this.value" placeholder="What comes out..." style="width:100%;padding:6px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);box-sizing:border-box"></div>';
    html += '</div>';
    // Prompt template (collapsible advanced)
    html += '<details style="margin-top:8px"><summary style="font-size:9px;text-transform:uppercase;color:var(--text3);letter-spacing:0.5px;cursor:pointer;user-select:none">Prompt Template / Role</summary>';
    html += '<div style="display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:6px">';
    html += '<div><textarea onchange="_wfSteps['+idx+'].prompt_template=this.value" placeholder="Agent prompt template for this step..." style="width:100%;padding:6px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);box-sizing:border-box;min-height:48px;resize:vertical;font-family:monospace">'+esc(step.prompt_template||'')+'</textarea></div>';
    html += '<div><div style="font-size:9px;text-transform:uppercase;color:var(--text3);letter-spacing:0.5px;margin-bottom:4px">Role</div>';
    html += '<input value="'+esc(step.suggested_role||'')+'" onchange="_wfSteps['+idx+'].suggested_role=this.value" placeholder="coder" style="width:80px;padding:6px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);box-sizing:border-box"></div>';
    html += '</div></details>';
    html += '</div>';
  });
  container.innerHTML = html;
}

async function createWorkflowFromDesigner() {
  var name = document.getElementById('wf-name').value.trim();
  if (!name) { alert('Please enter a workflow name'); return; }
  var desc = document.getElementById('wf-desc').value.trim();
  if (_wfSteps.length === 0) { alert('Please add at least one step'); return; }
  var steps = _wfSteps.map(function(s, idx) {
    return {
      name: s.name || ('Step ' + (idx+1)),
      description: s.description || '',
      input_desc: s.input_desc || '',
      output_desc: s.output_desc || '',
      task: (s.input_desc ? 'Input: '+s.input_desc+'. ' : '') + (s.output_desc ? 'Expected output: '+s.output_desc : ''),
      depends_on: s.depends_on || []
    };
  });
  var data = await api('POST', '/api/portal/workflows', {
    action: 'create', name: name, description: desc, steps: steps
  });
  if (data && !data.error) {
    closeModal();
    renderWorkflows();
  } else {
    alert((data && data.error) || 'Failed to create workflow');
  }
}

async function startWorkflow(wfId) {
  await api('POST', '/api/portal/workflows/' + wfId + '/start', {});
  renderCurrentView();
}

async function abortWorkflow(wfId) {
  if (!confirm('确定要中止这个工作流吗？')) return;
  await api('POST', '/api/portal/workflows/' + wfId + '/abort', {});
  renderCurrentView();
}

async function editWorkflow(wfId) {
  // Fetch full workflow data including steps
  var wfData = null;
  try {
    wfData = await api('GET', '/api/portal/workflows/' + wfId);
  } catch(e) {}
  if (!wfData || wfData.error) { alert('Could not load workflow'); return; }

  // Populate _wfSteps from existing steps
  _wfSteps = (wfData.steps || []).map(function(s) {
    return {
      name: s.name || '',
      description: s.description || '',
      input_desc: s.input_spec || s.input_desc || '',
      output_desc: s.output_spec || s.output_desc || '',
      prompt_template: s.prompt_template || '',
      suggested_role: s.suggested_role || '',
      depends_on: s.depends_on || []
    };
  });
  _editingWfId = wfId;

  var html = '<div style="max-width:700px">';
  html += '<h3 style="margin-bottom:4px"><span class="material-symbols-outlined" style="vertical-align:middle;color:var(--primary)">edit_note</span> Edit Workflow</h3>';
  html += '<p style="font-size:11px;color:var(--text3);margin:0 0 16px">Edit steps, add new steps, reorder, or remove steps.</p>';
  html += '<div class="form-group"><label>Workflow Name</label><input id="wf-edit-name" value="' + esc(wfData.name||'').replace(/"/g,'&quot;') + '" placeholder="e.g. Code Review Pipeline"></div>';
  html += '<div class="form-group"><label>Description</label><input id="wf-edit-desc" value="' + esc(wfData.description||'').replace(/"/g,'&quot;') + '" placeholder="Describe the workflow..."></div>';
  html += '<div style="display:flex;align-items:center;justify-content:space-between;margin:16px 0 8px">';
  html += '<span style="font-size:13px;font-weight:700;color:var(--text)">Steps (' + _wfSteps.length + ')</span>';
  html += '</div>';
  html += '<div id="wf-nodes-container" style="position:relative;min-height:80px;margin-bottom:12px;max-height:400px;overflow-y:auto"></div>';
  // Add step circle button
  html += '<div style="display:flex;justify-content:center;margin-bottom:16px">';
  html += '<div onclick="addWfStep();document.getElementById(\'wf-step-count\').textContent=_wfSteps.length" style="width:48px;height:48px;border-radius:50%;background:var(--primary);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:transform 0.15s;box-shadow:0 2px 12px rgba(203,201,255,0.3)" onmouseenter="this.style.transform=\'scale(1.1)\'" onmouseleave="this.style.transform=\'scale(1)\'">';
  html += '<span class="material-symbols-outlined" style="font-size:28px;color:#282572">add</span></div></div>';
  // Actions
  html += '<div class="form-actions" style="display:flex;justify-content:space-between;align-items:center">';
  html += '<span style="font-size:11px;color:var(--text3)"><span id="wf-step-count">' + _wfSteps.length + '</span> steps</span>';
  html += '<div style="display:flex;gap:8px">';
  html += '<button class="btn btn-ghost" onclick="_editingWfId=null;closeModal()">Cancel</button>';
  html += '<button class="btn btn-primary" onclick="saveEditedWorkflow()">Save Changes</button>';
  html += '</div></div></div>';
  showCustomModal(html);
  renderWfNodes();
}

var _editingWfId = null;

async function saveEditedWorkflow() {
  if (!_editingWfId) return;
  var name = document.getElementById('wf-edit-name').value.trim();
  if (!name) { alert('Please enter a workflow name'); return; }
  var desc = document.getElementById('wf-edit-desc').value.trim();
  var steps = _wfSteps.map(function(s, idx) {
    return {
      name: s.name || ('Step ' + (idx+1)),
      description: s.description || '',
      input_spec: s.input_desc || '',
      output_spec: s.output_desc || '',
      prompt_template: s.prompt_template || '',
      suggested_role: s.suggested_role || '',
      input_desc: s.input_desc || '',
      output_desc: s.output_desc || '',
      task: (s.input_desc ? 'Input: '+s.input_desc+'. ' : '') + (s.output_desc ? 'Expected output: '+s.output_desc : ''),
      depends_on: s.depends_on || []
    };
  });
  var data = await api('POST', '/api/portal/workflows', {
    action: 'update', workflow_id: _editingWfId,
    name: name, description: desc, steps: steps
  });
  if (data && !data.error) {
    _editingWfId = null;
    closeModal();
    renderWorkflows();
  } else {
    alert((data && data.error) || 'Failed to update workflow');
  }
}

// Move step up/down for reordering
function moveWfStep(idx, direction) {
  var newIdx = idx + direction;
  if (newIdx < 0 || newIdx >= _wfSteps.length) return;
  var tmp = _wfSteps[idx];
  _wfSteps[idx] = _wfSteps[newIdx];
  _wfSteps[newIdx] = tmp;
  renderWfNodes();
}

async function deleteWorkflow(wfId) {
  if (!confirm('确定要删除这个工作流吗？')) return;
  await api('POST', '/api/portal/workflows', {action:'delete', workflow_id:wfId});
  renderCurrentView();
}

// --- @mention autocomplete ---
var _mentionActiveIdx = -1;
var _mentionProjId = '';

function _getProjectMembers(projId) {
  // Get current project's members matched to agent data
  var projEl = document.getElementById('project-members-'+projId);
  if (!projEl) return [];
  // Use global agents array and match to project members
  // We'll fetch from the proj data cached in DOM or use agents list
  return agents.map(function(a) {
    return { id: a.id, name: a.name, role: a.role || 'general', display: (a.role||'general')+'-'+a.name };
  });
}

function _projInputChange(projId) {
  var input = document.getElementById('project-chat-input-'+projId);
  if (!input) return;
  var val = input.value;
  var cursorPos = input.selectionStart;
  // Find @ before cursor
  var textBefore = val.slice(0, cursorPos);
  var atIdx = textBefore.lastIndexOf('@');
  if (atIdx === -1 || (atIdx > 0 && textBefore[atIdx-1] !== ' ' && textBefore[atIdx-1] !== '\n')) {
    _hideMentionDropdown(projId);
    return;
  }
  var query = textBefore.slice(atIdx + 1).toLowerCase();
  var members = _getProjectMembers(projId);
  var filtered = members.filter(function(m) {
    return m.name.toLowerCase().indexOf(query) !== -1 ||
           m.role.toLowerCase().indexOf(query) !== -1 ||
           m.display.toLowerCase().indexOf(query) !== -1;
  });
  if (filtered.length === 0) { _hideMentionDropdown(projId); return; }
  _mentionProjId = projId;
  _mentionActiveIdx = 0;
  _showMentionDropdown(projId, filtered, atIdx);
}

function _showMentionDropdown(projId, members, atIdx) {
  var dd = document.getElementById('mention-dropdown-'+projId);
  if (!dd) return;
  dd.innerHTML = members.map(function(m, i) {
    return '<div class="mention-item'+(i===0?' active':'')+'" data-name="'+esc(m.display)+'" data-at-idx="'+atIdx+'" onclick="_selectMention(\''+projId+'\',\''+esc(m.display)+'\','+atIdx+')">' +
      '<span class="material-symbols-outlined" style="font-size:18px;color:var(--primary)">smart_toy</span>' +
      '<div><span class="mention-name">'+esc(m.display)+'</span><br><span class="mention-role">'+esc(m.role)+'</span></div>' +
    '</div>';
  }).join('');
  dd.classList.add('show');
}

function _hideMentionDropdown(projId) {
  var dd = document.getElementById('mention-dropdown-'+(projId||_mentionProjId));
  if (dd) { dd.classList.remove('show'); dd.innerHTML = ''; }
  _mentionActiveIdx = -1;
}

function _selectMention(projId, name, atIdx) {
  var input = document.getElementById('project-chat-input-'+projId);
  if (!input) return;
  var val = input.value;
  var cursorPos = input.selectionStart;
  var before = val.slice(0, atIdx);
  var after = val.slice(cursorPos);
  input.value = before + '@' + name + ' ' + after;
  input.focus();
  var newPos = before.length + 1 + name.length + 1;
  input.setSelectionRange(newPos, newPos);
  _hideMentionDropdown(projId);
}

function _projInputKeydown(e, projId) {
  var dd = document.getElementById('mention-dropdown-'+projId);
  if (dd && dd.classList.contains('show')) {
    var items = dd.querySelectorAll('.mention-item');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _mentionActiveIdx = Math.min(_mentionActiveIdx + 1, items.length - 1);
      items.forEach(function(el,i){ el.classList.toggle('active', i===_mentionActiveIdx); });
      return;
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _mentionActiveIdx = Math.max(_mentionActiveIdx - 1, 0);
      items.forEach(function(el,i){ el.classList.toggle('active', i===_mentionActiveIdx); });
      return;
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      if (_mentionActiveIdx >= 0 && _mentionActiveIdx < items.length) {
        var item = items[_mentionActiveIdx];
        _selectMention(projId, item.getAttribute('data-name'), parseInt(item.getAttribute('data-at-idx')));
      }
      return;
    } else if (e.key === 'Escape') {
      _hideMentionDropdown(projId);
      return;
    }
  }
  // Default: Enter sends message
  if (e.key === 'Enter' && !e.isComposing) {
    e.preventDefault();
    sendProjectMsg(projId);
  }
}

var _cachedWorkflows = [];

function _buildProjectMembersGroupedByNode() {
  // Group agents by node_id
  var nodeGroups = {};
  agents.forEach(function(a) {
    var nid = a.node_id || 'local';
    if (!nodeGroups[nid]) {
      nodeGroups[nid] = {
        agents: [],
        name: nid === 'local' ? '本机 (Local)' : nid
      };
    }
    nodeGroups[nid].agents.push(a);
  });

  // Render grouped HTML
  var html = '';
  Object.keys(nodeGroups).sort().forEach(function(nid) {
    var group = nodeGroups[nid];
    var statusDot = '<span style="width:6px;height:6px;border-radius:50%;background:#3fb950;display:inline-block;margin-right:4px"></span>';
    html += '<div style="margin-bottom:12px;border:1px solid var(--border-light);border-radius:8px;overflow:hidden">' +
      '<div style="background:var(--surface2);padding:8px 12px;font-size:11px;font-weight:700;display:flex;align-items:center;gap:6px;color:var(--text2)">' +
      statusDot + esc(group.name) + ' (' + group.agents.length + ')' +
      '</div>' +
      '<div style="padding:8px 12px">' +
      group.agents.map(function(a) {
        return '<label style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px;cursor:pointer"><input type="checkbox" class="proj-member-cb" value="'+a.id+'"> <span style="color:var(--text3);font-size:10px;flex-shrink:0">['+esc(a.role||'general')+']</span> '+esc(a.name)+'</label>';
      }).join('') +
      '</div></div>';
  });
  return html;
}

async function showCreateProjectModal() {
  // Load workflows for selection
  try {
    var wfData = await api('GET', '/api/portal/workflows');
    _cachedWorkflows = (wfData && wfData.workflows) ? wfData.workflows : [];
  } catch(e) { _cachedWorkflows = []; }

  var wfOpts = '<option value="">-- No Workflow (Free-form) --</option>' +
    _cachedWorkflows.map(function(wf) {
      return '<option value="'+wf.id+'">'+esc(wf.name)+' ('+((wf.steps||[]).length)+' steps)</option>';
    }).join('');

  var html = '<div style="padding:24px;max-width:600px"><h3 style="margin:0 0 16px">Create Project</h3>' +
    '<input id="new-proj-name" placeholder="Project Name" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;box-sizing:border-box">' +
    '<textarea id="new-proj-desc" placeholder="Description" rows="3" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;resize:vertical;box-sizing:border-box"></textarea>' +
    '<input id="new-proj-dir" placeholder="Working Directory (e.g. /home/user/project)" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;box-sizing:border-box">' +
    '<select id="new-proj-node" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;box-sizing:border-box">' +
      '<option value="local">Local Node</option>' +
      nodes.map(function(n){ return n.is_self ? '' : '<option value="'+n.id+'">'+esc(n.name||n.id)+'</option>'; }).join('') +
    '</select>' +

    // Workflow template selection
    '<div style="font-size:12px;font-weight:700;margin-bottom:6px"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">account_tree</span> Workflow Template</div>' +
    '<select id="new-proj-workflow" onchange="_onProjWorkflowChange()" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;box-sizing:border-box">' + wfOpts + '</select>' +

    // Step-Agent assignment area (hidden initially)
    '<div id="proj-wf-steps" style="display:none;margin-bottom:12px"></div>' +

    // Team members (shown when no workflow is selected)
    '<div id="proj-members-section">' +
    '<div style="font-size:12px;font-weight:700;margin-bottom:8px">Add Team Members</div>' +
    '<div id="proj-members-list" style="margin-bottom:12px">' +
      _buildProjectMembersGroupedByNode() +
    '</div></div>' +

    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary" onclick="createProject()">Create</button>' +
    '</div></div>';
  showModalHTML(html);
}

function _onProjWorkflowChange() {
  var wfId = document.getElementById('new-proj-workflow').value;
  var stepsDiv = document.getElementById('proj-wf-steps');
  var membersSection = document.getElementById('proj-members-section');
  if (!wfId) {
    // No workflow: show team members, hide step assignment
    stepsDiv.style.display = 'none';
    membersSection.style.display = '';
    return;
  }
  // Workflow selected: hide team members, show step→agent assignment
  membersSection.style.display = 'none';
  stepsDiv.style.display = '';

  var wf = _cachedWorkflows.find(function(w){ return w.id === wfId; });
  if (!wf || !wf.steps || wf.steps.length === 0) {
    stepsDiv.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:8px">This workflow has no steps.</div>';
    return;
  }

  var agentOpts = '<option value="">-- Select Agent --</option>' +
    agents.map(function(a) { return '<option value="'+a.id+'">'+esc(a.name)+' ('+esc(a.role||'general')+')</option>'; }).join('');

  var html = '<div style="font-size:12px;font-weight:700;margin-bottom:8px">Assign Agents to Workflow Steps</div>';
  html += '<div style="border:1px solid var(--border-light);border-radius:10px;overflow:hidden">';
  wf.steps.forEach(function(step, idx) {
    var borderTop = idx > 0 ? 'border-top:1px solid var(--border-light);' : '';
    html += '<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;'+borderTop+'">';
    html += '<span style="width:22px;height:22px;border-radius:50%;background:var(--primary);color:#282572;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;flex-shrink:0">'+(idx+1)+'</span>';
    html += '<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text)">'+esc(step.name||'Step '+(idx+1))+'</div>';
    if (step.description) html += '<div style="font-size:10px;color:var(--text3);margin-top:1px">'+esc(step.description)+'</div>';
    html += '</div>';
    html += '<select class="wf-step-agent" data-step-idx="'+idx+'" style="padding:6px 8px;font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);min-width:140px">' + agentOpts + '</select>';
    html += '<label title="启动前需人工批准" style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text3);cursor:pointer;white-space:nowrap"><input type="checkbox" class="wf-step-approval" data-step-idx="'+idx+'" style="cursor:pointer">审核</label>';
    html += '</div>';
  });
  html += '</div>';
  stepsDiv.innerHTML = html;
}

function closeModal() {
  // Remove ALL overlays with this id (there may be duplicates from prior renders)
  var overlays = document.querySelectorAll('#modal-overlay');
  overlays.forEach(function(o){ o.remove(); });
}

async function createProject() {
  var name = document.getElementById('new-proj-name').value.trim();
  var desc = document.getElementById('new-proj-desc').value.trim();
  var workingDir = document.getElementById('new-proj-dir').value.trim();
  var nodeId = document.getElementById('new-proj-node').value;
  if (!name) { alert('Name required'); return; }

  var wfId = document.getElementById('new-proj-workflow').value;
  var members = [];
  var stepAssignments = [];

  if (wfId) {
    // Workflow mode: collect step→agent assignments as members
    var selects = document.querySelectorAll('.wf-step-agent');
    var approvalMap = {};
    document.querySelectorAll('.wf-step-approval').forEach(function(chk){
      approvalMap[parseInt(chk.getAttribute('data-step-idx'))] = chk.checked;
    });
    var assignedAgentIds = {};
    selects.forEach(function(sel) {
      var agentId = sel.value;
      var stepIdx = parseInt(sel.getAttribute('data-step-idx'));
      if (agentId) {
        stepAssignments.push({
          step_index: stepIdx,
          agent_id: agentId,
          require_approval: !!approvalMap[stepIdx],
        });
        if (!assignedAgentIds[agentId]) {
          assignedAgentIds[agentId] = true;
          members.push({agent_id: agentId, responsibility: ''});
        }
      }
    });
  } else {
    // Free-form mode: collect checked members
    document.querySelectorAll('.proj-member-cb:checked').forEach(function(cb) {
      members.push({agent_id: cb.value, responsibility: ''});
    });
  }

  try {
    var payload = {name: name, description: desc, members: members, working_directory: workingDir, node_id: nodeId};
    if (wfId) {
      payload.workflow_id = wfId;
      payload.step_assignments = stepAssignments;
    }
    await api('POST', '/api/portal/projects', payload);
    closeModal();
    renderProjects();
  } catch(e) { alert('Error: '+e.message); }
}

function showProjectMemberModal(projId) {
  var html = '<div style="padding:24px"><h3 style="margin:0 0 16px">Manage Members</h3>' +
    '<div id="proj-add-members">' +
      agents.map(function(a) {
        return '<label style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px;cursor:pointer"><input type="checkbox" class="proj-add-member-cb" value="'+a.id+'"> '+esc(a.role+'-'+a.name)+' <input type="text" class="proj-resp-input" data-aid="'+a.id+'" placeholder="Responsibility" style="flex:1;padding:4px 8px;background:var(--bg);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:var(--text);font-size:11px"></label>';
      }).join('') +
    '</div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">' +
      '<button class="btn btn-ghost" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary" onclick="updateProjectMembers(\''+projId+'\')">Save</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function updateProjectMembers(projId) {
  var cbs = document.querySelectorAll('.proj-add-member-cb:checked');
  for (var i = 0; i < cbs.length; i++) {
    var aid = cbs[i].value;
    var respInput = document.querySelector('.proj-resp-input[data-aid="'+aid+'"]');
    var resp = respInput ? respInput.value : '';
    await api('POST', '/api/portal/projects/'+projId+'/members', {agent_id: aid, responsibility: resp});
  }
  closeModal();
  renderProjectDetail(projId);
}

function showProjectTaskModal(projId) {
  var html = '<div style="padding:24px"><h3 style="margin:0 0 16px">Assign Task</h3>' +
    '<input id="new-task-title" placeholder="Task Title" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<textarea id="new-task-desc" placeholder="Description" rows="3" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;resize:vertical"></textarea>' +
    '<select id="new-task-assignee" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="">Unassigned</option>' +
      agents.map(function(a){ return '<option value="'+a.id+'">'+esc(a.role+'-'+a.name)+'</option>'; }).join('') +
    '</select>' +
    '<select id="new-task-priority" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="0">Normal Priority</option><option value="1">High Priority</option><option value="2">Urgent</option>' +
    '</select>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary" onclick="createProjectTask(\''+projId+'\')">Assign</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function createProjectTask(projId) {
  var title = document.getElementById('new-task-title').value.trim();
  if (!title) { alert('Title required'); return; }
  var desc = document.getElementById('new-task-desc').value.trim();
  var assignee = document.getElementById('new-task-assignee').value;
  var priority = parseInt(document.getElementById('new-task-priority').value)||0;
  try {
    await api('POST', '/api/portal/projects/'+projId+'/tasks', {
      title: title, description: desc, assigned_to: assignee, priority: priority
    });
    closeModal();
    renderProjectDetail(projId);
    // Poll for agent responses if assigned
    if (assignee) {
      if (_projectChatPoll) clearInterval(_projectChatPoll);
      var pollCount = 0;
      _projectChatPoll = setInterval(function() {
        loadProjectChat(projId);
        pollCount++;
        if (pollCount > 30) clearInterval(_projectChatPoll);
      }, 2000);
    }
  } catch(e) { alert('Error: '+e.message); }
}


function showProjectMilestoneModal(projId) {
  var html = '<div style="padding:24px"><h3 style="margin:0 0 16px">Create Milestone</h3>' +
    '<input id="new-milestone-name" placeholder="Milestone Name" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<select id="new-milestone-agent" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="">Select Responsible Agent</option>' +
      agents.map(function(a){ return '<option value="'+a.id+'">'+esc(a.role+'-'+a.name)+'</option>'; }).join('') +
    '</select>' +
    '<input id="new-milestone-duedate" type="date" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary" onclick="createProjectMilestone(\''+projId+'\')">Create</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function createProjectMilestone(projId) {
  var name = document.getElementById('new-milestone-name').value.trim();
  if (!name) { alert('Name required'); return; }
  var agentId = document.getElementById('new-milestone-agent').value;
  var dueDate = document.getElementById('new-milestone-duedate').value;
  try {
    await api('POST', '/api/portal/projects/'+projId+'/milestones', {
      name: name, responsible_agent_id: agentId, due_date: dueDate
    });
    closeModal();
    renderProjectDetail(projId);
  } catch(e) { alert('Error: '+e.message); }
}

function showMilestoneUpdateModal(projId, milestoneId, milestoneName, currentStatus) {
  var newStatus = currentStatus === 'completed' ? 'in_progress' : currentStatus === 'in_progress' ? 'pending' : 'in_progress';
  var html = '<div style="padding:24px"><h3 style="margin:0 0 16px">Update Milestone</h3>' +
    '<div style="margin-bottom:16px"><div style="font-size:13px;font-weight:600;margin-bottom:8px">Current Status: '+currentStatus.toUpperCase()+'</div>' +
    '<select id="milestone-status-select" style="width:100%;padding:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="pending" '+(currentStatus==='pending'?'selected':'')+'>Pending</option>' +
      '<option value="in_progress" '+(currentStatus==='in_progress'?'selected':'')+'>In Progress</option>' +
      '<option value="completed" '+(currentStatus==='completed'?'selected':'')+'>Completed</option>' +
    '</select></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">Cancel</button>' +
      '<button class="btn btn-primary" onclick="updateProjectMilestone(\''+projId+'\',\''+milestoneId+'\')">Save</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function updateProjectMilestone(projId, milestoneId) {
  var newStatus = document.getElementById('milestone-status-select').value;
  try {
    await api('POST', '/api/portal/projects/'+projId+'/milestones/'+milestoneId+'/update', {
      milestone_id: milestoneId, status: newStatus
    });
    closeModal();
    renderProjectDetail(projId);
  } catch(e) { alert('Error: '+e.message); }
}

// ============ Audio MCP: TTS (Speaker) + STT (Microphone) ============
var _audioLastTs = 0;
var _audioPolling = false;
var _ttsEnabled = {};  // per agent TTS auto-read toggle
var _sttActive = {};   // per agent STT recording state

function _toggleTTS(agentId) {
  _ttsEnabled[agentId] = !_ttsEnabled[agentId];
  var btn = document.getElementById('tts-btn-' + agentId);
  if (!btn) return;
  var icon = btn.querySelector('.material-symbols-outlined');
  if (_ttsEnabled[agentId]) {
    icon.style.color = 'var(--primary)';
    icon.textContent = 'volume_up';
    btn.style.background = 'rgba(203,201,255,0.15)';
    btn.title = '自动朗读已开启 — 点击关闭';
  } else {
    icon.style.color = 'var(--text3)';
    icon.textContent = 'volume_off';
    btn.style.background = 'none';
    btn.title = '自动朗读开关 (Auto TTS)';
    if (window.speechSynthesis) speechSynthesis.cancel();
  }
}

function _autoSpeak(agentId, text) {
  // Called when agent finishes a reply — auto-speak if TTS toggle is on
  if (!_ttsEnabled[agentId]) return;
  if (!text || !text.trim()) return;
  if (!window.speechSynthesis) return;
  // Cancel any ongoing speech first
  speechSynthesis.cancel();
  var utt = new SpeechSynthesisUtterance(text);
  utt.lang = 'zh-CN';
  utt.rate = 1.0;
  speechSynthesis.speak(utt);
}

function _speakBubble(btnEl) {
  // Find the message text from the parent chat-msg element
  var msgEl = btnEl.closest('.chat-msg');
  if (!msgEl) return;
  var contentEl = msgEl.querySelector('.chat-msg-content');
  if (!contentEl) return;
  var text = contentEl._rawText || contentEl.textContent || '';
  if (!text.trim()) return;

  var icon = btnEl.querySelector('.material-symbols-outlined');
  if (!window.speechSynthesis) { alert('您的浏览器不支持语音合成 (TTS)'); return; }

  // If already speaking, stop
  if (speechSynthesis.speaking) {
    speechSynthesis.cancel();
    if (icon) { icon.style.color = ''; icon.textContent = 'volume_up'; }
    return;
  }

  var utt = new SpeechSynthesisUtterance(text);
  utt.lang = 'zh-CN';
  utt.rate = 1.0;
  if (icon) { icon.style.color = 'var(--primary)'; icon.textContent = 'stop_circle'; }
  utt.onend = function() {
    if (icon) { icon.style.color = ''; icon.textContent = 'volume_up'; }
  };
  utt.onerror = function() {
    if (icon) { icon.style.color = ''; icon.textContent = 'volume_up'; }
  };
  speechSynthesis.speak(utt);
}

function _toggleSTT(agentId) {
  if (_sttActive[agentId]) {
    // Stop recording
    if (_sttActive[agentId].recognition) {
      try { _sttActive[agentId].recognition.stop(); } catch(e) {}
    }
    _sttActive[agentId] = null;
    var btn = document.getElementById('stt-btn-' + agentId);
    if (btn) {
      var icon = btn.querySelector('.material-symbols-outlined');
      icon.style.color = 'var(--text3)';
      icon.textContent = 'mic';
      btn.style.background = 'none';
      btn.title = '语音输入 (STT Microphone)';
    }
    return;
  }
  // Start recording
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { alert('您的浏览器不支持语音识别 (Speech Recognition)'); return; }
  var recognition = new SpeechRecognition();
  recognition.lang = 'zh-CN';
  recognition.continuous = true;
  recognition.interimResults = true;
  var inputEl = document.getElementById('chat-input-' + agentId);
  recognition.onresult = function(e) {
    var transcript = '';
    for (var i = 0; i < e.results.length; i++) {
      transcript += e.results[i][0].transcript;
    }
    if (inputEl) inputEl.value = transcript;
  };
  recognition.onerror = function(e) {
    console.warn('STT error:', e.error);
    _toggleSTT(agentId); // auto-stop on error
  };
  recognition.onend = function() {
    // Auto-restart if still active
    if (_sttActive[agentId]) {
      try { recognition.start(); } catch(e) { _toggleSTT(agentId); }
    }
  };
  recognition.start();
  _sttActive[agentId] = { recognition: recognition };
  var btn = document.getElementById('stt-btn-' + agentId);
  if (btn) {
    var icon = btn.querySelector('.material-symbols-outlined');
    icon.style.color = '#E91E63';
    icon.textContent = 'mic';
    btn.style.background = 'rgba(233,30,99,0.15)';
    btn.title = '正在录音 — 点击停止';
  }
}
var _sttRecognition = null;

var _audioFetching = false;  // Guard against overlapping audio poll requests
function _startAudioPolling() {
  if (_audioPolling) return;
  _audioPolling = true;
  setInterval(async function() {
    if (_audioFetching) return;  // Skip if previous poll still in-flight
    _audioFetching = true;
    try {
      var resp = await api('GET', '/api/portal/audio/events?since=' + _audioLastTs);
      if (!resp || !resp.events) { _audioFetching = false; return; }
      resp.events.forEach(function(evt) {
        _audioLastTs = Math.max(_audioLastTs, evt.ts || 0);
        if (evt.type === 'tts_speak') _handleTTS(evt);
        else if (evt.type === 'stt_listen') _handleSTTListen(evt);
        else if (evt.type === 'stt_start') _handleSTTStart(evt);
        else if (evt.type === 'stt_stop') _handleSTTStop();
      });
    } catch(e) {}
    _audioFetching = false;
  }, 2000);  // Poll every 2 seconds
}

function _handleTTS(evt) {
  if (!window.speechSynthesis) { console.warn('TTS not supported'); return; }
  var utterance = new SpeechSynthesisUtterance(evt.text);
  utterance.lang = evt.lang || 'zh-CN';
  utterance.rate = evt.rate || 1.0;
  if (evt.voice) {
    var voices = speechSynthesis.getVoices();
    var match = voices.find(function(v) { return v.name.includes(evt.voice); });
    if (match) utterance.voice = match;
  }
  // Show speaking indicator
  var bubble = document.getElementById('robot-bubble-' + evt.agent_id);
  if (bubble) { bubble.textContent = '🔊 Speaking...'; bubble.style.background = '#E91E63'; }
  utterance.onend = function() {
    if (bubble) { bubble.textContent = '工作中...'; bubble.style.background = 'var(--primary)'; }
  };
  speechSynthesis.speak(utterance);
}

function _handleSTTListen(evt) {
  if (!window.SpeechRecognition && !window.webkitSpeechRecognition) {
    console.warn('STT not supported'); return;
  }
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recognition = new SpeechRecognition();
  recognition.lang = evt.lang || 'zh-CN';
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  recognition.onresult = function(e) {
    var transcript = e.results[0][0].transcript;
    if (transcript && evt.agent_id) {
      // Send recognized text as user message to the agent
      var input = document.getElementById('chat-input-' + evt.agent_id);
      if (input) { input.value = transcript; sendAgentMsg(evt.agent_id); }
    }
  };
  recognition.onerror = function(e) { console.warn('STT error:', e.error); };
  recognition.start();
  // Auto-stop after duration
  if (evt.duration) setTimeout(function() { try { recognition.stop(); } catch(e){} }, evt.duration * 1000);
}

function _handleSTTStart(evt) {
  if (_sttRecognition) { try { _sttRecognition.stop(); } catch(e){} }
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  _sttRecognition = new SpeechRecognition();
  _sttRecognition.lang = evt.lang || 'zh-CN';
  _sttRecognition.continuous = true;
  _sttRecognition.interimResults = false;
  _sttRecognition.onresult = function(e) {
    for (var i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) {
        var transcript = e.results[i][0].transcript;
        if (transcript && evt.agent_id) {
          var input = document.getElementById('chat-input-' + evt.agent_id);
          if (input) { input.value = transcript; sendAgentMsg(evt.agent_id); }
        }
      }
    }
  };
  _sttRecognition.start();
}

function _handleSTTStop() {
  if (_sttRecognition) { try { _sttRecognition.stop(); } catch(e){} _sttRecognition = null; }
}

// ============ Unified Settings Page ============
// ============ New Hub Wrappers ============
// 复用已有的 sub-renderers，只在外层套一个 tab 容器，避免重复实现内容

var _hubSubTab = {};

function _renderHubTabs(hubId, tabs) {
  var current = _hubSubTab[hubId] || tabs[0].id;
  var html = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;padding:8px;background:var(--surface);border-radius:12px;border:1px solid var(--border-light)">';
  tabs.forEach(function(t) {
    var active = current === t.id;
    html += '<button onclick="_hubSubTab[\''+hubId+'\']=\''+t.id+'\';renderCurrentView()" style="padding:8px 16px;border:none;background:'+(active?'var(--surface2)':'none')+';color:'+(active?'var(--primary)':'var(--text3)')+';font-size:12px;font-weight:'+(active?'700':'500')+';cursor:pointer;border-radius:8px;display:flex;align-items:center;gap:6px;font-family:inherit;white-space:nowrap;transition:all 0.15s">'
      + '<span class="material-symbols-outlined" style="font-size:16px">'+t.icon+'</span>'+esc(t.label)+'</button>';
  });
  html += '</div><div id="hub-'+hubId+'-content"></div>';
  return { html: html, current: current };
}

function renderProjectsHub() {
  var c = document.getElementById('content');
  var actionsEl = document.getElementById('topbar-actions');
  // All action buttons live inside each tab's own content header (top-right),
  // matching the Workflow tab pattern. The global topbar does NOT duplicate them.
  if (actionsEl) actionsEl.innerHTML = '';
  var tabs = [
    { id: 'projects', label: '项目列表', icon: 'folder_special' },
    { id: 'meetings', label: '群聊会议', icon: 'groups' },
    { id: 'task_center', label: '任务中心', icon: 'checklist' },
    { id: 'orchestration', label: '编排可视化', icon: 'hub' },
    { id: 'workflows', label: 'Workflow 模板', icon: 'account_tree' },
  ];
  var r = _renderHubTabs('projects', tabs);
  c.innerHTML = r.html;
  var sc = document.getElementById('hub-projects-content');
  var _orig = document.getElementById('content');
  sc.id = 'content'; _orig.id = 'content-outer';
  try {
    if (r.current === 'projects') {
      renderProjects();
    } else if (r.current === 'meetings') {
      renderMeetingsTab();
    } else if (r.current === 'task_center') {
      renderTaskCenterTab();
    } else if (r.current === 'orchestration') {
      renderOrchestration();
    } else if (r.current === 'workflows') {
      renderWorkflows();
    }
  } finally {
    sc.id = 'hub-projects-content'; _orig.id = 'content';
  }
}

// ============ Meetings Tab (群聊会议) ============

// ---------- Meeting list ----------
async function renderMeetingsTab() {
  var c = document.getElementById('content');
  c.innerHTML =
    '<div style="padding:18px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">' +
        '<div><h2 style="margin:0;font-size:22px;font-weight:800">群聊 / 会议</h2>' +
        '<p style="font-size:12px;color:var(--text3);margin-top:4px">多 Agent 临时协作会议 · 讨论 · 任务分派</p></div>' +
        '<div style="display:flex;gap:8px">' +
          '<select id="meetings-filter-status" onchange="renderMeetingsTab()" style="background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:var(--text);font-size:12px;padding:6px 10px">' +
            '<option value="">全部状态</option>' +
            '<option value="active">进行中</option>' +
            '<option value="scheduled">已安排</option>' +
            '<option value="closed">已结束</option>' +
            '<option value="cancelled">已取消</option>' +
          '</select>' +
          '<button class="btn btn-primary btn-sm" onclick="showCreateMeetingModal()"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建会议</button>' +
        '</div>' +
      '</div>' +
      '<div id="meetings-list-area" style="min-height:100px"><div style="color:var(--text3);font-size:12px">Loading…</div></div>' +
    '</div>';
  try {
    var filter = '';
    var fEl = document.getElementById('meetings-filter-status');
    if (fEl) filter = fEl.value || '';
    var qs = filter ? ('?status='+encodeURIComponent(filter)) : '';
    var r = await api('GET', '/api/portal/meetings'+qs);
    var list = r.meetings || [];
    var listEl = document.getElementById('meetings-list-area');
    if (!list.length) {
      listEl.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text3);font-size:13px">暂无会议。点击"新建会议"拉起一场跨 Agent 协作。</div>';
      return;
    }
    listEl.innerHTML = list.map(function(m){
      var statusColor = m.status==='active'?'#22c55e':m.status==='scheduled'?'var(--primary)':m.status==='closed'?'var(--text3)':'#ef4444';
      var statusLabel = m.status==='active'?'进行中':m.status==='scheduled'?'待开始':m.status==='closed'?'已结束':'已取消';
      var ts = m.created_at ? new Date(m.created_at*1000).toLocaleString() : '';
      var partAvatars = (m.participants||[]).slice(0,5).map(function(pid){
        var ag = (window._cachedAgents||agents||[]).find(function(a){return a.id===pid;});
        var nm = ag ? (ag.name||'?')[0] : '?';
        return '<div style="width:24px;height:24px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;border:2px solid var(--bg);margin-left:-6px" title="'+(ag?esc(ag.name):pid)+'">'+esc(nm)+'</div>';
      }).join('');
      var moreCount = Math.max(0, (m.participants||[]).length - 5);
      if (moreCount > 0) partAvatars += '<div style="width:24px;height:24px;border-radius:50%;background:rgba(255,255,255,0.1);color:var(--text3);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;margin-left:-6px">+'+moreCount+'</div>';
      return '<div onclick="openMeetingDetail(\''+m.id+'\')" style="background:var(--surface);border-radius:10px;padding:14px 16px;border:1px solid rgba(255,255,255,0.06);margin-bottom:10px;cursor:pointer;transition:border-color 0.2s" onmouseover="this.style.borderColor=\'var(--primary)\'" onmouseout="this.style.borderColor=\'rgba(255,255,255,0.06)\'">' +
        '<div style="display:flex;justify-content:space-between;align-items:center">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="display:flex;align-items:center;gap:8px"><span style="font-weight:700;font-size:15px">'+esc(m.title||'(untitled)')+'</span><span style="font-size:10px;padding:2px 8px;border-radius:10px;background:rgba(255,255,255,0.08);color:'+statusColor+';font-weight:600">'+statusLabel+'</span></div>' +
            '<div style="font-size:11px;color:var(--text3);margin-top:6px;display:flex;align-items:center;gap:12px">' +
              '<span>'+ts+'</span>' +
              '<span>💬 '+m.message_count+'</span>' +
              '<span>📌 '+m.open_assignments+'/'+m.assignment_count+'</span>' +
            '</div>' +
          '</div>' +
          '<div style="display:flex;align-items:center;padding-left:6px">'+partAvatars+'</div>' +
        '</div>' +
      '</div>';
    }).join('');
  } catch(e) {
    document.getElementById('meetings-list-area').innerHTML = '<div style="color:var(--error)">Error: '+esc(e.message)+'</div>';
  }
}

// ---------- Create Meeting Modal ----------
function showCreateMeetingModal() {
  var projOpts = (window._cachedProjects || []).map(function(p){
    return '<option value="'+p.id+'">'+esc(p.name)+'</option>';
  }).join('');
  var agentOpts = agents.map(function(a){
    return '<label style="display:flex;align-items:center;gap:8px;padding:6px 10px;font-size:12px;cursor:pointer;border-radius:6px;transition:background 0.15s" onmouseover="this.style.background=\'rgba(255,255,255,0.04)\'" onmouseout="this.style.background=\'transparent\'">' +
      '<input type="checkbox" name="mtg-part" value="'+a.id+'">' +
      '<div style="width:24px;height:24px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700">'+(a.name||'?')[0]+'</div>' +
      '<span>'+esc((a.role?a.role+' · ':'')+a.name)+'</span></label>';
  }).join('');
  var html = '<div style="padding:24px;max-width:500px"><h3 style="margin:0 0 16px">新建会议</h3>' +
    '<input id="mtg-title" placeholder="会议标题" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<textarea id="mtg-agenda" placeholder="议程 / 背景（可选）" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;min-height:60px;resize:vertical"></textarea>' +
    '<select id="mtg-project" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="">不关联项目 (非项目型)</option>'+projOpts +
    '</select>' +
    '<div style="font-size:12px;font-weight:600;color:var(--text2);margin:10px 0 6px">选择参会 Agent</div>' +
    '<div style="max-height:200px;overflow-y:auto;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:4px;margin-bottom:14px">'+(agentOpts||'<div style="color:var(--text3);font-size:12px;padding:12px">No agents</div>')+'</div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">取消</button>' +
      '<button class="btn btn-primary" onclick="createMeeting()">创建</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function createMeeting() {
  var title = document.getElementById('mtg-title').value.trim();
  if (!title) { alert('标题不能为空'); return; }
  var agenda = document.getElementById('mtg-agenda').value.trim();
  var projId = document.getElementById('mtg-project').value;
  var parts = Array.prototype.slice.call(document.querySelectorAll('input[name="mtg-part"]:checked')).map(function(i){return i.value;});
  try {
    await api('POST', '/api/portal/meetings', {
      title: title, agenda: agenda, project_id: projId, participants: parts,
    });
    closeModal();
    renderMeetingsTab();
  } catch(e) { alert('Error: '+e.message); }
}

// ---------- Meeting Detail (三栏布局: 参会者 | 讨论区 | 任务) ----------

// Polling handle for auto-refresh while meeting is active
var _mtgPollTimer = null;

async function openMeetingDetail(mid) {
  // Clear any previous poll
  if (_mtgPollTimer) { clearInterval(_mtgPollTimer); _mtgPollTimer = null; }

  try {
    var m = await api('GET', '/api/portal/meetings/'+mid);
    var c = document.getElementById('content');

    // -- Status bar buttons --
    var statusBtns = '';
    if (m.status === 'scheduled') statusBtns += '<button class="btn btn-primary btn-sm" onclick="meetingAction(\''+mid+'\',\'start\')" style="gap:4px"><span class="material-symbols-outlined" style="font-size:16px">play_arrow</span> 开始会议</button>';
    if (m.status === 'active') statusBtns += '<button class="btn btn-ghost btn-sm" onclick="meetingCloseWithSummary(\''+mid+'\')" style="gap:4px"><span class="material-symbols-outlined" style="font-size:16px">stop</span> 结束</button>';
    if (m.status !== 'cancelled' && m.status !== 'closed') statusBtns += '<button class="btn btn-ghost btn-sm" style="color:var(--error);gap:4px" onclick="meetingAction(\''+mid+'\',\'cancel\')"><span class="material-symbols-outlined" style="font-size:16px">close</span> 取消</button>';

    var statusColor = m.status==='active'?'#22c55e':m.status==='scheduled'?'var(--primary)':m.status==='closed'?'var(--text3)':'#ef4444';
    var statusLabel = m.status==='active'?'进行中':m.status==='scheduled'?'待开始':m.status==='closed'?'已结束':'已取消';
    var statusDot = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+statusColor+';margin-right:6px'+(m.status==='active'?';animation:pulse 1.5s infinite':'')+'"></span>';

    // -- Participants panel --
    var _agList = window._cachedAgents||agents||[];
    var partHtml = (m.participants||[]).map(function(pid){
      var ag = _agList.find(function(a){return a.id===pid;});
      var name = ag ? ag.name : pid.substring(0,8);
      var role = ag ? (ag.role||'') : '';
      var initial = (name||'?')[0];
      // Color-code: different subtle colors per participant
      var colors = ['#6366f1','#22c55e','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#ec4899','#14b8a6'];
      var ci = (m.participants||[]).indexOf(pid) % colors.length;
      var bgColor = colors[ci];
      return '<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;transition:background 0.15s" onmouseover="this.style.background=\'rgba(255,255,255,0.04)\'" onmouseout="this.style.background=\'transparent\'">' +
        '<div style="width:32px;height:32px;border-radius:50%;background:'+bgColor+';color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">'+esc(initial)+'</div>' +
        '<div style="min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(name)+'</div>' +
        (role ? '<div style="font-size:10px;color:var(--text3)">'+esc(role)+'</div>' : '') +
        '</div></div>';
    }).join('');
    // Add host as first entry
    var hostDisplay = '主持人';
    partHtml = '<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;background:rgba(99,102,241,0.08)">' +
      '<div style="width:32px;height:32px;border-radius:50%;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0">主</div>' +
      '<div style="min-width:0"><div style="font-size:12px;font-weight:600;color:var(--text)">'+hostDisplay+'</div>' +
      '<div style="font-size:10px;color:var(--text3)">主持</div></div></div>' + partHtml;
    if (!(m.participants||[]).length) partHtml += '<div style="color:var(--text3);font-size:11px;padding:10px">暂无参会 Agent</div>';

    // -- Messages (chat thread) --
    var _mtgMsgRefs = [];
    var msgHtml = (m.messages||[]).map(function(x, _i){
      var ts = x.created_at ? new Date(x.created_at*1000).toLocaleTimeString() : '';
      var anchor = 'mtg-msg-card-' + mid + '-' + _i;
      _mtgMsgRefs.push({anchor: anchor, refs: x.refs || []});
      var isUser = (x.role === 'user');
      var isSystem = (x.role === 'system');

      // Resolve display name: agent lookup > sender_name > sender
      var ag = (window._cachedAgents||agents||[]).find(function(a){return a.id===x.sender;});
      var senderName = isUser ? '主持人' : (ag ? ag.name : (x.sender_name || x.sender || '?'));
      // Clean up sender_name that looks like "role-name" from backend
      if (!isUser && ag && x.sender_name && x.sender_name.indexOf('-')>0) senderName = ag.name;

      if (isSystem) {
        // System messages (progress updates, file ops) — compact centered style
        return '<div style="text-align:center;padding:4px 0;margin:4px 0">' +
          '<span style="font-size:11px;color:var(--text3);background:rgba(255,255,255,0.03);padding:3px 10px;border-radius:10px">'+esc(x.content||'')+'</span>' +
        '</div>';
      }

      var avatarInitial = ag ? (ag.name||'?')[0] : (isUser ? '主' : (senderName||'?')[0]);
      var nameColor = isUser ? 'var(--primary)' : '#22c55e';
      var agRole = ag ? (ag.role||'') : '';
      var nameDisplay = senderName + (agRole && !isUser ? ' · '+agRole : '');

      return '<div style="display:flex;gap:10px;padding:10px 0;align-items:flex-start">' +
        '<div style="width:32px;height:32px;border-radius:50%;background:'+(isUser?'var(--primary)':'rgba(34,197,94,0.15)')+';color:'+(isUser?'#fff':'#22c55e')+';display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0;margin-top:2px">'+esc(avatarInitial)+'</div>' +
        '<div style="flex:1;min-width:0">' +
          '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:3px">' +
            '<span style="font-size:13px;font-weight:700;color:'+nameColor+'">'+esc(nameDisplay)+'</span>' +
            '<span style="font-size:10px;color:var(--text3)">'+ts+'</span>' +
          '</div>' +
          '<div style="font-size:13px;color:var(--text);white-space:pre-wrap;line-height:1.6">'+esc(x.content||'')+'</div>' +
          '<div id="'+anchor+'" class="chat-msg-content" style="margin-top:4px"></div>' +
        '</div>' +
      '</div>';
    }).join('');
    if (!msgHtml) msgHtml = '<div style="color:var(--text3);font-size:12px;text-align:center;padding:40px 0">会议尚未开始讨论<br><span style="font-size:11px">点击「开始会议」后发送第一条消息</span></div>';

    // -- Assignments panel --
    var asgHtml = (m.assignments||[]).map(function(a){
      var ag = (window._cachedAgents||agents||[]).find(function(ag){return ag.id===a.assignee_agent_id;});
      var agName = ag ? ag.name : (a.assignee_agent_id || '待分配');
      var stColor = a.status==='done'?'#22c55e':a.status==='in_progress'?'#f59e0b':a.status==='cancelled'?'var(--text3)':'var(--primary)';
      var stLabel = a.status==='done'?'已完成':a.status==='in_progress'?'进行中':a.status==='cancelled'?'已取消':'待处理';
      return '<div style="padding:10px;background:var(--surface);border-radius:8px;margin-bottom:6px;border-left:3px solid '+stColor+'">' +
        '<div style="font-weight:600;font-size:12px;color:var(--text)">'+esc(a.title)+'</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">' +
          '<div style="font-size:10px;color:var(--text3)">→ '+esc(agName)+(a.due_hint?' · '+esc(a.due_hint):'')+'</div>' +
          '<select onchange="updateMeetingAssignment(\''+mid+'\',\''+a.id+'\',this.value)" style="background:var(--bg);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:var(--text);font-size:10px;padding:2px 4px">' +
            ['open','in_progress','done','cancelled'].map(function(s){
              var sl = s==='done'?'已完成':s==='in_progress'?'进行中':s==='cancelled'?'已取消':'待处理';
              return '<option value="'+s+'"'+(a.status===s?' selected':'')+'>'+sl+'</option>';
            }).join('') +
          '</select>' +
        '</div>' +
      '</div>';
    }).join('');
    if (!asgHtml) asgHtml = '<div style="color:var(--text3);font-size:11px;text-align:center;padding:20px 0">讨论中产生的任务将显示在这里</div>';

    // -- Files panel (workspace) --
    var filesHtml = '';
    if (m.file_count > 0 || m.workspace_dir) {
      filesHtml = '<div style="margin-top:12px">' +
        '<div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;margin-bottom:6px">共享文件</div>' +
        '<div id="mtg-files-area-'+mid+'"><div style="font-size:10px;color:var(--text3)">加载中...</div></div>' +
      '</div>';
    }

    // -- FULL LAYOUT --
    c.innerHTML =
      '<div style="display:flex;flex-direction:column;height:calc(100vh - 60px)">' +
        // ---- Header ----
        '<div style="padding:12px 18px;border-bottom:1px solid rgba(255,255,255,0.06);flex-shrink:0">' +
          '<div style="display:flex;justify-content:space-between;align-items:center">' +
            '<div style="display:flex;align-items:center;gap:12px">' +
              '<button class="btn btn-ghost btn-sm" onclick="renderMeetingsTab()" style="padding:4px"><span class="material-symbols-outlined" style="font-size:18px">arrow_back</span></button>' +
              '<div>' +
                '<h2 style="margin:0;font-size:18px;font-weight:800;display:flex;align-items:center;gap:6px">'+statusDot+esc(m.title)+'</h2>' +
                '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+statusLabel+(m.project_id?' · 项目: '+esc(m.project_id):'')+(m.agenda?' · '+esc(m.agenda):'')+'</div>' +
              '</div>' +
            '</div>' +
            '<div style="display:flex;gap:6px">'+statusBtns+'</div>' +
          '</div>' +
        '</div>' +

        // ---- Summary banner (if closed) ----
        (m.summary ? '<div style="padding:10px 18px;background:rgba(34,197,94,0.06);border-bottom:1px solid rgba(34,197,94,0.15);flex-shrink:0"><span style="font-size:11px;font-weight:700;color:#22c55e">会议纪要:</span> <span style="font-size:12px;color:var(--text2)">'+esc(m.summary)+'</span></div>' : '') +

        // ---- Three-column body ----
        '<div style="display:flex;flex:1;min-height:0;overflow:hidden">' +
          // == Left: Participants ==
          '<div style="width:180px;flex-shrink:0;border-right:1px solid rgba(255,255,255,0.06);overflow-y:auto;padding:12px 8px">' +
            '<div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase;padding:0 10px 8px">参会者 ('+(m.participants||[]).length+')</div>' +
            partHtml +
            filesHtml +
          '</div>' +

          // == Center: Chat / Discussion ==
          '<div style="flex:1;display:flex;flex-direction:column;min-width:0">' +
            // Messages scrollable area
            '<div id="mtg-chat-scroll" style="flex:1;overflow-y:auto;padding:12px 18px">' +
              msgHtml +
            '</div>' +
            // Input area (only if meeting not closed/cancelled)
            (m.status !== 'closed' && m.status !== 'cancelled' ?
              '<div style="padding:10px 18px;border-top:1px solid rgba(255,255,255,0.06);flex-shrink:0">' +
                '<div id="mtg-attach-preview-'+mid+'" style="display:none;flex-wrap:wrap;gap:6px;margin-bottom:6px"></div>' +
                '<div style="display:flex;gap:8px;align-items:flex-end">' +
                  '<input type="file" id="mtg-file-input-'+mid+'" multiple accept="image/*,.pdf,.doc,.docx,.txt,.csv,.json,.yaml,.yml,.md" style="display:none" onchange="handleMtgAttach(\''+mid+'\',this)">' +
                  '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'mtg-file-input-'+mid+'\').click()" title="上传文件" style="flex-shrink:0;padding:6px"><span class="material-symbols-outlined" style="font-size:18px">attach_file</span></button>' +
                  '<textarea id="mtg-msg-input" placeholder="'+(m.status==='active'?'发送消息，所有参会 Agent 将按顺序回复...':'会议未开始，请先点击「开始会议」')+'" style="flex:1;padding:10px 14px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:10px;color:var(--text);font-size:13px;min-height:42px;max-height:120px;resize:none;line-height:1.4" onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();meetingPostMessage(\''+mid+'\')}"'+(m.status!=='active'?' disabled':'')+'></textarea>' +
                  '<button class="btn btn-primary btn-sm" onclick="meetingPostMessage(\''+mid+'\')" style="flex-shrink:0;padding:8px 16px;border-radius:10px"'+(m.status!=='active'?' disabled':'')+'>发送</button>' +
                '</div>' +
              '</div>'
            : '') +
          '</div>' +

          // == Right: Assignments / Tasks ==
          '<div style="width:260px;flex-shrink:0;border-left:1px solid rgba(255,255,255,0.06);overflow-y:auto;padding:12px">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
              '<div style="font-size:11px;font-weight:700;color:var(--text3);text-transform:uppercase">任务 ('+(m.assignments||[]).length+')</div>' +
              (m.status === 'active' ? '<button class="btn btn-ghost btn-xs" onclick="showMeetingAssignmentModal(\''+mid+'\')" style="font-size:11px">+ 新增</button>' : '') +
            '</div>' +
            asgHtml +
          '</div>' +
        '</div>' +
      '</div>' +
      '<style>@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}</style>';

    // -- Post-render: scroll to bottom --
    var chatScroll = document.getElementById('mtg-chat-scroll');
    if (chatScroll) chatScroll.scrollTop = chatScroll.scrollHeight;

    // -- Post-render: attach file cards --
    try {
      for (var _ri = 0; _ri < _mtgMsgRefs.length; _ri++) {
        var rec = _mtgMsgRefs[_ri];
        if (!rec || !rec.refs || !rec.refs.length) continue;
        var host = document.getElementById(rec.anchor);
        if (host && typeof _appendFileCards === 'function') _appendFileCards(host, rec.refs);
      }
    } catch(_e) { console.log('[meetingDetail] file card attach failed', _e); }

    // -- Post-render: load files list --
    if (m.workspace_dir) _loadMeetingFiles(mid);

    // -- Auto-refresh: poll every 3s while meeting is active --
    if (m.status === 'active') {
      _mtgPollTimer = setInterval(function(){
        _refreshMeetingMessages(mid);
      }, 3000);
    }

  } catch(e) {
    alert('Error: '+e.message);
  }
}

// Light refresh: only update messages + assignments without full re-render
var _mtgLastMsgCount = 0;
async function _refreshMeetingMessages(mid) {
  try {
    var m = await api('GET', '/api/portal/meetings/'+mid);
    var newCount = (m.messages||[]).length;
    var newAsgCount = (m.assignments||[]).length;
    // Only re-render if something changed
    if (newCount !== _mtgLastMsgCount || newAsgCount !== (m._prevAsgCount||0)) {
      _mtgLastMsgCount = newCount;
      openMeetingDetail(mid);
    }
  } catch(e) {
    // Silently ignore poll errors
  }
}

// ---------- Meeting Files ----------
async function _loadMeetingFiles(mid) {
  var area = document.getElementById('mtg-files-area-'+mid);
  if (!area) return;
  try {
    var r = await api('GET', '/api/portal/meetings/'+mid+'/files');
    var files = r.files || [];
    if (!files.length) {
      area.innerHTML = '<div style="font-size:10px;color:var(--text3)">暂无文件</div>';
      return;
    }
    area.innerHTML = files.map(function(f){
      var sizeStr = f.size < 1024 ? f.size+'B' : f.size < 1048576 ? Math.round(f.size/1024)+'KB' : (f.size/1048576).toFixed(1)+'MB';
      return '<div style="display:flex;align-items:center;gap:6px;padding:4px 6px;border-radius:4px;font-size:10px;cursor:pointer;transition:background 0.15s" onmouseover="this.style.background=\'rgba(255,255,255,0.04)\'" onmouseout="this.style.background=\'transparent\'" onclick="window.open(\'/api/portal/meetings/'+mid+'/files/'+encodeURIComponent(f.name)+'\')" title="点击下载">' +
        '<span class="material-symbols-outlined" style="font-size:14px;color:var(--primary)">description</span>' +
        '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)">'+esc(f.name)+'</span>' +
        '<span style="color:var(--text3)">'+sizeStr+'</span>' +
      '</div>';
    }).join('');
  } catch(e) {
    area.innerHTML = '<div style="font-size:10px;color:var(--error)">Error</div>';
  }
}

// ---------- Meeting Actions ----------
async function meetingAction(mid, action) {
  try {
    await api('POST', '/api/portal/meetings/'+mid+'/'+action, {});
    openMeetingDetail(mid);
  } catch(e) { alert('Error: '+e.message); }
}

async function meetingCloseWithSummary(mid) {
  var s = prompt('会议纪要 / 结论 (可留空):') || '';
  try {
    await api('POST', '/api/portal/meetings/'+mid+'/close', {summary: s});
    openMeetingDetail(mid);
  } catch(e) { alert('Error: '+e.message); }
}

// ============ Meeting Attachments ============
var _mtgAttachments = {};

function _mtgAttachList(mid) {
  if (!_mtgAttachments[mid]) _mtgAttachments[mid] = [];
  return _mtgAttachments[mid];
}

function _renderMtgAttachPreview(mid) {
  var box = document.getElementById('mtg-attach-preview-'+mid);
  if (!box) return;
  var list = _mtgAttachList(mid);
  if (list.length === 0) { box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = list.map(function(a, idx) {
    var thumb = a.preview_url
      ? '<img src="'+a.preview_url+'" style="width:28px;height:28px;object-fit:cover;border-radius:3px">'
      : '<span class="material-symbols-outlined" style="font-size:16px;color:var(--text3)">draft</span>';
    return '<div style="display:inline-flex;align-items:center;gap:4px;background:var(--surface);border:1px solid rgba(255,255,255,0.08);border-radius:4px;padding:2px 6px;font-size:10px;color:var(--text)">' +
      thumb + '<span style="max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(a.name)+'</span>' +
      '<button onclick="_mtgAttachList(\''+mid+'\').splice('+idx+',1);_renderMtgAttachPreview(\''+mid+'\')" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:0"><span class="material-symbols-outlined" style="font-size:12px">close</span></button>' +
    '</div>';
  }).join('');
}

function handleMtgAttach(mid, fileInput) {
  if (!fileInput || !fileInput.files || !fileInput.files.length) return;
  var list = _mtgAttachList(mid);
  var files = Array.prototype.slice.call(fileInput.files, 0, Math.max(0, 10 - list.length));
  files.forEach(function(f) {
    if (f.size > 10*1024*1024) { alert('File "'+f.name+'" exceeds 10 MB.'); return; }
    var reader = new FileReader();
    reader.onload = function(e) {
      var dataUrl = e.target.result || '';
      var b64 = dataUrl.indexOf(',') >= 0 ? dataUrl.split(',')[1] : dataUrl;
      var isImage = (f.type||'').indexOf('image/') === 0;
      list.push({ name: f.name, mime: f.type||'application/octet-stream', size: f.size, data_base64: b64, preview_url: isImage ? dataUrl : '' });
      _renderMtgAttachPreview(mid);
    };
    reader.readAsDataURL(f);
  });
  fileInput.value = '';
}

// ---------- Send Message ----------
async function meetingPostMessage(mid) {
  var el = document.getElementById('mtg-msg-input');
  var v = (el && el.value || '').trim();
  var attachments = _mtgAttachList(mid).slice();
  if (!v && !attachments.length) return;

  // Disable input while sending
  if (el) { el.disabled = true; el.value = ''; }

  var msgBody = {content: v || '(attached files)', role: 'user'};
  if (attachments.length) {
    msgBody.attachments = attachments.map(function(a) {
      return { name: a.name, mime: a.mime, data_base64: a.data_base64 };
    });
    _mtgAttachments[mid] = [];
    _renderMtgAttachPreview(mid);
  }
  try {
    await api('POST', '/api/portal/meetings/'+mid+'/messages', msgBody);
    // Show "Agent 正在思考..." indicator
    var chatScroll = document.getElementById('mtg-chat-scroll');
    if (chatScroll) {
      chatScroll.insertAdjacentHTML('beforeend',
        '<div id="mtg-thinking-indicator" style="text-align:center;padding:12px;color:var(--text3);font-size:12px">' +
          '<span style="display:inline-block;animation:pulse 1.5s infinite">💭 Agent 正在按顺序发言中...</span>' +
        '</div>');
      chatScroll.scrollTop = chatScroll.scrollHeight;
    }
    // Start polling for agent replies
    _mtgLastMsgCount = 0; // force refresh
  } catch(e) {
    alert('Error: '+e.message);
    if (el) { el.disabled = false; el.value = v; }
  }
}

// ---------- Assignment Modal ----------
function showMeetingAssignmentModal(mid) {
  var agentOpts = '<option value="">选择负责 Agent</option>' + agents.map(function(a){
    return '<option value="'+a.id+'">'+esc((a.role?a.role+' · ':'')+a.name)+'</option>';
  }).join('');
  var html = '<div style="padding:24px;max-width:480px"><h3 style="margin:0 0 16px">新建任务</h3>' +
    '<p style="font-size:12px;color:var(--text3);margin:-8px 0 14px">从讨论中明确的行动项</p>' +
    '<input id="asg-title" placeholder="任务标题" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<textarea id="asg-desc" placeholder="描述 / 背景 (可选)" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;min-height:60px;resize:vertical"></textarea>' +
    '<select id="asg-assignee" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">'+agentOpts+'</select>' +
    '<input id="asg-due" placeholder="截止提示 (e.g. 明天 17:00)" style="width:100%;padding:10px;margin-bottom:14px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">取消</button>' +
      '<button class="btn btn-primary" onclick="createMeetingAssignment(\''+mid+'\')">创建</button>' +
    '</div></div>';
  showModalHTML(html);
}

async function createMeetingAssignment(mid) {
  var title = document.getElementById('asg-title').value.trim();
  if (!title) { alert('标题不能为空'); return; }
  try {
    await api('POST', '/api/portal/meetings/'+mid+'/assignments', {
      title: title,
      description: document.getElementById('asg-desc').value.trim(),
      assignee_agent_id: document.getElementById('asg-assignee').value,
      due_hint: document.getElementById('asg-due').value.trim(),
    });
    closeModal();
    openMeetingDetail(mid);
  } catch(e) { alert('Error: '+e.message); }
}

async function updateMeetingAssignment(mid, aid, status) {
  if (!status) return;
  try {
    await api('POST', '/api/portal/meetings/'+mid+'/assignments/'+aid+'/update', {status: status});
    openMeetingDetail(mid);
  } catch(e) { alert('Error: '+e.message); }
}


// ============ Task Center (定时任务 + 独立任务统一视图) ============
async function renderTaskCenterTab() {
  var c = document.getElementById('content');
  c.innerHTML =
    '<div style="padding:18px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">' +
        '<div><h2 style="margin:0;font-size:22px;font-weight:800">任务中心</h2>' +
        '<p style="font-size:12px;color:var(--text3);margin-top:4px">定时任务 (Agent / Workflow) + 独立任务 统一视图</p></div>' +
        '<div style="display:flex;gap:8px">' +
          '<button class="btn btn-primary btn-sm" onclick="showCreateJob()"><span class="material-symbols-outlined" style="font-size:16px">schedule</span> 新建定时任务</button>' +
          '<button class="btn btn-sm" onclick="showCreateStandaloneTaskModal()"><span class="material-symbols-outlined" style="font-size:16px">add</span> 新建独立任务</button>' +
        '</div>' +
      '</div>' +
      '<div id="tc-content"><div style="color:var(--text3);font-size:12px">Loading…</div></div>' +
    '</div>';
  loadTaskCenter();
}

async function loadTaskCenter() {
  var area = document.getElementById('tc-content');
  if (!area) return;
  try {
    var results = await Promise.all([
      api('GET', '/api/portal/scheduler/jobs'),
      api('GET', '/api/portal/standalone-tasks'),
      api('GET', '/api/portal/agents').catch(function(){ return {agents: []}; }),
      api('GET', '/api/portal/workflows').catch(function(){ return {workflows: []}; }),
    ]);
    var jobsResp = results[0] || {};
    var stResp = results[1] || {};
    var agentsResp = results[2] || {};
    var wfResp = results[3] || {};
    var jobs = jobsResp.jobs || [];
    var standaloneTasks = (stResp.tasks || []).map(function(t){ t._src='standalone'; return t; });
    var agentsList = agentsResp.agents || [];
    var wfList = wfResp.workflows || [];
    var agentNameById = {};
    agentsList.forEach(function(a){ agentNameById[a.id] = (a.role?a.role+'-':'')+a.name; });
    var wfNameById = {};
    wfList.forEach(function(w){ wfNameById[w.id||w.template_id] = w.name||'(unnamed)'; });

    // Split scheduled jobs into agent-chat targets vs workflow targets
    var chatJobs = [];
    var workflowJobs = [];
    jobs.forEach(function(j){
      if ((j.target_type||'chat') === 'workflow' || j.workflow_id) workflowJobs.push(j);
      else chatJobs.push(j);
    });

    var renderJobCard = function(job, kind){
      var enabled = !!job.enabled;
      var statusIcon = enabled
        ? (job.last_status==='success'?'✅':job.last_status==='failed'?'❌':'⏱')
        : '⏸';
      var nextRun = job.next_run_at ? new Date(job.next_run_at*1000).toLocaleString() : '-';
      var lastRun = job.last_run_at ? new Date(job.last_run_at*1000).toLocaleString() : '从未';
      var targetLine = '';
      if (kind === 'workflow') {
        var wfName = wfNameById[job.workflow_id] || job.workflow_id || '(未指定)';
        var defaultAgent = job.agent_id ? (agentNameById[job.agent_id] || job.agent_id) : '(全部默认)';
        targetLine =
          '<div style="font-size:11px;color:var(--text2);margin-top:4px">' +
            '🔀 Workflow: <strong>'+esc(wfName)+'</strong>' +
          '</div>' +
          '<div style="font-size:10px;color:var(--text3);margin-top:2px">默认 Agent: '+esc(defaultAgent)+'</div>';
      } else {
        var agName = job.agent_id ? (agentNameById[job.agent_id] || job.agent_id) : '(未指定)';
        targetLine =
          '<div style="font-size:11px;color:var(--text2);margin-top:4px">👤 Agent: <strong>'+esc(agName)+'</strong></div>';
      }
      return '<div style="background:var(--surface);border-radius:10px;padding:12px 14px;border:1px solid rgba(255,255,255,0.06);margin-bottom:8px">' +
        '<div style="display:flex;align-items:center;gap:8px">' +
          '<span style="font-size:14px;line-height:1">'+statusIcon+'</span>' +
          '<span style="font-weight:700;font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(job.name||'(unnamed)')+'</span>' +
          '<span style="font-size:10px;background:rgba(255,255,255,0.06);padding:2px 8px;border-radius:10px;color:var(--text3)">'+esc(job.job_type||'recurring')+'</span>' +
        '</div>' +
        targetLine +
        '<div style="font-size:10px;color:var(--text3);margin-top:6px;font-family:monospace">⏰ '+esc(job.cron_expr||'one-time')+'</div>' +
        '<div style="font-size:10px;color:var(--text3);margin-top:2px">下次: '+esc(nextRun)+' · 上次: '+esc(lastRun)+' · 执行 '+(job.run_count||0)+' 次</div>' +
        '<div style="display:flex;gap:6px;margin-top:8px;border-top:1px solid rgba(255,255,255,0.05);padding-top:8px">' +
          '<button class="btn btn-primary btn-xs" onclick="triggerJob(\''+job.id+'\')">▶ 立即运行</button>' +
          '<button class="btn btn-ghost btn-xs" onclick="toggleJob(\''+job.id+'\','+(!enabled)+')">'+(enabled?'暂停':'恢复')+'</button>' +
          '<button class="btn btn-ghost btn-xs" onclick="viewJobHistory(\''+job.id+'\')">历史</button>' +
          '<button class="btn btn-ghost btn-xs" style="margin-left:auto;color:var(--error)" onclick="deleteJob(\''+job.id+'\')">删除</button>' +
        '</div>' +
      '</div>';
    };

    var chatJobsHtml = chatJobs.map(function(j){ return renderJobCard(j, 'chat'); }).join('')
      || '<div style="color:var(--text3);font-size:12px;padding:12px;text-align:center">无 Agent 定时任务</div>';
    var wfJobsHtml = workflowJobs.map(function(j){ return renderJobCard(j, 'workflow'); }).join('')
      || '<div style="color:var(--text3);font-size:12px;padding:12px;text-align:center">无 Workflow 定时任务</div>';

    var stRows = standaloneTasks.map(function(t){
      var icon = t.status==='done'?'✅':t.status==='in_progress'?'⏳':t.status==='blocked'?'🚫':t.status==='cancelled'?'❌':'📋';
      var statusSelect = '<select onchange="updateStandaloneTask(\''+t.id+'\',{status:this.value})" style="background:var(--surface2);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:var(--text);font-size:10px;padding:2px 6px">' +
        ['todo','in_progress','done','blocked','cancelled'].map(function(s){return '<option value="'+s+'"'+(t.status===s?' selected':'')+'>'+s+'</option>';}).join('') +
      '</select>';
      return '<div style="background:var(--surface);border-radius:8px;padding:10px 12px;border:1px solid rgba(255,255,255,0.05);margin-bottom:6px">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
          '<div style="flex:1;min-width:0"><span style="font-weight:600;font-size:13px">'+icon+' '+esc(t.title||'')+'</span>' +
          (t.description?'<div style="font-size:10px;color:var(--text3);margin-top:2px">'+esc(t.description.slice(0,120))+'</div>':'') +
          '<div style="font-size:10px;color:var(--text3);margin-top:3px">'+esc(t.priority||'normal')+(t.due_hint?' · ⏰ '+esc(t.due_hint):'')+' · → '+esc(t.assigned_to||'—')+(t.source_meeting_id?' · 来自会议':'')+'</div>' +
          '</div>' +
          '<div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end">'+statusSelect+
            '<button class="btn btn-ghost btn-xs" style="color:var(--error)" onclick="deleteStandaloneTask(\''+t.id+'\')">删除</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    }).join('') || '<div style="color:var(--text3);font-size:12px;padding:12px;text-align:center">无独立任务</div>';

    area.innerHTML =
      '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px">' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">⏱ 定时任务 · Agent ('+chatJobs.length+')</div>'+chatJobsHtml+'</div>' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">🔀 定时任务 · Workflow ('+workflowJobs.length+')</div>'+wfJobsHtml+'</div>' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">⚡ 独立任务 ('+standaloneTasks.length+')</div>'+stRows+'</div>' +
      '</div>';
  } catch(e) {
    area.innerHTML = '<div style="color:var(--error)">Error: '+esc(e.message)+'</div>';
  }
}

function showCreateStandaloneTaskModal() {
  var agentOpts = '<option value="">Unassigned</option>' + agents.map(function(a){
    return '<option value="'+a.id+'">'+esc((a.role||'')+'-'+a.name)+'</option>';
  }).join('');
  var html = '<div style="padding:24px;max-width:480px"><h3 style="margin:0 0 16px">新建独立任务</h3>' +
    '<input id="st-title" placeholder="任务标题" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<textarea id="st-desc" placeholder="描述 (可选)" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px;min-height:60px;resize:vertical"></textarea>' +
    '<select id="st-assignee" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">'+agentOpts+'</select>' +
    '<select id="st-priority" style="width:100%;padding:10px;margin-bottom:10px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
      '<option value="normal">Normal</option><option value="low">Low</option><option value="high">High</option><option value="urgent">Urgent</option>' +
    '</select>' +
    '<input id="st-due" placeholder="截止提示 (e.g. \"明天下班前\")" style="width:100%;padding:10px;margin-bottom:14px;background:var(--surface);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:var(--text);font-size:13px">' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-ghost" onclick="closeModal()">取消</button>' +
      '<button class="btn btn-primary" onclick="createStandaloneTask()">创建</button>' +
    '</div></div>';
  showModalHTML(html);
}
async function createStandaloneTask() {
  var title = document.getElementById('st-title').value.trim();
  if (!title) { alert('标题不能为空'); return; }
  try {
    await api('POST', '/api/portal/standalone-tasks', {
      title: title,
      description: document.getElementById('st-desc').value.trim(),
      assigned_to: document.getElementById('st-assignee').value,
      priority: document.getElementById('st-priority').value,
      due_hint: document.getElementById('st-due').value.trim(),
    });
    closeModal();
    loadTaskCenter();
  } catch(e) { alert('Error: '+e.message); }
}
async function updateStandaloneTask(tid, fields) {
  try { await api('POST', '/api/portal/standalone-tasks/'+tid, fields); loadTaskCenter(); }
  catch(e) { alert('Error: '+e.message); }
}
async function deleteStandaloneTask(tid) {
  if (!confirm('删除此任务？')) return;
  try { await api('POST', '/api/portal/standalone-tasks/'+tid+'/delete', {}); loadTaskCenter(); }
  catch(e) { alert('Error: '+e.message); }
}

// ============ Orchestration Visualization (force-directed SVG) ============
function renderOrchestration() {
  var c = document.getElementById('content');
  c.innerHTML =
    '<div style="padding:18px">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">'
    + '<div><h2 style="margin:0;font-family:\'Plus Jakarta Sans\',sans-serif;font-size:22px;font-weight:800">编排可视化</h2>'
    + '<p style="font-size:12px;color:var(--text3);margin-top:4px">Orchestration · 项目 / Agent / 任务关系图</p></div>'
    + '<div style="display:flex;align-items:center;gap:12px"><div id="orch-stats" style="font-size:12px;color:var(--text3)"></div>'
    + '<button class="btn btn-sm" onclick="renderOrchestration()"><span class="material-symbols-outlined" style="font-size:14px">refresh</span> 刷新</button></div></div>'
    + '<div id="orch-legend" style="font-size:11px;color:var(--text3);margin-bottom:8px">'
    + '<span style="display:inline-block;width:10px;height:10px;background:#5b8def;border-radius:2px;margin:0 4px 0 0;vertical-align:middle"></span>项目'
    + '<span style="display:inline-block;width:10px;height:10px;background:#22c55e;border-radius:50%;margin:0 4px 0 12px;vertical-align:middle"></span>Agent'
    + '<span style="display:inline-block;width:10px;height:10px;background:#a855f7;border-radius:50%;margin:0 4px 0 12px;vertical-align:middle"></span>子 Agent'
    + '<span style="display:inline-block;width:10px;height:10px;background:#f59e0b;border-radius:2px;margin:0 4px 0 12px;vertical-align:middle"></span>任务</div>'
    + '<div id="orch-svg-wrap" style="border:1px solid var(--border);border-radius:8px;background:var(--surface);overflow:auto">'
    + '<svg id="orch-svg" width="100%" height="640" style="display:block"></svg></div>'
    + '<div id="orch-detail" style="margin-top:12px;font-size:12px;color:var(--text2)"></div></div>';

  fetch('/api/portal/orchestration').then(function(r){return r.json();}).then(function(g){
    var stats = g.stats || {};
    document.getElementById('orch-stats').textContent =
      '项目 ' + (stats.projects||0) + ' · Agent ' + (stats.agents||0)
      + ' · 子 Agent ' + (stats.subagents||0) + ' · 任务 ' + (stats.tasks||0);
    _drawOrchestrationGraph(g);
  }).catch(function(e){
    document.getElementById('orch-svg-wrap').innerHTML =
      '<div style="padding:20px;color:var(--error)">加载失败: '+esc(String(e))+'</div>';
  });
}

function _drawOrchestrationGraph(g) {
  var svg = document.getElementById('orch-svg');
  if (!svg) return;
  var nodes = (g.nodes || []).slice();
  var edges = (g.edges || []).slice();
  if (!nodes.length) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#888">暂无数据</text>';
    return;
  }
  var W = svg.clientWidth || 1000;
  var H = 640;

  // Simple layered layout: projects → agents → tasks
  var byType = { project: [], agent: [], subagent: [], task: [] };
  nodes.forEach(function(n){ (byType[n.type] || (byType[n.type]=[])).push(n); });
  var lanes = [
    { type: 'project',  y: 80,  color: '#5b8def', shape: 'rect' },
    { type: 'agent',    y: 240, color: '#22c55e', shape: 'circle' },
    { type: 'subagent', y: 380, color: '#a855f7', shape: 'circle' },
    { type: 'task',     y: 540, color: '#f59e0b', shape: 'rect' },
  ];
  var posMap = {};
  lanes.forEach(function(lane) {
    var arr = byType[lane.type] || [];
    var n = arr.length;
    if (!n) return;
    var step = W / (n + 1);
    arr.forEach(function(node, i) {
      posMap[node.id] = { x: step * (i+1), y: lane.y, color: lane.color, shape: lane.shape, node: node };
    });
  });

  // Build SVG
  var defs = '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#888"/></marker></defs>';
  var edgeSvg = edges.map(function(e) {
    var a = posMap[e.from], b = posMap[e.to];
    if (!a || !b) return '';
    var color = e.type === 'parent' ? '#a855f7' :
                e.type === 'assigned' ? '#f59e0b' :
                e.type === 'belongs_to' ? '#94a3b8' : '#5b8def';
    var dash = e.type === 'belongs_to' ? '3,3' : '0';
    return '<line x1="'+a.x+'" y1="'+a.y+'" x2="'+b.x+'" y2="'+b.y+'" stroke="'+color+'" stroke-width="1.2" stroke-dasharray="'+dash+'" marker-end="url(#arrow)" opacity="0.65"/>';
  }).join('');
  var nodeSvg = nodes.map(function(n) {
    var p = posMap[n.id]; if (!p) return '';
    var label = (n.label || n.id).replace(/[<>&]/g, '');
    var status = n.status || '';
    var sub = '';
    if (n.type === 'task' && n.step_total > 0) {
      sub = n.step_done + '/' + n.step_total + ' steps';
    } else if (n.type === 'project') {
      sub = (n.task_count||0) + ' tasks · ' + (n.member_count||0) + ' members';
    } else if (n.type === 'agent' || n.type === 'subagent') {
      sub = (n.role||'') + ' · ' + (n.task_count||0) + ' open';
    }
    var shape;
    if (p.shape === 'circle') {
      shape = '<circle cx="'+p.x+'" cy="'+p.y+'" r="22" fill="'+p.color+'" stroke="#fff" stroke-width="2"/>';
    } else {
      shape = '<rect x="'+(p.x-44)+'" y="'+(p.y-18)+'" width="88" height="36" rx="6" fill="'+p.color+'" stroke="#fff" stroke-width="2"/>';
    }
    var clickHandler = "onclick=\"_orchSelect('"+n.id+"')\"";
    return '<g style="cursor:pointer" '+clickHandler+'>'
      + shape
      + '<text x="'+p.x+'" y="'+(p.y+44)+'" text-anchor="middle" font-size="11" fill="var(--text)" font-weight="600">'+label.substring(0,18)+'</text>'
      + '<text x="'+p.x+'" y="'+(p.y+58)+'" text-anchor="middle" font-size="9" fill="var(--text3)">'+esc(sub)+'</text>'
      + (status ? '<text x="'+p.x+'" y="'+(p.y+4)+'" text-anchor="middle" font-size="9" fill="#fff" font-weight="700">'+esc(status.substring(0,5))+'</text>' : '')
      + '</g>';
  }).join('');
  svg.innerHTML = defs + edgeSvg + nodeSvg;
  window._orchData = g;
}

function _orchSelect(nid) {
  var g = window._orchData; if (!g) return;
  var n = (g.nodes||[]).find(function(x){return x.id===nid;});
  if (!n) return;
  var lines = [];
  Object.keys(n).forEach(function(k){
    if (k === 'id' || k === 'type') return;
    lines.push('<div><span style="color:var(--text3)">'+esc(k)+':</span> '+esc(String(n[k])) + '</div>');
  });
  var related = (g.edges||[]).filter(function(e){return e.from===nid || e.to===nid;});

  // Build navigation buttons based on node type
  var navBtns = '';
  var realId = '';
  if (n.type === 'project') {
    realId = nid.replace(/^proj:/, '');
    navBtns =
      '<button class="btn btn-primary btn-sm" onclick="_orchOpenProject(\''+esc(realId)+'\')">'
      + '<span class="material-symbols-outlined" style="font-size:14px">open_in_new</span> 打开项目</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="_orchOpenProjectTab(\''+esc(realId)+'\',\'goals\')">目标</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="_orchOpenProjectTab(\''+esc(realId)+'\',\'milestones\')">里程碑</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="_orchOpenProjectTab(\''+esc(realId)+'\',\'chat\')">协作</button>';
  } else if (n.type === 'agent' || n.type === 'subagent') {
    realId = nid.replace(/^agent:/, '');
    navBtns =
      '<button class="btn btn-primary btn-sm" onclick="_orchOpenAgent(\''+esc(realId)+'\')">'
      + '<span class="material-symbols-outlined" style="font-size:14px">smart_toy</span> 进入 Agent</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="showAgentMemoryView(\''+esc(realId)+'\')">'
      + '<span class="material-symbols-outlined" style="font-size:14px">psychology</span> 记忆视图</button>';
  } else if (n.type === 'task') {
    // task:projId:taskId
    var rest = nid.replace(/^task:/, '');
    var colon = rest.indexOf(':');
    var projId = colon > 0 ? rest.substring(0, colon) : rest;
    var taskId = colon > 0 ? rest.substring(colon + 1) : '';
    navBtns =
      '<button class="btn btn-primary btn-sm" onclick="_orchOpenTask(\''+esc(projId)+'\',\''+esc(taskId)+'\')">'
      + '<span class="material-symbols-outlined" style="font-size:14px">task</span> 打开任务</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="_orchOpenProject(\''+esc(projId)+'\')">所属项目</button>';
    if (n.assigned_to) {
      navBtns += '<button class="btn btn-ghost btn-sm" onclick="_orchOpenAgent(\''+esc(n.assigned_to)+'\')">执行 Agent</button>';
    }
  }

  document.getElementById('orch-detail').innerHTML =
    '<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">'
    + '  <div style="font-weight:600">'+esc(n.label||n.id)+'</div>'
    + '  <div style="font-size:10px;padding:2px 6px;background:var(--surface2);border-radius:4px;color:var(--text3)">'+esc(n.type||'')+'</div>'
    + '</div>'
    + (navBtns ? '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">'+navBtns+'</div>' : '')
    + '<div style="font-size:11px;color:var(--text2);line-height:1.6">'+lines.join('')+'</div>'
    + '<div style="margin-top:8px;font-size:11px;color:var(--text3)">关联边: '+related.length+' 条</div></div>';
}

// Navigation helpers for orchestration click-through
function _orchOpenProject(projId) {
  if (typeof openProject === 'function') { openProject(projId); }
  else { currentView = 'project_detail'; currentProject = projId; renderCurrentView(); }
}
function _orchOpenProjectTab(projId, tab) {
  window._projectDetailTab = window._projectDetailTab || {};
  window._projectDetailTab[projId] = tab;
  _orchOpenProject(projId);
}
function _orchOpenAgent(agentId) {
  if (typeof showAgentView === 'function') { showAgentView(agentId, null); }
  else { currentView = 'agent'; currentAgent = agentId; renderCurrentView(); }
}
function _orchOpenTask(projId, taskId) {
  window._projectDetailTab = window._projectDetailTab || {};
  window._projectDetailTab[projId] = 'milestones';
  window._highlightTaskId = taskId;
  _orchOpenProject(projId);
}

// ---- Knowledge & Memory Hub (redesigned with RAG integration) ----
var _kmTab = 'shared';

function renderKnowledgeMemoryHub() {
  var c = document.getElementById('content');
  var tabs = [
    { id: 'shared',  label: '共享知识库',       icon: 'public' },
    { id: 'private', label: '专业领域知识库',   icon: 'school' },
    { id: 'rag',     label: 'RAG 提供方',       icon: 'cloud' },
    { id: 'memory',  label: 'Agent 私有记忆',   icon: 'psychology' },
  ];
  var tabsHtml = tabs.map(function(t) {
    var active = _kmTab === t.id;
    return '<button onclick="_kmTab=\''+t.id+'\';renderKnowledgeMemoryHub()" style="padding:8px 16px;border:none;background:'+(active?'var(--surface2)':'none')+';color:'+(active?'var(--primary)':'var(--text3)')+';font-size:12px;font-weight:'+(active?'700':'500')+';cursor:pointer;border-radius:8px;display:flex;align-items:center;gap:6px;font-family:inherit;transition:all 0.15s"><span class="material-symbols-outlined" style="font-size:16px">'+t.icon+'</span>'+t.label+'</button>';
  }).join('');

  c.innerHTML = '<div style="display:flex;gap:8px;margin-bottom:20px;padding:8px;background:var(--surface);border-radius:12px;border:1px solid var(--border-light);align-items:center;flex-wrap:wrap">'+tabsHtml+'</div>'
    + '<div id="km-content"></div>';

  if (_kmTab === 'shared') _renderKmShared();
  else if (_kmTab === 'private') _renderKmPrivate();
  else if (_kmTab === 'rag') _renderKmRagProviders();
  else if (_kmTab === 'memory') _renderKmMemory();
}

// ── Tab 1: Shared Knowledge ──
async function _renderKmShared() {
  var sc = document.getElementById('km-content');
  sc.innerHTML = '<div style="color:var(--text3);padding:20px;text-align:center">加载中...</div>';
  try {
    var data = await api('GET', '/api/portal/knowledge');
    var entries = data || [];
    if (Array.isArray(data)) entries = data;
    else if (data && data.entries) entries = data.entries;
    else entries = [];

    var cardsHtml = entries.map(function(e) {
      var tags = (e.tags||[]).map(function(t){return '<span style="padding:1px 6px;border-radius:4px;font-size:10px;background:rgba(203,201,255,0.1);color:var(--primary)">'+esc(t)+'</span>';}).join(' ');
      var preview = (e.content||'').substring(0,120).replace(/\n/g,' ');
      return '<div style="background:var(--surface);border-radius:10px;padding:14px;border:1px solid var(--border-light)">'
        + '<div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:6px">'
          + '<div style="font-weight:600;font-size:13px;color:var(--text)">'+esc(e.title||'')+'</div>'
          + '<div style="display:flex;gap:4px;flex-shrink:0">'
            + '<button onclick="_kmEditEntry(\''+esc(e.id)+'\')" class="btn btn-sm" style="padding:4px 8px;font-size:11px">编辑</button>'
            + '<button onclick="_kmDeleteEntry(\''+esc(e.id)+'\')" class="btn btn-sm" style="padding:4px 8px;font-size:11px;color:var(--error)">删除</button>'
          + '</div>'
        + '</div>'
        + '<div style="font-size:11px;color:var(--text3);margin-bottom:6px;line-height:1.5">'+esc(preview)+(preview.length>=120?'...':'')+'</div>'
        + (tags ? '<div style="display:flex;gap:4px;flex-wrap:wrap">'+tags+'</div>' : '')
        + '</div>';
    }).join('');

    sc.innerHTML = ''
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
        + '<div><div style="font-size:15px;font-weight:700;color:var(--text)">共享知识库</div>'
        + '<div style="font-size:12px;color:var(--text3);margin-top:2px">所有企业办公智能体共享的 RAG 知识，支持向量语义检索</div></div>'
        + '<div style="display:flex;gap:8px">'
          + '<button class="btn btn-sm" onclick="_kmShowImport(\'knowledge\',\'\')"><span class="material-symbols-outlined" style="font-size:14px">upload_file</span> 批量导入</button>'
          + '<button class="btn btn-primary btn-sm" onclick="_kmShowAddEntry()"><span class="material-symbols-outlined" style="font-size:14px">add</span> 新增条目</button>'
        + '</div>'
      + '</div>'
      + '<div style="display:flex;gap:10px;margin-bottom:16px">'
        + '<input id="km-search-input" placeholder="搜索知识库..." style="flex:1;font-size:13px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:8px 12px" onkeydown="if(event.key===\'Enter\')_kmSearch()">'
        + '<button class="btn btn-sm" onclick="_kmSearch()">搜索</button>'
      + '</div>'
      + (entries.length === 0 ? '<div style="color:var(--text3);padding:40px;text-align:center"><span class="material-symbols-outlined" style="font-size:40px;display:block;margin-bottom:8px">library_books</span>暂无知识条目，点击上方按钮添加</div>' : '')
      + '<div id="km-entries-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:10px">'+cardsHtml+'</div>';
  } catch(e) {
    sc.innerHTML = '<div style="color:var(--error);padding:20px">加载失败: '+e+'</div>';
  }
}

async function _kmSearch() {
  var q = (document.getElementById('km-search-input')||{}).value||'';
  if (!q.trim()) { _renderKmShared(); return; }
  try {
    var data = await api('GET', '/api/portal/knowledge/search?q='+encodeURIComponent(q));
    var grid = document.getElementById('km-entries-grid');
    if (!grid) return;
    var entries = data || [];
    if (!entries.length) { grid.innerHTML = '<div style="color:var(--text3);padding:20px;text-align:center">未找到匹配 "'+esc(q)+'" 的条目</div>'; return; }
    grid.innerHTML = entries.map(function(e) {
      return '<div style="background:var(--surface);border-radius:10px;padding:14px;border:1px solid var(--border-light)">'
        + '<div style="font-weight:600;font-size:13px;margin-bottom:4px">'+esc(e.title||'')+'</div>'
        + '<div style="font-size:11px;color:var(--text3)">'+esc((e.content||'').substring(0,120))+'</div>'
        + '</div>';
    }).join('');
  } catch(e) { console.error(e); }
}

function _kmShowAddEntry() {
  var html = '<div class="modal-overlay" id="km-add-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:560px">'
    + '<h3>新增知识条目</h3>'
    + '<div class="form-group"><label>标题 *</label><input id="km-add-title" placeholder="e.g. 公司代码规范"></div>'
    + '<div class="form-group"><label>内容 *</label><textarea id="km-add-content" rows="8" placeholder="知识内容..."></textarea></div>'
    + '<div class="form-group"><label>标签 (逗号分隔)</label><input id="km-add-tags" placeholder="e.g. 规范,编码,Python"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-add-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" onclick="_kmSaveNewEntry()">保存</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

async function _kmSaveNewEntry() {
  var title = (document.getElementById('km-add-title')||{}).value||'';
  var content = (document.getElementById('km-add-content')||{}).value||'';
  var tagsRaw = (document.getElementById('km-add-tags')||{}).value||'';
  if (!title.trim() || !content.trim()) { alert('标题和内容不能为空'); return; }
  var tags = tagsRaw.split(',').map(function(t){return t.trim()}).filter(Boolean);
  try {
    await api('POST', '/api/portal/knowledge', {title:title,content:content,tags:tags});
    var m = document.getElementById('km-add-modal'); if(m)m.remove();
    _renderKmShared();
  } catch(e) { alert('保存失败: '+e); }
}

async function _kmDeleteEntry(id) {
  if (!confirm('确定删除此知识条目？')) return;
  try {
    await api('POST', '/api/portal/knowledge/'+id+'/delete');
    _renderKmShared();
  } catch(e) { alert('删除失败: '+e); }
}

async function _kmEditEntry(id) {
  try {
    var entries = await api('GET', '/api/portal/knowledge');
    var arr = Array.isArray(entries) ? entries : (entries.entries||[]);
    var entry = arr.find(function(e){return e.id===id;});
    if (!entry) { alert('未找到条目'); return; }
    var html = '<div class="modal-overlay" id="km-edit-modal" onclick="if(event.target===this)this.remove()">'
      + '<div class="modal" style="max-width:560px">'
      + '<h3>编辑知识条目</h3>'
      + '<div class="form-group"><label>标题</label><input id="km-edit-title" value="'+esc(entry.title||'')+'"></div>'
      + '<div class="form-group"><label>内容</label><textarea id="km-edit-content" rows="8">'+esc(entry.content||'')+'</textarea></div>'
      + '<div class="form-group"><label>标签</label><input id="km-edit-tags" value="'+esc((entry.tags||[]).join(', '))+'"></div>'
      + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-edit-modal\').remove()">取消</button>'
      + '<button class="btn btn-primary" onclick="_kmSaveEditEntry(\''+esc(id)+'\')">保存</button></div>'
      + '</div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
  } catch(e) { alert('加载失败: '+e); }
}

async function _kmSaveEditEntry(id) {
  var title = (document.getElementById('km-edit-title')||{}).value;
  var content = (document.getElementById('km-edit-content')||{}).value;
  var tags = ((document.getElementById('km-edit-tags')||{}).value||'').split(',').map(function(t){return t.trim()}).filter(Boolean);
  try {
    await api('POST', '/api/portal/knowledge/'+id, {title:title,content:content,tags:tags});
    var m = document.getElementById('km-edit-modal'); if(m)m.remove();
    _renderKmShared();
  } catch(e) { alert('保存失败: '+e); }
}

// ── Batch import modal (shared between shared & private) ──
// Supports: file upload (PDF/DOCX/HTML/TXT/MD/CSV) + paste text
function _kmShowImport(collection, providerId) {
  var html = '<div class="modal-overlay" id="km-import-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:640px">'
    + '<h3>导入知识</h3>'
    + '<div style="font-size:12px;color:var(--text3);margin-bottom:14px">上传文件或粘贴文本，系统自动解析并按段落分块导入向量库。目标: <b>'+esc(collection)+'</b></div>'

    // File upload area
    + '<div id="km-imp-file-zone" style="border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer;margin-bottom:14px;transition:all 0.2s;background:var(--surface)" onclick="document.getElementById(\'km-imp-file-input\').click()" ondragover="event.preventDefault();this.style.borderColor=\'var(--primary)\';this.style.background=\'rgba(203,201,255,0.06)\'" ondragleave="this.style.borderColor=\'var(--border)\';this.style.background=\'var(--surface)\'" ondrop="event.preventDefault();this.style.borderColor=\'var(--border)\';this.style.background=\'var(--surface)\';_kmHandleFileDrop(event)">'
      + '<input type="file" id="km-imp-file-input" accept=".pdf,.docx,.doc,.html,.htm,.txt,.md,.csv,.tsv,.json,.log" style="display:none" onchange="_kmHandleFileSelect(this)">'
      + '<span class="material-symbols-outlined" style="font-size:36px;color:var(--text3);display:block;margin-bottom:8px">upload_file</span>'
      + '<div style="font-size:13px;color:var(--text2);font-weight:600">点击或拖拽文件到此处</div>'
      + '<div style="font-size:11px;color:var(--text3);margin-top:4px">支持 PDF、Word (.docx)、HTML、TXT、Markdown、CSV</div>'
    + '</div>'
    + '<div id="km-imp-file-info" style="display:none;padding:10px 14px;background:rgba(63,185,80,0.08);border:1px solid rgba(63,185,80,0.2);border-radius:8px;margin-bottom:14px;font-size:12px">'
      + '<div style="display:flex;align-items:center;gap:8px">'
        + '<span class="material-symbols-outlined" style="font-size:18px;color:#3fb950">description</span>'
        + '<span id="km-imp-file-name" style="font-weight:600;color:var(--text)"></span>'
        + '<span id="km-imp-file-size" style="color:var(--text3)"></span>'
        + '<span id="km-imp-file-method" style="color:var(--text3);font-size:10px;background:var(--surface2);padding:1px 6px;border-radius:4px"></span>'
        + '<button onclick="_kmClearFile()" style="margin-left:auto;background:none;border:none;cursor:pointer;color:var(--text3);font-size:11px">✕ 清除</button>'
      + '</div>'
      + '<div id="km-imp-file-preview" style="margin-top:6px;font-size:11px;color:var(--text3);max-height:60px;overflow:hidden"></div>'
    + '</div>'

    // Divider
    + '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><div style="flex:1;height:1px;background:var(--border)"></div><span style="font-size:11px;color:var(--text3)">或直接粘贴文本</span><div style="flex:1;height:1px;background:var(--border)"></div></div>'

    + '<div class="form-group"><label>文档标题</label><input id="km-imp-title" placeholder="e.g. 劳动法合集（上传文件时自动填入文件名）"></div>'
    + '<div class="form-group"><label>文本内容</label><textarea id="km-imp-content" rows="6" placeholder="粘贴文本内容...\n上传文件后此处自动填入解析后的文本"></textarea></div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
      + '<div class="form-group"><label>标签 (逗号分隔)</label><input id="km-imp-tags" placeholder="法律,劳动法"></div>'
      + '<div class="form-group"><label>分块大小 (字符)</label><input id="km-imp-chunk" type="number" value="1000" min="200" max="5000"></div>'
    + '</div>'
    + '<div id="km-imp-status" style="display:none;padding:8px 12px;margin-bottom:10px;border-radius:6px;font-size:12px"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-import-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" id="km-imp-submit-btn" onclick="_kmDoImport(\''+esc(collection)+'\',\''+esc(providerId)+'\')">导入</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

// Store parsed file data globally for the modal
window._kmFileData = null;

function _kmHandleFileDrop(event) {
  var files = event.dataTransfer && event.dataTransfer.files;
  if (files && files.length > 0) _kmParseFile(files[0]);
}

function _kmHandleFileSelect(input) {
  if (input.files && input.files.length > 0) _kmParseFile(input.files[0]);
}

function _kmClearFile() {
  window._kmFileData = null;
  var info = document.getElementById('km-imp-file-info');
  var zone = document.getElementById('km-imp-file-zone');
  if (info) info.style.display = 'none';
  if (zone) zone.style.display = '';
  var ta = document.getElementById('km-imp-content');
  if (ta) ta.value = '';
  var titleEl = document.getElementById('km-imp-title');
  if (titleEl) titleEl.value = '';
}

async function _kmParseFile(file) {
  var statusEl = document.getElementById('km-imp-status');
  var infoEl = document.getElementById('km-imp-file-info');
  var zoneEl = document.getElementById('km-imp-file-zone');
  var submitBtn = document.getElementById('km-imp-submit-btn');

  // Show loading state
  if (statusEl) {
    statusEl.style.display = 'block';
    statusEl.style.background = 'rgba(203,201,255,0.08)';
    statusEl.style.color = 'var(--primary)';
    statusEl.textContent = '正在解析文件: ' + file.name + ' (' + _kmFmtSize(file.size) + ')...';
  }
  if (submitBtn) submitBtn.disabled = true;

  try {
    // Read file as base64
    var b64 = await new Promise(function(resolve, reject) {
      var reader = new FileReader();
      reader.onload = function() {
        var result = reader.result;
        resolve(result.split(',')[1]); // strip data:...;base64, prefix
      };
      reader.onerror = function() { reject(new Error('文件读取失败')); };
      reader.readAsDataURL(file);
    });

    // Send to server for parsing
    var res = await api('POST', '/api/portal/rag/parse-file', {
      file_data: b64,
      file_name: file.name
    });

    if (res.error) throw new Error(res.error);

    window._kmFileData = { name: file.name, size: file.size, text: res.text, method: res.method };

    // Fill in the form
    var titleEl = document.getElementById('km-imp-title');
    if (titleEl && !titleEl.value) {
      titleEl.value = file.name.replace(/\.[^.]+$/, '');
    }
    var contentEl = document.getElementById('km-imp-content');
    if (contentEl) contentEl.value = res.text;

    // Show file info
    if (infoEl) {
      infoEl.style.display = 'block';
      var nameEl = document.getElementById('km-imp-file-name');
      var sizeEl = document.getElementById('km-imp-file-size');
      var methodEl = document.getElementById('km-imp-file-method');
      var previewEl = document.getElementById('km-imp-file-preview');
      if (nameEl) nameEl.textContent = file.name;
      if (sizeEl) sizeEl.textContent = _kmFmtSize(file.size) + ' → ' + res.length + ' 字符';
      if (methodEl) methodEl.textContent = res.method;
      if (previewEl) previewEl.textContent = (res.text || '').substring(0, 200) + (res.length > 200 ? '...' : '');
    }
    if (zoneEl) zoneEl.style.display = 'none';

    if (statusEl) {
      statusEl.style.background = 'rgba(63,185,80,0.08)';
      statusEl.style.color = '#3fb950';
      statusEl.textContent = '解析完成: ' + res.length + ' 字符，使用 ' + res.method + ' 解析器';
      setTimeout(function(){ if(statusEl) statusEl.style.display='none'; }, 3000);
    }
  } catch(e) {
    if (statusEl) {
      statusEl.style.background = 'rgba(248,81,73,0.08)';
      statusEl.style.color = 'var(--error)';
      statusEl.textContent = '解析失败: ' + e;
    }
    window._kmFileData = null;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function _kmFmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

async function _kmDoImport(collection, providerId) {
  var title = (document.getElementById('km-imp-title')||{}).value||'Imported';
  var content = (document.getElementById('km-imp-content')||{}).value||'';
  var tags = ((document.getElementById('km-imp-tags')||{}).value||'').split(',').map(function(t){return t.trim()}).filter(Boolean);
  var chunk = parseInt((document.getElementById('km-imp-chunk')||{}).value)||1000;
  if (!content.trim()) { alert('内容不能为空，请上传文件或粘贴文本'); return; }
  var statusEl = document.getElementById('km-imp-status');
  var submitBtn = document.getElementById('km-imp-submit-btn');
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = '导入中...'; }
  if (statusEl) { statusEl.style.display = 'block'; statusEl.style.background = 'rgba(203,201,255,0.08)'; statusEl.style.color = 'var(--primary)'; statusEl.textContent = '正在导入并分块...'; }
  try {
    var res = await api('POST', '/api/portal/rag/import', {
      collection:collection, provider_id:providerId,
      title:title, content:content, tags:tags, chunk_size:chunk
    });
    window._kmFileData = null;
    alert('导入完成: '+((res&&res.chunks)||0)+' 个分块');
    var m = document.getElementById('km-import-modal'); if(m)m.remove();
    if (_kmTab === 'shared') _renderKmShared();
    else if (_kmTab === 'private') _renderKmPrivate();
  } catch(e) {
    alert('导入失败: '+e);
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = '导入'; }
    if (statusEl) { statusEl.style.background = 'rgba(248,81,73,0.08)'; statusEl.style.color = 'var(--error)'; statusEl.textContent = '导入失败: '+e; }
  }
}

// ── Tab 2: Domain Knowledge Bases (standalone, decoupled from agents) ──
async function _renderKmPrivate() {
  var sc = document.getElementById('km-content');
  sc.innerHTML = '<div style="color:var(--text3);padding:20px;text-align:center">加载中...</div>';
  try {
    var data = await api('POST', '/api/portal/domain-kb/list');
    var kbs = (data && data.knowledge_bases) || [];

    // Find which agents use each KB
    var agentsByKb = {};
    (agents||[]).forEach(function(a) {
      var colIds = (a.profile && a.profile.rag_collection_ids) || [];
      colIds.forEach(function(cid) { if (!agentsByKb[cid]) agentsByKb[cid] = []; agentsByKb[cid].push(a); });
    });

    var cardsHtml = kbs.map(function(kb) {
      var boundAgents = agentsByKb[kb.id] || [];
      var agentNames = boundAgents.map(function(a){ return esc(a.name); }).join(', ');
      var tagHtml = (kb.tags||[]).map(function(t){return '<span style="padding:1px 6px;border-radius:4px;font-size:10px;background:rgba(240,136,62,0.1);color:#f0883e">'+esc(t)+'</span>';}).join(' ');
      return '<div style="background:var(--surface);border-radius:12px;padding:18px;border:1px solid var(--border-light)">'
        + '<div style="display:flex;align-items:start;gap:12px;margin-bottom:10px">'
          + '<div style="width:42px;height:42px;border-radius:12px;background:rgba(240,136,62,0.12);display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="material-symbols-outlined" style="font-size:22px;color:#f0883e">menu_book</span></div>'
          + '<div style="flex:1;min-width:0">'
            + '<div style="font-weight:700;font-size:14px;color:var(--text)">'+esc(kb.name)+'</div>'
            + '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+esc(kb.description||'')+'</div>'
          + '</div>'
          + '<div style="display:flex;gap:4px;flex-shrink:0">'
            + '<button onclick="_kmEditDomainKb(\''+esc(kb.id)+'\')" class="btn btn-sm" style="padding:4px 8px;font-size:11px" title="编辑"><span class="material-symbols-outlined" style="font-size:14px">edit</span></button>'
            + '<button onclick="_kmDeleteDomainKb(\''+esc(kb.id)+'\',\''+esc(kb.name)+'\')" class="btn btn-sm" style="padding:4px 8px;font-size:11px;color:var(--error)" title="删除"><span class="material-symbols-outlined" style="font-size:14px">delete</span></button>'
          + '</div>'
        + '</div>'
        + '<div style="display:flex;gap:12px;font-size:11px;color:var(--text3);margin-bottom:10px">'
          + '<span style="display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">description</span>'+kb.doc_count+' 文档块</span>'
          + '<span style="display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">smart_toy</span>'+(boundAgents.length > 0 ? boundAgents.length+' 个 Agent 使用' : '未绑定 Agent')+'</span>'
          + (kb.provider_id ? '<span style="display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">cloud</span>远程</span>' : '<span style="display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">storage</span>本地</span>')
        + '</div>'
        + (agentNames ? '<div style="font-size:11px;color:var(--text3);margin-bottom:8px">绑定: '+agentNames+'</div>' : '')
        + (tagHtml ? '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px">'+tagHtml+'</div>' : '')
        + '<div style="display:flex;gap:8px">'
          + '<button class="btn btn-sm" onclick="_kmShowDomainImport(\''+esc(kb.id)+'\',\''+esc(kb.name)+'\')"><span class="material-symbols-outlined" style="font-size:14px">upload_file</span> 导入知识</button>'
          + '<button class="btn btn-sm" onclick="_kmSearchDomainKb(\''+esc(kb.id)+'\',\''+esc(kb.name)+'\')"><span class="material-symbols-outlined" style="font-size:14px">search</span> 检索测试</button>'
        + '</div>'
        + '</div>';
    }).join('');

    sc.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
      + '<div><div style="font-size:15px;font-weight:700;color:var(--text)">专业领域知识库</div>'
      + '<div style="font-size:12px;color:var(--text3);margin-top:2px">独立于智能体，可创建后绑定给多个顾问。删除智能体不影响知识库。</div></div>'
      + '<button class="btn btn-primary btn-sm" onclick="_kmShowCreateDomainKb()"><span class="material-symbols-outlined" style="font-size:14px">add</span> 新建知识库</button>'
      + '</div>'
      + (kbs.length === 0
        ? '<div style="text-align:center;padding:60px 20px;border:1px dashed var(--border);border-radius:12px">'
          + '<span class="material-symbols-outlined" style="font-size:48px;color:var(--text3);display:block;margin-bottom:12px">menu_book</span>'
          + '<div style="font-size:14px;color:var(--text2);margin-bottom:8px">暂无专业领域知识库</div>'
          + '<div style="font-size:12px;color:var(--text3);margin-bottom:16px">创建知识库后，可导入 PDF/Word/文本，然后在创建顾问智能体时选择绑定</div>'
          + '<button class="btn btn-primary btn-sm" onclick="_kmShowCreateDomainKb()"><span class="material-symbols-outlined" style="font-size:14px">add</span> 新建专业领域知识库</button>'
          + '</div>'
        : '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:12px">'+cardsHtml+'</div>');
  } catch(e) {
    sc.innerHTML = '<div style="color:var(--error);padding:20px">加载失败: '+e+'</div>';
  }
}

function _kmShowCreateDomainKb() {
  var html = '<div class="modal-overlay" id="km-dkb-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:500px">'
    + '<h3>新建专业领域知识库</h3>'
    + '<div class="form-group"><label>知识库名称 *</label><input id="km-dkb-name" placeholder="e.g. 法律知识库、财务知识库"></div>'
    + '<div class="form-group"><label>描述</label><input id="km-dkb-desc" placeholder="e.g. 包含劳动法、合同法等法律文档"></div>'
    + '<div class="form-group"><label>标签 (逗号分隔)</label><input id="km-dkb-tags" placeholder="法律,劳动法,合同法"></div>'
    + '<div class="form-group"><label>存储提供方</label><select id="km-dkb-provider"><option value="">本地 ChromaDB (默认)</option></select></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-dkb-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" onclick="_kmSaveDomainKb()">创建</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
  // Load remote providers into dropdown
  api('GET', '/api/portal/rag/providers').then(function(data) {
    var sel = document.getElementById('km-dkb-provider');
    if (!sel || !data || !data.providers) return;
    data.providers.forEach(function(p) {
      if (!p.enabled) return;
      var opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + ' (' + p.kind + ')';
      sel.appendChild(opt);
    });
  }).catch(function(){});
}

async function _kmSaveDomainKb() {
  var name = (document.getElementById('km-dkb-name')||{}).value||'';
  var desc = (document.getElementById('km-dkb-desc')||{}).value||'';
  var tags = ((document.getElementById('km-dkb-tags')||{}).value||'').split(',').map(function(t){return t.trim()}).filter(Boolean);
  var provider = (document.getElementById('km-dkb-provider')||{}).value||'';
  if (!name.trim()) { alert('名称不能为空'); return; }
  try {
    await api('POST', '/api/portal/domain-kb/create', {name:name, description:desc, tags:tags, provider_id:provider});
    var m = document.getElementById('km-dkb-modal'); if(m) m.remove();
    _renderKmPrivate();
  } catch(e) { alert('创建失败: '+e); }
}

function _kmEditDomainKb(kbId) {
  api('POST', '/api/portal/domain-kb/list').then(function(data) {
    var kbs = (data && data.knowledge_bases) || [];
    var kb = kbs.find(function(k){ return k.id === kbId; });
    if (!kb) { alert('未找到'); return; }
    var html = '<div class="modal-overlay" id="km-dkb-edit-modal" onclick="if(event.target===this)this.remove()">'
      + '<div class="modal" style="max-width:500px">'
      + '<h3>编辑知识库</h3>'
      + '<div class="form-group"><label>名称</label><input id="km-dkb-edit-name" value="'+esc(kb.name)+'"></div>'
      + '<div class="form-group"><label>描述</label><input id="km-dkb-edit-desc" value="'+esc(kb.description||'')+'"></div>'
      + '<div class="form-group"><label>标签</label><input id="km-dkb-edit-tags" value="'+esc((kb.tags||[]).join(', '))+'"></div>'
      + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-dkb-edit-modal\').remove()">取消</button>'
      + '<button class="btn btn-primary" onclick="_kmSaveEditDomainKb(\''+esc(kbId)+'\')">保存</button></div>'
      + '</div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
  }).catch(function(e){ alert('加载失败: '+e); });
}

async function _kmSaveEditDomainKb(kbId) {
  var name = (document.getElementById('km-dkb-edit-name')||{}).value;
  var desc = (document.getElementById('km-dkb-edit-desc')||{}).value;
  var tags = ((document.getElementById('km-dkb-edit-tags')||{}).value||'').split(',').map(function(t){return t.trim()}).filter(Boolean);
  try {
    await api('POST', '/api/portal/domain-kb/update', {id:kbId, name:name, description:desc, tags:tags});
    var m = document.getElementById('km-dkb-edit-modal'); if(m) m.remove();
    _renderKmPrivate();
  } catch(e) { alert('保存失败: '+e); }
}

async function _kmDeleteDomainKb(kbId, name) {
  if (!confirm('确定删除知识库 "'+name+'" 吗？\\n\\n注意：删除后知识库中的所有文档将被永久移除！')) return;
  try {
    await api('POST', '/api/portal/domain-kb/delete', {id:kbId});
    _renderKmPrivate();
  } catch(e) { alert('删除失败: '+e); }
}

function _kmShowDomainImport(kbId, kbName) {
  // Reuse the file upload import modal but target domain KB API
  var html = '<div class="modal-overlay" id="km-import-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:640px">'
    + '<h3>导入知识到: '+esc(kbName)+'</h3>'
    + '<div style="font-size:12px;color:var(--text3);margin-bottom:14px">上传文件或粘贴文本，系统自动解析并分块导入。</div>'
    // File upload area
    + '<div id="km-imp-file-zone" style="border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer;margin-bottom:14px;transition:all 0.2s;background:var(--surface)" onclick="document.getElementById(\'km-imp-file-input\').click()" ondragover="event.preventDefault();this.style.borderColor=\'var(--primary)\';this.style.background=\'rgba(203,201,255,0.06)\'" ondragleave="this.style.borderColor=\'var(--border)\';this.style.background=\'var(--surface)\'" ondrop="event.preventDefault();this.style.borderColor=\'var(--border)\';this.style.background=\'var(--surface)\';_kmHandleFileDrop(event)">'
      + '<input type="file" id="km-imp-file-input" accept=".pdf,.docx,.doc,.html,.htm,.txt,.md,.csv,.tsv,.json,.log" style="display:none" onchange="_kmHandleFileSelect(this)">'
      + '<span class="material-symbols-outlined" style="font-size:36px;color:var(--text3);display:block;margin-bottom:8px">upload_file</span>'
      + '<div style="font-size:13px;color:var(--text2);font-weight:600">点击或拖拽文件到此处</div>'
      + '<div style="font-size:11px;color:var(--text3);margin-top:4px">支持 PDF、Word (.docx)、HTML、TXT、Markdown、CSV</div>'
    + '</div>'
    + '<div id="km-imp-file-info" style="display:none;padding:10px 14px;background:rgba(63,185,80,0.08);border:1px solid rgba(63,185,80,0.2);border-radius:8px;margin-bottom:14px;font-size:12px">'
      + '<div style="display:flex;align-items:center;gap:8px"><span class="material-symbols-outlined" style="font-size:18px;color:#3fb950">description</span>'
        + '<span id="km-imp-file-name" style="font-weight:600;color:var(--text)"></span>'
        + '<span id="km-imp-file-size" style="color:var(--text3)"></span>'
        + '<span id="km-imp-file-method" style="color:var(--text3);font-size:10px;background:var(--surface2);padding:1px 6px;border-radius:4px"></span>'
        + '<button onclick="_kmClearFile()" style="margin-left:auto;background:none;border:none;cursor:pointer;color:var(--text3);font-size:11px">✕</button>'
      + '</div>'
      + '<div id="km-imp-file-preview" style="margin-top:6px;font-size:11px;color:var(--text3);max-height:60px;overflow:hidden"></div>'
    + '</div>'
    + '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><div style="flex:1;height:1px;background:var(--border)"></div><span style="font-size:11px;color:var(--text3)">或直接粘贴文本</span><div style="flex:1;height:1px;background:var(--border)"></div></div>'
    + '<div class="form-group"><label>文档标题</label><input id="km-imp-title" placeholder="e.g. 劳动法合集"></div>'
    + '<div class="form-group"><label>文本内容</label><textarea id="km-imp-content" rows="6" placeholder="上传文件后此处自动填入"></textarea></div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
      + '<div class="form-group"><label>标签</label><input id="km-imp-tags" placeholder="法律,劳动法"></div>'
      + '<div class="form-group"><label>分块大小</label><input id="km-imp-chunk" type="number" value="1000" min="200" max="5000"></div>'
    + '</div>'
    + '<div id="km-imp-status" style="display:none;padding:8px 12px;margin-bottom:10px;border-radius:6px;font-size:12px"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-import-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" id="km-imp-submit-btn" onclick="_kmDoDomainImport(\''+esc(kbId)+'\')">导入</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

async function _kmDoDomainImport(kbId) {
  var title = (document.getElementById('km-imp-title')||{}).value||'Imported';
  var content = (document.getElementById('km-imp-content')||{}).value||'';
  var tags = ((document.getElementById('km-imp-tags')||{}).value||'').split(',').map(function(t){return t.trim()}).filter(Boolean);
  var chunk = parseInt((document.getElementById('km-imp-chunk')||{}).value)||1000;
  if (!content.trim()) { alert('内容不能为空'); return; }
  var submitBtn = document.getElementById('km-imp-submit-btn');
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = '导入中...'; }
  try {
    var res = await api('POST', '/api/portal/domain-kb/import', {
      kb_id:kbId, title:title, content:content, tags:tags, chunk_size:chunk
    });
    window._kmFileData = null;
    alert('导入完成: '+((res&&res.chunks)||0)+' 个分块');
    var m = document.getElementById('km-import-modal'); if(m)m.remove();
    _renderKmPrivate();
  } catch(e) {
    alert('导入失败: '+e);
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = '导入'; }
  }
}

function _kmSearchDomainKb(kbId, kbName) {
  var q = prompt('在 "'+kbName+'" 中检索:');
  if (!q) return;
  api('POST', '/api/portal/domain-kb/search', {kb_id:kbId, query:q, top_k:5})
    .then(function(data) {
      var results = (data&&data.results)||[];
      if (!results.length) { alert('未找到匹配结果'); return; }
      var msg = results.map(function(r,i){return (i+1)+'. '+(r.title||'untitled')+' (distance: '+(r.distance||0).toFixed(3)+')\n  '+((r.content||'').substring(0,100));}).join('\n\n');
      alert('检索结果 ('+results.length+' 条):\n\n'+msg);
    }).catch(function(e){ alert('检索失败: '+e); });
}

// ── Tab 3: RAG Providers ──
async function _renderKmRagProviders() {
  var sc = document.getElementById('km-content');
  sc.innerHTML = '<div style="color:var(--text3);padding:20px;text-align:center">加载中...</div>';
  try {
    var data = await api('GET', '/api/portal/rag/providers');
    var providers = (data&&data.providers)||[];

    var listHtml = '<div style="background:var(--surface);border-radius:10px;padding:14px;border:1px solid var(--border-light);margin-bottom:10px">'
      + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
        + '<span class="material-symbols-outlined" style="font-size:20px;color:#3fb950">storage</span>'
        + '<div><div style="font-weight:600;font-size:13px">本地 ChromaDB (内置)</div>'
        + '<div style="font-size:11px;color:var(--text3)">默认向量数据库，零配置，数据存储在 ~/.tudou_claw/chromadb</div></div>'
      + '</div></div>';

    listHtml += providers.map(function(p) {
      return '<div style="background:var(--surface);border-radius:10px;padding:14px;border:1px solid var(--border-light);margin-bottom:10px">'
        + '<div style="display:flex;justify-content:space-between;align-items:start">'
          + '<div style="display:flex;align-items:center;gap:10px">'
            + '<span class="material-symbols-outlined" style="font-size:20px;color:var(--primary)">cloud</span>'
            + '<div><div style="font-weight:600;font-size:13px">'+esc(p.name)+'</div>'
            + '<div style="font-size:11px;color:var(--text3)">'+esc(p.kind)+' · '+esc(p.base_url||'N/A')+(p.enabled?'':' · <span style="color:var(--error)">已禁用</span>')+'</div></div>'
          + '</div>'
          + '<button onclick="_kmDeleteProvider(\''+esc(p.id)+'\')" class="btn btn-sm" style="padding:4px 8px;font-size:11px;color:var(--error)">移除</button>'
        + '</div></div>';
    }).join('');

    sc.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
      + '<div><div style="font-size:15px;font-weight:700;color:var(--text)">RAG 提供方管理</div>'
      + '<div style="font-size:12px;color:var(--text3);margin-top:2px">管理向量数据库后端，支持本地 ChromaDB 和远程节点 HTTP 接口</div></div>'
      + '<button class="btn btn-primary btn-sm" onclick="_kmShowAddProvider()"><span class="material-symbols-outlined" style="font-size:14px">add</span> 添加提供方</button>'
      + '</div>'
      + listHtml;
  } catch(e) {
    sc.innerHTML = '<div style="color:var(--error);padding:20px">加载失败: '+e+'</div>';
  }
}

function _kmShowAddProvider() {
  var html = '<div class="modal-overlay" id="km-prov-modal" onclick="if(event.target===this)this.remove()">'
    + '<div class="modal" style="max-width:480px">'
    + '<h3>添加 RAG 提供方</h3>'
    + '<div class="form-group"><label>名称 *</label><input id="km-prov-name" placeholder="e.g. 远程 Node-2 RAG"></div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">'
      + '<div class="form-group"><label>类型</label><select id="km-prov-kind"><option value="remote">远程 HTTP</option><option value="local">本地</option></select></div>'
      + '<div class="form-group"><label>API Key (可选)</label><input id="km-prov-key" placeholder="Bearer token"></div>'
    + '</div>'
    + '<div class="form-group"><label>Base URL *</label><input id="km-prov-url" placeholder="http://192.168.1.100:8765"></div>'
    + '<div class="form-group"><label>Node Secret (可选，TudouClaw 节点间认证)</label><input id="km-prov-secret" placeholder="X-Claw-Secret"></div>'
    + '<div class="form-actions"><button class="btn btn-ghost" onclick="document.getElementById(\'km-prov-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary" onclick="_kmSaveProvider()">保存</button></div>'
    + '</div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

async function _kmSaveProvider() {
  var name = (document.getElementById('km-prov-name')||{}).value||'';
  var kind = (document.getElementById('km-prov-kind')||{}).value||'remote';
  var url = (document.getElementById('km-prov-url')||{}).value||'';
  var key = (document.getElementById('km-prov-key')||{}).value||'';
  var secret = (document.getElementById('km-prov-secret')||{}).value||'';
  if (!name.trim()) { alert('名称不能为空'); return; }
  try {
    await api('POST', '/api/portal/rag/providers', {
      name:name, kind:kind, base_url:url, api_key:key,
      config: secret ? {node_secret:secret} : {}
    });
    var m = document.getElementById('km-prov-modal'); if(m)m.remove();
    _renderKmRagProviders();
  } catch(e) { alert('保存失败: '+e); }
}

async function _kmDeleteProvider(id) {
  if (!confirm('确定移除此 RAG 提供方？')) return;
  try {
    await api('POST', '/api/portal/rag/providers/'+id+'/delete');
    _renderKmRagProviders();
  } catch(e) { alert('删除失败: '+e); }
}

// ── Tab 4: Agent Private Memory ──
var _agentMemStats = {};  // {agent_id: {l1, l2, l3}}

function _renderKmMemory() {
  var sc = document.getElementById('km-content');
  var visibleAgents = (agents||[]).filter(function(a){return !a.parent_id;});
  if (!visibleAgents.length) {
    sc.innerHTML = '<div style="color:var(--text3);padding:40px;text-align:center">暂无智能体</div>';
    return;
  }
  sc.innerHTML = '<div style="margin-bottom:16px"><div style="font-size:15px;font-weight:700;color:var(--text)">Agent 私有记忆</div>'
    + '<div style="font-size:12px;color:var(--text3);margin-top:2px">点击任一 agent 查看其记忆层级（L1/L2/L3）、ExecutionPlan、Transcript</div></div>'
    + '<div id="km-agent-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px"></div>';
  _renderKmAgentCards(visibleAgents);
  api('GET', '/api/portal/agents/memory-stats').then(function(data) {
    if (data) { _agentMemStats = data; _renderKmAgentCards(visibleAgents); }
  }).catch(function(){});
}

function _renderKmAgentCards(visibleAgents) {
  var grid = document.getElementById('km-agent-grid');
  if (!grid) return;
  grid.innerHTML = visibleAgents.map(function(a) {
    var cls = a.agent_class || (a.profile&&a.profile.agent_class) || 'enterprise';
    var clsMeta = _AGENT_CLASSES[cls] || _AGENT_CLASSES.enterprise;
    var memMode = a.memory_mode || (a.profile&&a.profile.memory_mode) || 'full';
    var memLabel = memMode === 'full' ? '完整记忆' : memMode === 'light' ? '轻量记忆' : '无记忆';
    var st = _agentMemStats[a.id] || {};
    var l1 = st.l1 || 0, l2 = st.l2 || 0, l3 = st.l3 || 0;
    var total = l1 + l2 + l3;
    return '<div onclick="showAgentMemoryView(\''+a.id+'\')" style="background:var(--surface);border-radius:10px;padding:14px;cursor:pointer;border:1px solid var(--border-light);transition:all 0.15s" onmouseenter="this.style.borderColor=\'var(--primary)\'" onmouseleave="this.style.borderColor=\'var(--border-light)\'">'
      + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'
        + '<span class="material-symbols-outlined" style="font-size:20px;color:'+clsMeta.color+'">'+clsMeta.icon+'</span>'
        + '<div style="font-weight:600;font-size:13px;flex:1">'+esc(a.name)+'</div>'
        + '<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--surface2, #333);color:var(--text3)">'+memLabel+'</span>'
      + '</div>'
      + '<div style="display:flex;gap:6px;margin-bottom:6px">'
        + _memBadge('L1 短时', l1, '#3b82f6')
        + _memBadge('L2 工作', l2, '#8b5cf6')
        + _memBadge('L3 长期', l3, '#10b981')
      + '</div>'
      + '<div style="display:flex;gap:2px;height:3px;border-radius:2px;overflow:hidden;background:var(--surface2, #222)">'
        + (l1 ? '<div style="flex:'+l1+';background:#3b82f6"></div>' : '')
        + (l2 ? '<div style="flex:'+l2+';background:#8b5cf6"></div>' : '')
        + (l3 ? '<div style="flex:'+l3+';background:#10b981"></div>' : '')
        + (total === 0 ? '<div style="flex:1;background:var(--border-light)"></div>' : '')
      + '</div>'
      + '<div style="font-size:10px;color:var(--text3);margin-top:4px">共 '+total+' 条记忆</div>'
    + '</div>';
  }).join('');
}

function _memBadge(label, count, color) {
  var bg = count > 0 ? color + '20' : 'transparent';
  var fg = count > 0 ? color : 'var(--text3)';
  return '<div style="font-size:10px;padding:2px 6px;border-radius:4px;background:'+bg+';color:'+fg+';border:1px solid '+(count>0?color+'40':'var(--border-light)')+'">'
    + label + ' <b>' + count + '</b></div>';
}

function showAgentMemoryView(aid) {
  var ag = null;
  if (typeof agents !== 'undefined' && agents) {
    for (var i=0;i<agents.length;i++) { if (agents[i].id === aid) { ag = agents[i]; break; } }
  }
  var name = (ag && ag.name) || aid;

  var modal = document.createElement('div');
  modal.id = 'agent-mem-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = ''
    + '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;width:820px;max-width:94vw;max-height:86vh;display:flex;flex-direction:column">'
    + '  <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">'
    + '    <div><div style="font-weight:700;font-size:15px">🧠 '+esc(name)+' — 记忆视图</div>'
    + '    <div style="font-size:11px;color:var(--text3);margin-top:2px">L1 短时 / L2 工作 / L3 长期 · ExecutionPlan · Transcript</div></div>'
    + '    <div style="display:flex;gap:8px">'
    + '      <button class="btn btn-sm" onclick="compactAgentMemoryFromModal(\''+esc(aid)+'\')">压缩记忆</button>'
    + '      <button class="btn btn-sm" onclick="document.getElementById(\'agent-mem-modal\').remove()">×</button>'
    + '    </div>'
    + '  </div>'
    + '  <div id="agent-mem-body" style="flex:1;overflow:auto;padding:16px 18px;font-size:12px;color:var(--text2)">加载中…</div>'
    + '</div>';
  document.body.appendChild(modal);

  Promise.all([
    api('GET', '/api/portal/agent/'+encodeURIComponent(aid)+'/engine').catch(function(){return {};}),
    api('GET', '/api/portal/agent/'+encodeURIComponent(aid)+'/plans').catch(function(){return {};}),
    api('GET', '/api/portal/agent/'+encodeURIComponent(aid)+'/transcript').catch(function(){return {};}),
    api('GET', '/api/portal/agent/'+encodeURIComponent(aid)+'/memory-stats').catch(function(){return {};}),
  ]).then(function(res){
    var eng = res[0] || {};
    var plans = res[1] || {};
    var tr = res[2] || {};
    var mem = res[3] || {};
    var body = document.getElementById('agent-mem-body');
    if (!body) return;

    var es = eng.engine_summary || {};
    var sections = [];

    // ── Memory overview card with L1/L2/L3 ──
    var l1 = mem.l1 || 0, l2 = mem.l2 || 0, l3 = mem.l3 || 0;
    var l3cat = mem.l3_by_category || {};
    sections.push(
      '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
      + '<div style="font-weight:600;margin-bottom:10px">记忆层级概览</div>'
      + '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px">'
      + _memStatCard('L1 短时记忆', l1, '#3b82f6', '当前对话窗口中的消息')
      + _memStatCard('L2 工作记忆', l2, '#8b5cf6', '历史对话压缩摘要')
      + _memStatCard('L3 长期记忆', l3, '#10b981', '结构化语义知识')
      + '</div>'
      + (l3 > 0 ? '<div style="display:flex;gap:6px;flex-wrap:wrap">'
        + _catBadge('intent', '意图', l3cat.intent||0)
        + _catBadge('reasoning', '推理', l3cat.reasoning||0)
        + _catBadge('outcome', '结果', l3cat.outcome||0)
        + _catBadge('rule', '规则', l3cat.rule||0)
        + _catBadge('reflection', '反思', l3cat.reflection||0)
        + '</div>' : '')
      + '<div style="margin-top:10px;padding:8px 10px;background:var(--surface2, #222);border-radius:6px;font-size:11px;color:var(--text3)">'
      + '<b>压缩策略：</b>L1（最近对话）超出窗口后 → 自动压缩为 L2 摘要（渐进式 Level 0→1→2，信息保留递减）→ 对话中提取结构化事实写入 L3（分 5 类：意图/推理/结果/规则/反思）'
      + '</div></div>'
    );

    // ── L2 Episodic entries ──
    var l2entries = mem.l2_entries || [];
    if (l2entries.length) {
      var l2rows = l2entries.map(function(ep){
        var lvl = ep.compression_level || 0;
        var lvlLabel = lvl === 0 ? '详细' : lvl === 1 ? '中等' : '精简';
        var lvlColor = lvl === 0 ? '#3b82f6' : lvl === 1 ? '#f59e0b' : '#ef4444';
        return '<div style="padding:8px;border-bottom:1px solid var(--border-light)">'
          + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
            + '<span style="font-size:10px;padding:1px 5px;border-radius:3px;background:'+lvlColor+'20;color:'+lvlColor+'">Level '+lvl+' '+lvlLabel+'</span>'
            + '<span style="font-size:10px;color:var(--text3)">Turn '+esc(String(ep.turn_start||'?'))+'-'+esc(String(ep.turn_end||'?'))+'</span>'
            + '<span style="font-size:10px;color:var(--text3)">'+esc(String(ep.message_count||0))+' msgs</span>'
            + '<span style="font-size:10px;color:var(--text3);margin-left:auto">'+esc(String(ep.created_at||'').slice(0,19))+'</span>'
          + '</div>'
          + '<div style="font-size:11px;color:var(--text2);white-space:pre-wrap;max-height:80px;overflow:auto">'+esc(ep.summary||'')+'</div>'
          + (ep.keywords ? '<div style="margin-top:4px;font-size:10px;color:var(--text3)">关键词: '+esc(ep.keywords)+'</div>' : '')
          + '</div>';
      }).join('');
      sections.push(
        '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
        + '<div style="font-weight:600;margin-bottom:8px;color:#8b5cf6">L2 工作记忆 — 对话摘要 ('+l2+')</div>'
        + l2rows + '</div>'
      );
    }

    // ── L3 Semantic facts ──
    var l3entries = mem.l3_entries || [];
    if (l3entries.length) {
      var CAT_LABELS = {intent:'意图',reasoning:'推理',outcome:'结果',rule:'规则',reflection:'反思'};
      var CAT_COLORS = {intent:'#3b82f6',reasoning:'#f59e0b',outcome:'#10b981',rule:'#ef4444',reflection:'#8b5cf6'};
      var l3rows = l3entries.map(function(f){
        var cat = f.category || 'unknown';
        var catLabel = CAT_LABELS[cat] || cat;
        var catColor = CAT_COLORS[cat] || 'var(--text3)';
        var conf = f.confidence ? Math.round(f.confidence * 100) : 0;
        return '<div style="padding:8px;border-bottom:1px solid var(--border-light)">'
          + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">'
            + '<span style="font-size:10px;padding:1px 5px;border-radius:3px;background:'+catColor+'20;color:'+catColor+'">'+esc(catLabel)+'</span>'
            + '<span style="font-size:10px;color:var(--text3)">置信度 '+conf+'%</span>'
            + '<span style="font-size:10px;color:var(--text3);margin-left:auto">'+esc(String(f.created_at||'').slice(0,19))+'</span>'
          + '</div>'
          + '<div style="font-size:11px;color:var(--text2)">'+esc(f.content||'')+'</div>'
          + '</div>';
      }).join('');
      sections.push(
        '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
        + '<div style="font-weight:600;margin-bottom:8px;color:#10b981">L3 长期记忆 — 语义知识 ('+l3+')</div>'
        + l3rows + '</div>'
      );
    }

    // ── Engine overview ──
    sections.push(
      '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
      + '<div style="font-weight:600;margin-bottom:8px">引擎概览</div>'
      + '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">'
      + '<div><div style="color:var(--text3);font-size:10px">TURN</div><div style="font-size:18px;font-weight:600">'+esc(String(eng.turn_count||0))+'</div></div>'
      + '<div><div style="color:var(--text3);font-size:10px">TRANSCRIPT</div><div style="font-size:18px;font-weight:600">'+esc(String(eng.transcript_size||0))+'</div></div>'
      + '<div><div style="color:var(--text3);font-size:10px">MESSAGES (L1)</div><div style="font-size:18px;font-weight:600">'+esc(String(l1))+'</div></div>'
      + '<div><div style="color:var(--text3);font-size:10px">TOKENS</div><div style="font-size:18px;font-weight:600">'+esc(String(es.total_tokens || 0))+'</div></div>'
      + '</div></div>'
    );

    // Current execution plan
    if (plans.current_plan) {
      var cp = plans.current_plan;
      var steps = (cp.steps || []).map(function(s){
        var icon = s.status === 'completed' ? '✅' : (s.status === 'in_progress' ? '🔄' : (s.status === 'failed' ? '❌' : '⭕'));
        return '<div style="padding:4px 0">'+icon+' '+esc(s.title||'')+' <span style="color:var(--text3);font-size:10px">['+esc(s.status||'')+']</span></div>';
      }).join('');
      sections.push(
        '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
        + '<div style="font-weight:600;margin-bottom:8px">当前执行计划</div>'
        + '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">'+esc(cp.task_summary||'')+'</div>'
        + (steps || '<div style="color:var(--text3)">无步骤</div>')
        + '</div>'
      );
    }

    // Recent transcript entries — filter infra noise
    var trEntriesRaw = tr.transcript || eng.transcript_preview || [];
    if (trEntriesRaw && trEntriesRaw.length) {
      var NOISE_PATTERNS = [
        /LLM provider.*(connection failed|timeout|unreachable)/i,
        /429\s*(Client Error|Too Many Requests)/i,
        /\bToo Many Requests\b/i,
        /ConnectionError|ReadTimeout|ReadTimeoutError/i,
        /ECONNREFUSED|ECONNRESET|ETIMEDOUT/i,
        /\[Delegated task from [0-9a-f]+\]\s+Error:.*(429|connection failed|timeout|Too Many Requests)/i,
      ];
      function isNoise(s){
        for (var i=0;i<NOISE_PATTERNS.length;i++){
          if (NOISE_PATTERNS[i].test(s)) return true;
        }
        return false;
      }
      var noiseFiltered = 0;
      var cleaned = [];
      for (var i=0;i<trEntriesRaw.length;i++){
        var t = trEntriesRaw[i];
        var s = (typeof t === 'string') ? t : JSON.stringify(t);
        if (isNoise(s)) { noiseFiltered++; continue; }
        var key = s.slice(0,200);
        var last = cleaned.length ? cleaned[cleaned.length-1] : null;
        if (last && last.key === key) {
          last.count += 1;
        } else {
          cleaned.push({ text: s, key: key, count: 1 });
        }
      }
      var tail = cleaned.slice(-20);
      var rows = tail.map(function(row){
        var disp = row.text.slice(0,400) + (row.text.length>400?'…':'');
        var badge = row.count > 1 ? ' <span style="color:var(--text3);font-size:10px">×'+row.count+'</span>' : '';
        return '<div style="padding:4px 6px;border-bottom:1px solid var(--border-light);font-size:11px;color:var(--text2);word-break:break-word">'+esc(disp)+badge+'</div>';
      }).join('');
      var subtitle = '最近 Transcript (显示 ' + tail.length + ' 条';
      if (noiseFiltered > 0) subtitle += '，已过滤 ' + noiseFiltered + ' 条基础设施错误';
      subtitle += ')';
      sections.push(
        '<div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:14px;border:1px solid var(--border-light)">'
        + '<div style="font-weight:600;margin-bottom:8px">'+esc(subtitle)+'</div>'
        + (rows || '<div style="color:var(--text3);padding:6px">无可展示内容</div>')
        + '</div>'
      );
    }

    if (!sections.length) {
      body.innerHTML = '<div style="color:var(--text3);padding:20px;text-align:center">该 agent 暂无记忆数据</div>';
    } else {
      body.innerHTML = sections.join('');
    }
  }).catch(function(e){
    var body = document.getElementById('agent-mem-body');
    if (body) body.innerHTML = '<div style="color:var(--error)">加载失败: '+esc(String(e))+'</div>';
  });
}

function _memStatCard(label, count, color, desc) {
  return '<div style="text-align:center;padding:10px;border-radius:6px;background:'+color+'10;border:1px solid '+color+'30">'
    + '<div style="font-size:24px;font-weight:700;color:'+color+'">'+count+'</div>'
    + '<div style="font-size:12px;font-weight:600;color:'+color+';margin-top:2px">'+label+'</div>'
    + '<div style="font-size:10px;color:var(--text3);margin-top:2px">'+desc+'</div>'
    + '</div>';
}

function _catBadge(cat, label, count) {
  var colors = {intent:'#3b82f6',reasoning:'#f59e0b',outcome:'#10b981',rule:'#ef4444',reflection:'#8b5cf6'};
  var c = colors[cat] || 'var(--text3)';
  return '<span style="font-size:10px;padding:2px 6px;border-radius:3px;background:'+c+'15;color:'+c+'">'+label+' '+count+'</span>';
}

function compactAgentMemoryFromModal(aid) {
  if (!confirm('确定要压缩该 agent 的记忆？L1 将被折叠写入 L2。')) return;
  fetch('/api/portal/agent/'+encodeURIComponent(aid)+'/compact-memory', {method:'POST'})
    .then(function(r){return r.json();}).then(function(d){
      if (d && d.error) { alert('压缩失败: '+d.error); return; }
      alert('压缩完成');
      document.getElementById('agent-mem-modal').remove();
      showAgentMemoryView(aid);
    }).catch(function(e){ alert('压缩失败: '+e); });
}

function renderRolesSkillsHub() {
  var c = document.getElementById('content');
  var tabs = [
    { id: 'templates', label: '角色 / 专业领域', icon: 'library_books' },
    { id: 'skill-store', label: '技能商店 Store', icon: 'storefront' },
    { id: 'pending-skills', label: '技能锻造 SkillForge', icon: 'auto_fix_high' },
    { id: 'self-improvement', label: '学习闭环 / 经验沉淀', icon: 'psychology' },
  ];
  var r = _renderHubTabs('roles', tabs);
  c.innerHTML = r.html;

  var sc = document.getElementById('hub-roles-content');
  var _orig = document.getElementById('content');
  sc.id = 'content'; _orig.id = 'content-outer';
  try {
    if (r.current === 'templates') renderTemplateLibrary();
    else if (r.current === 'skill-store') renderSkillStore();
    else if (r.current === 'skill-pkgs') renderSkillStore();  // legacy redirect
    else if (r.current === 'pending-skills') renderPendingSkills();
    else if (r.current === 'self-improvement') renderSelfImprovement();
  } catch(e) { sc.innerHTML = '<div style="color:var(--error);padding:20px">'+e.message+'</div>'; }
  finally { sc.id = 'hub-roles-content'; _orig.id = 'content'; }
}

// ============ Skill Store (Hub-level marketplace) ============
var _skillStoreState = { q: '', source: '', entries: [], stats: null };

function _fmtSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}

async function renderSkillStore() {
  var c = document.getElementById('content');
  c.innerHTML = ''
    + '<div style="padding:18px">'
    + '  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;gap:12px">'
    + '    <div><h2 style="margin:0">技能商店 / Skill Store</h2>'
    + '      <div style="font-size:12px;color:var(--text3);margin-top:4px">兼容 Anthropic Agent Skills 规范 (SKILL.md) 与 TudouClaw manifest.yaml。按信任分级浏览、安装、授权给 agent。</div>'
    + '    </div>'
    + '    <div style="display:flex;gap:8px">'
    + '    <button class="btn btn-sm" onclick="_showLocalImportModal()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">folder_open</span> 从本地导入</button>'
    + '    <button class="btn btn-sm" onclick="_showRemoteScanModal()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">cloud_download</span> 从 URL 导入</button>'
    + '    <button class="btn btn-sm" onclick="rescanSkillStore()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">refresh</span> 重新扫描</button>'
    + '    </div>'
    + '  </div>'
    + '  <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">'
    + '    <input id="store-q" placeholder="搜索名称/描述/标签…" style="flex:1;min-width:260px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)" oninput="_skillStoreState.q=this.value;_debouncedLoadStore()">'
    + '    <select id="store-source" onchange="_skillStoreState.source=this.value;loadSkillStore()" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)">'
    + '      <option value="">所有来源</option>'
    + '      <option value="official">official 官方</option>'
    + '      <option value="maintainer">maintainer 维护</option>'
    + '      <option value="community">community 社区</option>'
    + '      <option value="agent">agent Agent创建</option>'
    + '      <option value="local">local 本地</option>'
    + '    </select>'
    + '  </div>'
    + '  <div id="store-stats" style="font-size:11px;color:var(--text3);margin-bottom:10px"></div>'
    + '  <div id="store-list" style="color:var(--text3)">加载中…</div>'
    + '</div>';
  loadSkillStore();
}

var _storeLoadTimer = null;
function _debouncedLoadStore() {
  if (_storeLoadTimer) clearTimeout(_storeLoadTimer);
  _storeLoadTimer = setTimeout(loadSkillStore, 250);
}

async function loadSkillStore() {
  var box = document.getElementById('store-list');
  if (!box) return;
  var params = [];
  if (_skillStoreState.q) params.push('q=' + encodeURIComponent(_skillStoreState.q));
  if (_skillStoreState.source) params.push('source=' + encodeURIComponent(_skillStoreState.source));
  var qs = params.length ? ('?' + params.join('&')) : '';
  try {
    var data = await api('GET', '/api/portal/skill-store' + qs);
    _skillStoreState.entries = data.entries || [];
    _skillStoreState.stats = data.stats || null;
    var annMap = {};
    (data.annotations || []).forEach(function(a){ annMap[a.skill_id] = a; });
    var installedMap = data.installed || {};
    var s = data.stats || {};
    var stats = document.getElementById('store-stats');
    if (stats) {
      var by = s.by_source || {};
      var pill = function(k,v){ return '<span style="padding:2px 8px;border:1px solid var(--border);border-radius:10px;margin-right:6px">'+esc(k)+': '+v+'</span>'; };
      stats.innerHTML = '共 ' + (s.total||0) + ' 个 · 已安装 ' + (s.installed||0) + ' · '
        + Object.keys(by).map(function(k){ return pill(k, by[k]); }).join('');
    }
    if (!_skillStoreState.entries.length) {
      box.innerHTML = '<div style="padding:20px;text-align:center">目录为空。把 SKILL.md 或 manifest.yaml 放到 data/skill_catalog/ 下再点"重新扫描"。</div>';
      return;
    }
    box.innerHTML = _skillStoreState.entries.map(function(e){
      var srcColor = e.source === 'official' ? '#10b981'
                   : e.source === 'maintainer' ? '#a78bfa'
                   : e.source === 'community' ? '#60a5fa'
                   : e.source === 'agent' ? '#f59e0b' : '#94a3b8';
      var ann = annMap[e.id] || annMap[e.installed_id];
      var annBadge = (ann && ann.notes && ann.notes.length)
        ? '<span title="本地笔记 " style="padding:2px 6px;font-size:10px;background:rgba(251,191,36,0.15);color:#f59e0b;border-radius:10px;margin-left:6px">💡 '+ann.notes.length+'</span>'
        : '';
      var actions = '';
      if (e.installed) {
        actions = '<button class="btn btn-sm" onclick="openGrantModal(\''+esc(e.installed_id)+'\',\''+esc(e.name)+'\')">授权给 Agent</button>'
                + '<button class="btn btn-sm" onclick="openAnnotateModal(\''+esc(e.installed_id)+'\',\''+esc(e.name)+'\')">📝 笔记</button>'
                + '<button class="btn btn-sm" style="color:var(--error)" onclick="uninstallStoreEntry(\''+esc(e.id)+'\')">卸载</button>';
      } else {
        actions = '<button class="btn btn-primary btn-sm" onclick="installStoreEntry(\''+esc(e.id)+'\')">安装到 Hub</button>';
      }
      var sensitive = e.sensitive ? '<span style="padding:2px 6px;font-size:10px;background:rgba(239,68,68,0.15);color:#ef4444;border-radius:10px;margin-left:6px">敏感</span>' : '';
      var tagList = (e.tags||[]).slice(0,6).map(function(t){ return '<span style="font-size:10px;padding:1px 6px;border:1px solid var(--border);border-radius:8px;margin-right:4px">'+esc(t)+'</span>'; }).join('');
      return '<div style="border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px;background:var(--surface)">'
        + '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">'
        + '  <div style="flex:1;min-width:0">'
        + '    <div style="font-weight:600;font-size:14px">'+esc(e.name)
        + '      <span style="font-size:11px;color:var(--text3);font-weight:400;margin-left:6px">'+esc(e.id)+' v'+esc(e.version)+'</span>'
        + '      <span style="padding:2px 8px;font-size:10px;background:'+srcColor+'20;color:'+srcColor+';border:1px solid '+srcColor+';border-radius:10px;margin-left:8px">'+esc(e.source)+'</span>'
        + annBadge + sensitive
        + '    </div>'
        + '    <div style="font-size:12px;color:var(--text2);margin-top:4px">'+esc(e.description||'')+'</div>'
        + '    <div style="font-size:11px;color:var(--text3);margin-top:6px">'
        + 'spec: '+esc(e.spec)+' · runtime: '+esc(e.runtime)+' · entry: '+esc(e.entry||'')
        + ' · author: '+esc(e.author)
        + (e.size_bytes ? ' · '+_fmtSize(e.size_bytes) : '')
        + (e.last_updated ? ' · '+new Date(e.last_updated*1000).toLocaleDateString() : '')
        + '</div>'
        + (e.languages && e.languages.length ? '<div style="font-size:10px;color:var(--text3);margin-top:3px">languages: '+esc(e.languages.join(', '))+'</div>' : '')
        + (tagList ? '<div style="margin-top:6px">'+tagList+'</div>' : '')
        + '  </div>'
        + '  <div style="display:flex;gap:6px;flex-shrink:0">'+actions+'</div>'
        + '</div></div>';
    }).join('');
  } catch(err) {
    box.innerHTML = '<div style="color:var(--error);padding:20px">加载失败: '+esc(String(err))+'</div>';
  }
}

async function rescanSkillStore() {
  try {
    await api('POST', '/api/portal/skill-store', {action: 'rescan'});
    loadSkillStore();
  } catch(e) { alert('扫描失败: '+e); }
}

async function installStoreEntry(entryId) {
  try {
    var r = await api('POST', '/api/portal/skill-store', {action:'install', entry_id: entryId});
    if (r && r.ok) loadSkillStore();
    else alert('安装失败: ' + JSON.stringify(r));
  } catch(e) { alert('安装失败: '+e); }
}

async function uninstallStoreEntry(entryId) {
  if (!confirm('卸载这个技能？对 agent 的授权会同时撤销。')) return;
  try {
    var r = await api('POST', '/api/portal/skill-store', {action:'uninstall', entry_id: entryId});
    if (r && r.ok) loadSkillStore();
    else alert('卸载失败');
  } catch(e) { alert('卸载失败: '+e); }
}

// ── Local Path Import Modal ──
function _showLocalImportModal() {
  var modal = document.createElement('div');
  modal.id = 'local-import-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:600px;width:94%;max-height:85vh;overflow:auto">'
    + '<h3 style="margin:0 0 6px">从本地路径导入技能</h3>'
    + '<div style="font-size:11px;color:var(--text3);margin-bottom:12px">'
    + '输入包含 SKILL.md 或 manifest.yaml 的本地目录绝对路径。系统会将技能包复制到 skill catalog 并自动安装。'
    + '</div>'
    + '<div style="margin-bottom:10px">'
    + '<input id="local-import-path" placeholder="/path/to/skill-folder  (包含 SKILL.md 的目录)" '
    + 'style="width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px;box-sizing:border-box">'
    + '</div>'
    + '<div style="margin-bottom:12px">'
    + '<label style="font-size:12px;color:var(--text2);margin-right:8px">来源分级:</label>'
    + '<select id="local-import-tier" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:12px">'
    + '<option value="community">community 社区</option>'
    + '<option value="local">local 本地</option>'
    + '</select>'
    + '</div>'
    + '<div id="local-import-status" style="font-size:12px;margin-bottom:8px;display:none"></div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px">'
    + '<button class="btn btn-sm" onclick="document.getElementById(\'local-import-modal\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" id="local-import-btn" onclick="_doLocalImport()">导入</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var inp = document.getElementById('local-import-path'); if (inp) inp.focus(); }, 0);
}

async function _doLocalImport() {
  var pathInput = document.getElementById('local-import-path');
  var srcPath = (pathInput && pathInput.value || '').trim();
  if (!srcPath) { alert('请输入本地路径'); return; }
  var tierSel = document.getElementById('local-import-tier');
  var tier = tierSel ? tierSel.value : 'community';
  var btn = document.getElementById('local-import-btn');
  var status = document.getElementById('local-import-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle;animation:spin 1s linear infinite">progress_activity</span> 导入中…';
  status.style.display = 'block';
  status.style.color = 'var(--text3)';
  status.innerHTML = '正在导入 ' + esc(srcPath) + ' …';
  try {
    var data = await api('POST', '/api/portal/skill-store', {
      action: 'import', src_path: srcPath, tier: tier, auto_install: true
    });
    if (data.ok) {
      status.style.color = '#10b981';
      status.innerHTML = '✓ 技能 <b>' + esc(data.name || '') + '</b> 已导入'
        + (data.install ? ' 并安装' : '') + '。';
      loadSkillStore();
      setTimeout(function(){
        var m = document.getElementById('local-import-modal');
        if (m) m.remove();
      }, 1500);
    } else {
      status.style.color = 'var(--error)';
      status.innerHTML = '导入失败: ' + esc(data.error || JSON.stringify(data));
    }
  } catch(err) {
    status.style.color = 'var(--error)';
    status.innerHTML = '请求失败: ' + esc(String(err));
  }
  btn.disabled = false;
  btn.innerHTML = '导入';
}

// ── Remote URL Scan Modal ──
var _remoteScanState = { temp_dir: '', skills: [] };

function _showRemoteScanModal() {
  var modal = document.createElement('div');
  modal.id = 'remote-scan-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:680px;width:94%;max-height:85vh;overflow:auto">'
    + '<h3 style="margin:0 0 6px">从 URL 导入技能</h3>'
    + '<div style="font-size:11px;color:var(--text3);margin-bottom:12px">'
    + '支持 GitHub 仓库地址、.zip、.tar.gz 文件链接。系统会扫描其中的 SKILL.md / manifest.yaml 技能包。'
    + '</div>'
    + '<div style="display:flex;gap:8px;margin-bottom:12px">'
    + '<input id="remote-scan-url" placeholder="https://github.com/user/repo  或  https://example.com/skill.zip" '
    + 'style="flex:1;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px">'
    + '<button id="remote-scan-btn" class="btn btn-primary btn-sm" onclick="_doRemoteScan()" style="white-space:nowrap">'
    + '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">search</span> 扫描</button>'
    + '</div>'
    + '<div id="remote-scan-status" style="font-size:12px;color:var(--text3);margin-bottom:8px;display:none"></div>'
    + '<div id="remote-scan-results"></div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '<button class="btn btn-sm" onclick="_closeRemoteScan()">关闭</button>'
    + '</div>'
    + '</div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var inp = document.getElementById('remote-scan-url'); if (inp) inp.focus(); }, 0);
}

function _closeRemoteScan() {
  // cleanup temp_dir if scan was done but not imported
  if (_remoteScanState.temp_dir) {
    try { api('POST', '/api/portal/skill-store', {action: 'cleanup_scan', temp_dir: _remoteScanState.temp_dir}); } catch(e) {}
    _remoteScanState.temp_dir = '';
    _remoteScanState.skills = [];
  }
  var m = document.getElementById('remote-scan-modal');
  if (m) m.remove();
}

async function _doRemoteScan() {
  var urlInput = document.getElementById('remote-scan-url');
  var url = (urlInput && urlInput.value || '').trim();
  if (!url) { alert('请输入 URL'); return; }
  var btn = document.getElementById('remote-scan-btn');
  var status = document.getElementById('remote-scan-status');
  var results = document.getElementById('remote-scan-results');
  btn.disabled = true;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle;animation:spin 1s linear infinite">progress_activity</span> 扫描中…';
  status.style.display = 'block';
  status.innerHTML = '正在下载并扫描 ' + esc(url) + ' …';
  results.innerHTML = '';
  // cleanup previous scan
  if (_remoteScanState.temp_dir) {
    try { await api('POST', '/api/portal/skill-store', {action: 'cleanup_scan', temp_dir: _remoteScanState.temp_dir}); } catch(e) {}
    _remoteScanState.temp_dir = '';
    _remoteScanState.skills = [];
  }
  try {
    var data = await api('POST', '/api/portal/skill-store', {action: 'scan_url', url: url});
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">search</span> 扫描';
    if (!data.ok) {
      status.innerHTML = '<span style="color:var(--error)">扫描失败: ' + esc(data.error || 'unknown') + '</span>';
      results.innerHTML = '';
      if (data.scanned_dirs) {
        results.innerHTML = '<div style="font-size:11px;color:var(--text3);margin-top:6px">目录结构: ' + esc(JSON.stringify(data.scanned_dirs).substring(0, 400)) + '</div>';
      }
      return;
    }
    _remoteScanState.temp_dir = data.temp_dir || '';
    _remoteScanState.skills = data.skills || [];
    status.innerHTML = '找到 <b>' + data.skill_count + '</b> 个技能包：';
    _renderScanResults(data.skills || []);
  } catch(err) {
    btn.disabled = false;
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">search</span> 扫描';
    status.innerHTML = '<span style="color:var(--error)">请求失败: ' + esc(String(err)) + '</span>';
  }
}

function _renderScanResults(skills) {
  var box = document.getElementById('remote-scan-results');
  if (!box) return;
  if (!skills.length) { box.innerHTML = ''; return; }
  var html = skills.map(function(s, idx) {
    var specColor = s.spec === 'agent-skills' ? '#10b981' : '#60a5fa';
    var filesInfo = (s.files || []).slice(0, 8).map(function(f) {
      var sz = f.size > 1024 ? ((f.size / 1024).toFixed(1) + ' KB') : (f.size + ' B');
      return esc(f.name) + ' (' + sz + ')';
    }).join(', ');
    return '<div style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px;background:var(--surface)">'
      + '<div style="display:flex;align-items:flex-start;gap:10px">'
      + '<input type="checkbox" id="scan-skill-' + idx + '" data-skill-name="' + esc(s.name) + '" checked style="margin-top:4px">'
      + '<div style="flex:1;min-width:0">'
      + '<div style="font-weight:600;font-size:14px">' + esc(s.name)
      + '  <span style="padding:2px 8px;font-size:10px;background:' + specColor + '20;color:' + specColor + ';border:1px solid ' + specColor + ';border-radius:10px;margin-left:6px">' + esc(s.spec) + '</span>'
      + (s.version ? '<span style="font-size:11px;color:var(--text3);margin-left:6px">v' + esc(s.version) + '</span>' : '')
      + '</div>'
      + '<div style="font-size:12px;color:var(--text2);margin-top:4px">' + esc(s.description || '') + '</div>'
      + '<div style="font-size:11px;color:var(--text3);margin-top:4px">author: ' + esc(s.author || '-') + ' · runtime: ' + esc(s.runtime || '-') + ' · files: ' + (s.file_count || 0) + '</div>'
      + (filesInfo ? '<div style="font-size:10px;color:var(--text3);margin-top:2px">' + filesInfo + '</div>' : '')
      + '</div></div></div>';
  }).join('');
  html += '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px">'
    + '<select id="scan-import-tier" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:12px">'
    + '<option value="community">community 社区</option>'
    + '<option value="local">local 本地</option>'
    + '</select>'
    + '<button class="btn btn-primary btn-sm" id="scan-import-btn" onclick="_doImportScanned()">导入选中的技能</button>'
    + '</div>';
  box.innerHTML = html;
}

async function _doImportScanned() {
  if (!_remoteScanState.temp_dir) { alert('没有扫描数据'); return; }
  var checks = document.querySelectorAll('[id^="scan-skill-"]');
  var names = [];
  for (var i = 0; i < checks.length; i++) {
    if (checks[i].checked) names.push(checks[i].getAttribute('data-skill-name'));
  }
  if (!names.length) { alert('请至少选择一个技能'); return; }
  var tierSel = document.getElementById('scan-import-tier');
  var tier = tierSel ? tierSel.value : 'community';
  var btn = document.getElementById('scan-import-btn');
  var status = document.getElementById('remote-scan-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle;animation:spin 1s linear infinite">progress_activity</span> 导入中…';
  status.innerHTML = '正在导入 ' + names.length + ' 个技能…';
  try {
    var data = await api('POST', '/api/portal/skill-store', {
      action: 'import_scanned',
      temp_dir: _remoteScanState.temp_dir,
      skill_names: names,
      tier: tier,
      auto_install: true,
    });
    _remoteScanState.temp_dir = '';  // temp dir cleaned by backend
    _remoteScanState.skills = [];
    if (data.ok) {
      var imported = (data.results || []).filter(function(r){ return r.ok; });
      var failed = (data.results || []).filter(function(r){ return !r.ok; });
      var msg = '成功导入 ' + imported.length + ' 个技能';
      if (failed.length) msg += '，' + failed.length + ' 个失败';
      status.innerHTML = '<span style="color:#10b981">' + msg + '</span>';
      var results = document.getElementById('remote-scan-results');
      if (results) {
        var detailHtml = (data.results || []).map(function(r) {
          if (r.ok) return '<div style="font-size:12px;color:#10b981;padding:4px 0">✓ ' + esc(r.name) + ' 已导入' + (r.install ? ' 并安装' : '') + '</div>';
          return '<div style="font-size:12px;color:var(--error);padding:4px 0">✗ ' + esc(r.name || '?') + ': ' + esc(r.error || 'unknown') + '</div>';
        }).join('');
        results.innerHTML = detailHtml;
      }
      // Refresh skill store list
      loadSkillStore();
    } else {
      status.innerHTML = '<span style="color:var(--error)">导入失败: ' + esc(data.error || 'unknown') + '</span>';
    }
  } catch(err) {
    status.innerHTML = '<span style="color:var(--error)">请求失败: ' + esc(String(err)) + '</span>';
  }
  btn.disabled = false;
  btn.innerHTML = '导入选中的技能';
}

async function openGrantModal(installedId, skillName) {
  var ags = [];
  try {
    var d = await api('GET', '/api/portal/agents');
    ags = (d && d.agents) || [];
  } catch(e) { alert('无法获取 agent 列表: '+e); return; }
  if (!ags.length) { alert('还没有 agent'); return; }
  var opts = ags.map(function(a){
    var granted = (a.granted_skills||[]).indexOf(installedId) >= 0;
    return '<label style="display:flex;align-items:center;gap:8px;padding:6px;border-bottom:1px solid var(--border)"><input type="checkbox" data-agent-id="'+esc(a.id)+'" '+(granted?'checked':'')+'><span>'+esc(a.name||a.id)+' <span style="color:var(--text3);font-size:11px">('+esc(a.role||'-')+')</span></span></label>';
  }).join('');
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:18px;max-width:500px;width:90%;max-height:80vh;overflow:auto">'
    + '<h3 style="margin:0 0 10px">授权 '+esc(skillName)+' 给 Agent</h3>'
    + '<div style="font-size:11px;color:var(--text3);margin-bottom:10px">勾选后点击保存。会同时向 agent working_dir/.claw/granted_skills/ 写入 pointer 文件，支持独立进程 agent 发现。</div>'
    + '<div id="grant-ag-list" style="max-height:320px;overflow:auto">'+opts+'</div>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="submitGrant(\''+esc(installedId)+'\', this)">保存</button>'
    + '</div></div>';
  document.body.appendChild(modal);
}

async function submitGrant(installedId, btn) {
  var modal = btn.closest('div[style*=fixed]');
  var checks = modal.querySelectorAll('#grant-ag-list input[type=checkbox]');
  btn.disabled = true;
  for (var i=0;i<checks.length;i++) {
    var cb = checks[i];
    var aid = cb.getAttribute('data-agent-id');
    var action = cb.checked ? 'grant' : 'revoke';
    try {
      await api('POST', '/api/portal/skill-store', {action: action, installed_id: installedId, agent_id: aid});
    } catch(e) { /* swallow */ }
  }
  modal.remove();
  loadSkillStore();
}

function openAnnotateModal(installedId, skillName) {
  var modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:18px;max-width:560px;width:90%">'
    + '<h3 style="margin:0 0 6px">📝 为 '+esc(skillName)+' 添加笔记</h3>'
    + '<div style="font-size:11px;color:var(--text3);margin-bottom:10px">本地笔记会在 agent 加载这个 skill 时自动附加到 prompt 里，下次 session 无需手动回忆。建议写：踩过的坑 / workaround / 版本差异。</div>'
    + '<textarea id="ann-text" rows="5" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px" placeholder="例如：调用时如果 traceId 为空会 500，必须先生成 UUID 再传入"></textarea>'
    + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">'
    + '<button class="btn btn-sm" onclick="this.closest(\'div[style*=fixed]\').remove()">取消</button>'
    + '<button class="btn btn-primary btn-sm" onclick="submitAnnotate(\''+esc(installedId)+'\', this)">保存</button>'
    + '</div></div>';
  document.body.appendChild(modal);
  setTimeout(function(){ var ta = document.getElementById('ann-text'); if (ta) ta.focus(); }, 0);
}

async function submitAnnotate(installedId, btn) {
  var modal = btn.closest('div[style*=fixed]');
  var ta = modal.querySelector('#ann-text');
  var text = (ta && ta.value || '').trim();
  if (!text) { alert('笔记不能为空'); return; }
  try {
    await api('POST', '/api/portal/skill-store', {action:'annotate', skill_id: installedId, text: text});
    modal.remove();
    loadSkillStore();
  } catch(e) { alert('保存失败: '+e); }
}

// ============ Skill Packages (new SkillRegistry-backed UI) ============
function renderSkillPkgs() {
  var c = document.getElementById('content');
  c.innerHTML = '<div style="padding:18px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px"><h2 style="margin:0">技能库 / Skill Packages</h2><button class="btn btn-primary btn-sm" onclick="showInstallSkillPkg()"><span class="material-symbols-outlined" style="font-size:16px">add</span> 安装技能</button></div><div id="skill-pkgs-list" style="color:var(--text3)">加载中…</div></div>';
  fetch('/api/portal/skill-pkgs').then(function(r){return r.json();}).then(function(d){
    var box = document.getElementById('skill-pkgs-list');
    var items = (d && d.skills) || [];
    if (!items.length) { box.innerHTML = '<div style="padding:20px;text-align:center">暂无已安装的技能。点击右上角"安装技能"添加。</div>'; return; }
    var rows = items.map(function(s){
      var m = s.manifest || {};
      var grants = (s.granted_to || []).length;
      var statusColor = s.status === 'ready' ? 'var(--success)' : (s.status === 'error' ? 'var(--error)' : 'var(--warning)');
      return '<div style="border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px;background:var(--surface)">'
        + '<div style="display:flex;justify-content:space-between;align-items:center"><div><div style="font-weight:600;font-size:14px">'+esc(m.name||s.id)+' <span style="font-size:11px;color:var(--text3);font-weight:400">v'+esc(m.version||'?')+'</span></div>'
        + '<div style="font-size:12px;color:var(--text2);margin-top:4px">'+esc(m.description||'')+'</div>'
        + '<div style="font-size:11px;color:var(--text3);margin-top:6px">runtime: '+esc(m.runtime||'?')+' · 触发词: '+esc((m.triggers||[]).join(', ')||'-')+' · 已授权 '+grants+' 个 agent</div></div>'
        + '<div style="display:flex;gap:6px;align-items:center"><span style="font-size:11px;color:'+statusColor+';padding:2px 8px;border:1px solid '+statusColor+';border-radius:4px">'+esc(s.status||'?')+'</span>'
        + '<button class="btn btn-sm" onclick="grantSkillPkg(\''+esc(s.id)+'\')">授权</button>'
        + '<button class="btn btn-sm" style="color:var(--error)" onclick="uninstallSkillPkg(\''+esc(s.id)+'\')">卸载</button></div></div></div>';
    }).join('');
    box.innerHTML = rows;
  }).catch(function(e){
    document.getElementById('skill-pkgs-list').innerHTML = '<div style="color:var(--error)">加载失败: '+esc(String(e))+'</div>';
  });
}

function showInstallSkillPkg() {
  var c = document.getElementById('content');
  c.innerHTML = ''
    + '<div style="padding:18px;max-width:760px">'
    + '  <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">'
    + '    <button class="btn btn-sm" onclick="renderSkillPkgs()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">arrow_back</span> 返回</button>'
    + '    <h2 style="margin:0">安装技能包</h2>'
    + '  </div>'
    + '  <div style="border:1px solid var(--border);border-radius:10px;padding:18px;background:var(--surface)">'
    + '    <div style="font-size:13px;color:var(--text2);margin-bottom:10px">从服务器本地目录安装一个技能包。技能包目录需包含 <code>SKILL.md</code> 和可执行脚本。</div>'
    + '    <label style="display:block;font-size:12px;color:var(--text3);margin-bottom:6px">技能包目录绝对路径 (服务器侧)</label>'
    + '    <input id="install-skill-path" type="text" placeholder="例如：/Users/you/skills/my_skill" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-family:ui-monospace,Menlo,monospace;font-size:13px">'
    + '    <div style="margin-top:6px;font-size:11px;color:var(--text3)">支持 <code>~</code> 开头的用户目录；相对路径会基于服务器工作目录解析。</div>'
    + '    <div id="install-skill-msg" style="margin-top:12px;font-size:12px;min-height:16px"></div>'
    + '    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">'
    + '      <button class="btn btn-sm" onclick="renderSkillPkgs()">取消</button>'
    + '      <button id="install-skill-submit" class="btn btn-primary btn-sm" onclick="submitInstallSkillPkg()">安装</button>'
    + '    </div>'
    + '  </div>'
    + '</div>';
  setTimeout(function(){
    var inp = document.getElementById('install-skill-path');
    if (inp) {
      inp.focus();
      inp.addEventListener('keydown', function(e){
        if (e.key === 'Enter') { e.preventDefault(); submitInstallSkillPkg(); }
      });
    }
  }, 0);
}

function submitInstallSkillPkg() {
  var inp = document.getElementById('install-skill-path');
  var msg = document.getElementById('install-skill-msg');
  var btn = document.getElementById('install-skill-submit');
  if (!inp || !msg || !btn) return;
  var path = (inp.value || '').trim();
  if (!path) {
    msg.innerHTML = '<span style="color:var(--error)">请输入技能包目录路径</span>';
    inp.focus();
    return;
  }
  btn.disabled = true;
  msg.innerHTML = '<span style="color:var(--text3)">安装中…</span>';
  fetch('/api/portal/skill-pkgs/install', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({path: path})
  }).then(function(r){return r.json();}).then(function(d){
    btn.disabled = false;
    if (d && d.error) {
      msg.innerHTML = '<span style="color:var(--error)">安装失败：'+esc(String(d.error))+'</span>';
      return;
    }
    var sid = (d && d.skill && d.skill.id) || '';
    msg.innerHTML = '<span style="color:var(--success)">✓ 已安装：'+esc(sid)+'，正在返回列表…</span>';
    setTimeout(function(){ renderSkillPkgs(); }, 600);
  }).catch(function(e){
    btn.disabled = false;
    msg.innerHTML = '<span style="color:var(--error)">请求失败：'+esc(String(e))+'</span>';
  });
}

function grantSkillPkg(sid) {
  // Look up the skill so we can show which agents already have it.
  fetch('/api/portal/skill-pkgs').then(function(r){return r.json();}).then(function(d){
    var items = (d && d.skills) || [];
    var sk = null;
    for (var i=0;i<items.length;i++) { if (items[i].id === sid) { sk = items[i]; break; } }
    var granted = (sk && sk.granted_to) || [];
    var grantedSet = {};
    granted.forEach(function(a){ grantedSet[a] = true; });
    var skName = (sk && sk.manifest && sk.manifest.name) || sid;

    var list = (typeof agents !== 'undefined' && agents) ? agents : [];
    if (!list.length) { alert('当前没有可授权的 agent，请先创建 agent。'); return; }

    // Filter: only include top-level agents (hide sub-agents)
    var visible = list.filter(function(a){ return !a.parent_id; });
    if (!visible.length) visible = list;

    var rows = visible.map(function(a){
      var checked = grantedSet[a.id] ? 'checked' : '';
      var roleLabel = esc(a.role || '');
      var nodeLabel = a.node_id && a.node_id !== 'local' ? ' · '+esc(a.node_id) : '';
      return '<label style="display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;cursor:pointer;background:var(--surface)">'
        + '<input type="checkbox" data-aid="'+esc(a.id)+'" '+checked+' style="margin:0">'
        + '<div style="flex:1;min-width:0">'
        + '<div style="font-weight:600;font-size:13px">'+esc(a.name||a.id)+'</div>'
        + '<div style="font-size:11px;color:var(--text3)">'+roleLabel+nodeLabel+' · '+esc(a.id.slice(0,8))+'</div>'
        + '</div></label>';
    }).join('');

    var modal = document.createElement('div');
    modal.id = 'grant-skill-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
    modal.innerHTML = ''
      + '<div style="background:var(--bg);border:1px solid var(--border);border-radius:10px;width:480px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column">'
      + '  <div style="padding:14px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">'
      + '    <div><div style="font-weight:600">授权技能：'+esc(skName)+'</div>'
      + '    <div style="font-size:11px;color:var(--text3);margin-top:2px">勾选要授权的 agent · 取消勾选已授权项以撤销</div></div>'
      + '    <button class="btn btn-sm" onclick="document.getElementById(\'grant-skill-modal\').remove()">×</button>'
      + '  </div>'
      + '  <div style="padding:8px 10px"><input id="grant-skill-search" placeholder="搜索 agent…" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text)"></div>'
      + '  <div id="grant-skill-list" style="flex:1;overflow:auto;padding:6px 12px 0">'+rows+'</div>'
      + '  <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px">'
      + '    <button class="btn btn-sm" onclick="document.getElementById(\'grant-skill-modal\').remove()">取消</button>'
      + '    <button class="btn btn-primary btn-sm" onclick="submitGrantSkill(\''+esc(sid)+'\')">保存</button>'
      + '  </div>'
      + '</div>';
    document.body.appendChild(modal);

    // Search filter
    document.getElementById('grant-skill-search').addEventListener('input', function(e){
      var q = (e.target.value || '').toLowerCase();
      var labels = document.querySelectorAll('#grant-skill-list label');
      labels.forEach(function(lb){
        lb.style.display = lb.textContent.toLowerCase().indexOf(q) >= 0 ? 'flex' : 'none';
      });
    });
    // Stash original grants for diff
    modal._originalGrants = grantedSet;
  }).catch(function(e){ alert('加载技能信息失败: '+e); });
}

function submitGrantSkill(sid) {
  var modal = document.getElementById('grant-skill-modal');
  if (!modal) return;
  var orig = modal._originalGrants || {};
  var checks = modal.querySelectorAll('#grant-skill-list input[type=checkbox]');
  var toGrant = [], toRevoke = [];
  checks.forEach(function(cb){
    var aid = cb.getAttribute('data-aid');
    var was = !!orig[aid];
    if (cb.checked && !was) toGrant.push(aid);
    if (!cb.checked && was) toRevoke.push(aid);
  });
  if (!toGrant.length && !toRevoke.length) { modal.remove(); return; }
  var calls = [];
  toGrant.forEach(function(aid){
    calls.push(fetch('/api/portal/skill-pkgs/'+encodeURIComponent(sid)+'/grant', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent_id: aid})
    }).then(function(r){return r.json();}));
  });
  toRevoke.forEach(function(aid){
    calls.push(fetch('/api/portal/skill-pkgs/'+encodeURIComponent(sid)+'/revoke', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent_id: aid})
    }).then(function(r){return r.json();}));
  });
  Promise.all(calls).then(function(results){
    var errs = results.filter(function(d){return d && d.error;});
    if (errs.length) { alert('部分操作失败：'+errs.map(function(e){return e.error;}).join('; ')); }
    modal.remove();
    renderSkillPkgs();
  }).catch(function(e){ alert('授权失败: '+e); });
}

function uninstallSkillPkg(sid) {
  if (!confirm('确定卸载技能 '+sid+'？')) return;
  fetch('/api/portal/skill-pkgs/'+encodeURIComponent(sid)+'/uninstall', {method:'POST'})
    .then(function(r){return r.json();}).then(function(d){
      if (d.error) { alert('卸载失败: '+d.error); return; }
      renderSkillPkgs();
    });
}

function renderToolsApprovalsHub() {
  var c = document.getElementById('content');
  var actionsEl = document.getElementById('topbar-actions');
  var tabs = [
    { id: 'approvals', label: '待审批 / 历史', icon: 'shield' },
    { id: 'mcpconfig', label: 'MCP 服务器', icon: 'hub' },
  ];
  var r = _renderHubTabs('tools', tabs);
  c.innerHTML = r.html;
  if (r.current === 'mcpconfig') {
    actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showAddMCP()"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add MCP</button>';
  }
  var sc = document.getElementById('hub-tools-content');
  var _orig = document.getElementById('content');
  sc.id = 'content'; _orig.id = 'content-outer';
  try {
    if (r.current === 'approvals') renderApprovals();
    else if (r.current === 'mcpconfig') renderMCPConfig();
  } catch(e) { sc.innerHTML = '<div style="color:var(--error);padding:20px">'+e.message+'</div>'; }
  finally { sc.id = 'hub-tools-content'; _orig.id = 'content'; }
}

function renderIntegrationsHub() {
  var c = document.getElementById('content');
  var sc;
  c.innerHTML = '<div id="integrations-channels-wrap"></div>';
  sc = document.getElementById('integrations-channels-wrap');
  var _orig = document.getElementById('content');
  sc.id = 'content'; _orig.id = 'content-outer';
  try { renderChannels(); }
  catch(e) { sc.innerHTML = '<div style="color:var(--error);padding:20px">'+e.message+'</div>'; }
  finally { sc.id = 'integrations-channels-wrap'; _orig.id = 'content'; }
}

function renderSettingsHub() {
  var c = document.getElementById('content');
  var tabs = [
    { id: 'config', label: '全局配置', icon: 'settings' },
    { id: 'providers', label: 'LLM 提供商', icon: 'dns' },
    { id: 'nodeconfig', label: '节点配置', icon: 'tune' },
    { id: 'nodes', label: '节点列表', icon: 'device_hub' },
    { id: 'tokens', label: 'API Tokens', icon: 'key' },
    { id: 'audit', label: '审计日志', icon: 'assignment' },
  ];
  var r = _renderHubTabs('settings', tabs);
  c.innerHTML = r.html;
  var actionsEl = document.getElementById('topbar-actions');
  var sc = document.getElementById('hub-settings-content');
  var _orig = document.getElementById('content');
  sc.id = 'content'; _orig.id = 'content-outer';
  try {
    if (r.current === 'config') renderConfig();
    else if (r.current === 'providers') {
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-provider\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add Provider</button>';
      renderProviders();
    } else if (r.current === 'nodeconfig') renderNodeConfig();
    else if (r.current === 'nodes') {
      actionsEl.innerHTML = '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-node\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Connect Node</button>';
      renderNodes();
    } else if (r.current === 'tokens') renderTokens();
    else if (r.current === 'audit') renderAudit();
  } catch(e) { sc.innerHTML = '<div style="color:var(--error);padding:20px">'+e.message+'</div>'; }
  finally { sc.id = 'hub-settings-content'; _orig.id = 'content'; }
}

// ============ Node Switcher (topbar global) ============
var _globalNodeFilter = '';
function onNodeSwitch(nodeId) {
  _globalNodeFilter = nodeId;
  renderCurrentView();
}
function _refreshNodeSwitcher() {
  var sel = document.getElementById('global-node-switcher');
  if (!sel || !nodes) return;
  var current = sel.value;
  var html = '<option value="">所有节点</option>';
  nodes.forEach(function(n) {
    var label = (n.name || n.node_id) + (n.is_self ? ' (Local)' : '')
      + (n.project_count ? ' · ' + n.project_count + 'p' : '');
    html += '<option value="'+esc(n.node_id||'')+'">'+esc(label)+'</option>';
  });
  sel.innerHTML = html;
  if (current) sel.value = current;
}

function renderSettingsPage() {
  var c = document.getElementById('content');
  var tabs = [
    { id: 'providers', label: 'LLM Providers', icon: 'dns' },
    { id: 'mcpconfig', label: 'MCP', icon: 'hub' },
    { id: 'config', label: 'Global Config', icon: 'settings' },
    { id: 'nodeconfig', label: 'Node Config', icon: 'tune' },
    { id: 'nodes', label: 'Nodes', icon: 'device_hub' },
    { id: 'channels', label: 'Channels', icon: 'cable' },
    { id: 'templates', label: '专业领域', icon: 'library_books' },
    { id: 'policy', label: '审批策略 Policy', icon: 'shield' },
    { id: 'tokens', label: 'API Tokens', icon: 'key' },
  ];

  var tabsHtml = tabs.map(function(t) {
    var active = _settingsSubTab === t.id;
    return '<button onclick="_settingsSubTab=\''+t.id+'\';renderSettingsPage()" style="padding:8px 16px;border:none;background:'+(active?'var(--surface2)':'none')+';color:'+(active?'var(--primary)':'var(--text3)')+';font-size:12px;font-weight:'+(active?'700':'500')+';cursor:pointer;border-radius:8px;display:flex;align-items:center;gap:6px;font-family:inherit;white-space:nowrap;transition:all 0.15s"><span class="material-symbols-outlined" style="font-size:16px">'+t.icon+'</span>'+t.label+'</button>';
  }).join('');

  c.innerHTML = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;padding:8px;background:var(--surface);border-radius:12px;border:1px solid var(--border-light)">'+tabsHtml+'</div><div id="settings-content"></div>';

  var sc = document.getElementById('settings-content');
  var actionsEl = document.getElementById('topbar-actions');
  var _tabActions = {
    'providers':  '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-provider\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add Provider</button>',
    'mcpconfig':  '<button class="btn btn-primary btn-sm" onclick="showAddMCP()"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add MCP</button>',
    'nodes':      '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-node\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Connect Node</button>',
    'channels':   '<button class="btn btn-primary btn-sm" onclick="showModal(\'add-channel\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Add Channel</button>',
    'tokens':     '<button class="btn btn-primary btn-sm" onclick="showModal(\'create-token\')"><span class="material-symbols-outlined" style="font-size:16px">add</span> Create Token</button>',
    'templates':  '<button class="btn btn-primary btn-sm" onclick="showCreateTemplate()"><span class="material-symbols-outlined" style="font-size:16px">add</span> New Template</button>',
  };
  if (actionsEl) actionsEl.innerHTML = _tabActions[_settingsSubTab] || '';
  switch(_settingsSubTab) {
    case 'providers': renderProviders(sc); break;
    case 'mcpconfig': renderMCPConfig(sc); break;
    case 'config': renderConfig(sc); break;
    case 'nodeconfig': renderNodeConfig(sc); break;
    case 'nodes': renderNodes(sc); break;
    case 'channels': renderChannels(sc); break;
    case 'templates': renderTemplateLibrary(sc); break;
    case 'policy': renderPolicyConfig(sc); break;
    case 'tokens': renderTokens(sc); break;
    default: sc.innerHTML = '<div style="color:var(--text3);padding:20px">Select a settings tab</div>';
  }
}

// ============ Policy Config Page (审批策略) ============
async function renderPolicyConfig(container) {
  var c = container || document.getElementById('content');
  c.innerHTML = '<div style="color:var(--text3);padding:20px">Loading policy config...</div>';
  try {
    var cfg = await api('GET', '/api/portal/policy');
    if (!cfg) return;

    var riskColors = { red:'#ef4444', high:'#f59e0b', moderate:'#3b82f6', low:'#22c55e' };
    var riskLabels = { red:'🚫 红线', high:'⚠️ 高风险', moderate:'🟡 中风险', low:'✅ 低风险' };
    var riskOptions = ['red','high','moderate','low'];

    // Group tools by current risk
    var toolsByRisk = { red:[], high:[], moderate:[], low:[] };
    var toolRisks = cfg.tool_risks || {};
    Object.keys(toolRisks).sort().forEach(function(t) {
      var r = toolRisks[t];
      if (toolsByRisk[r]) toolsByRisk[r].push(t);
      else toolsByRisk['moderate'].push(t);
    });

    var html = '<div style="margin-bottom:20px">' +
      '<h3 style="font-family:\'Plus Jakarta Sans\',sans-serif;font-size:20px;font-weight:700;margin-bottom:4px">审批策略 Tool Policy</h3>' +
      '<p style="color:var(--text2);font-size:13px">管理员可以调整每个工具的风险级别，定义不同角色的审批权限。</p>' +
    '</div>';

    // ── Risk level legend ──
    html += '<div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap">';
    (cfg.risk_levels||[]).forEach(function(rl) {
      html += '<div style="display:flex;align-items:center;gap:6px;padding:8px 14px;background:var(--surface);border-radius:8px;border:1px solid rgba(255,255,255,0.05);font-size:12px">' +
        '<span style="width:10px;height:10px;border-radius:50%;background:'+riskColors[rl.id]+'"></span>' +
        '<span style="font-weight:700">'+rl.label+'</span>' +
        '<span style="color:var(--text3);font-size:11px">'+rl.desc+'</span>' +
      '</div>';
    });
    html += '</div>';

    // ── Tool risk table ──
    var toolDescriptions = {
      read_file:'读取文件 Read file contents', search_files:'搜索文件内容 Search file contents (grep)', glob_files:'文件名匹配 Find files by pattern',
      web_search:'网络搜索 Search the web', web_fetch:'抓取网页 Fetch web page content', write_file:'写入/创建文件 Write or create a file',
      edit_file:'编辑文件 Edit existing file', bash:'执行Shell命令 Execute shell commands', team_create:'创建子Agent Spawn sub-agent',
      send_message:'Agent间消息 Inter-agent messaging', task_update:'任务状态更新 Update task status', plan_update:'计划更新 Update plan',
      web_screenshot:'网页截图 Capture web screenshot', http_request:'HTTP请求 Outbound HTTP/API call', mcp_call:'调用MCP工具 Call external MCP tool',
      datetime_calc:'日期时间计算 Date/time calculation', json_process:'JSON处理 JSON parsing & transform', text_process:'文本处理 Text processing',
      delete_file:'删除文件 Delete a file', rm_rf:'递归删除目录 Recursive directory delete', drop_table:'删除数据库表 Drop database table', truncate:'清空数据表 Truncate database table',
    };
    var toolCategories = {
      'low': { icon:'✅', label:'查询/读取 Query & Read', sort: 4 },
      'moderate': { icon:'🟡', label:'创建/修改 Create & Modify', sort: 3 },
      'high': { icon:'⚠️', label:'危险执行 Dangerous Execution', sort: 2 },
      'red': { icon:'🚫', label:'红线操作 Red Line (Destructive)', sort: 1 },
    };

    // Group tools by current risk level
    var groupedTools = {};
    riskOptions.forEach(function(r) { groupedTools[r] = []; });
    Object.keys(toolRisks).forEach(function(t) {
      var r = toolRisks[t];
      if (!groupedTools[r]) groupedTools[r] = [];
      groupedTools[r].push(t);
    });
    // Sort each group alphabetically
    Object.keys(groupedTools).forEach(function(r) { groupedTools[r].sort(); });

    html += '<div style="margin-bottom:24px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
        '<div style="font-size:14px;font-weight:700">工具风险级别 Tool Risk Levels</div>' +
        '<button class="btn btn-ghost btn-sm" onclick="showAddToolRiskPrompt()" style="font-size:11px;display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">add</span>Add Custom Tool</button>' +
      '</div>' +
      '<div style="background:var(--surface);border-radius:12px;border:1px solid var(--border-light);overflow:hidden">' +
      '<table style="width:100%;border-collapse:collapse;font-size:13px">' +
      '<thead><tr style="background:var(--surface2)">' +
        '<th style="text-align:left;padding:10px 14px;font-weight:700;color:var(--text);width:200px">Tool</th>' +
        '<th style="text-align:left;padding:10px 14px;font-weight:700;color:var(--text);width:180px">Risk Level</th>' +
        '<th style="text-align:left;padding:10px 14px;font-weight:700;color:var(--text)">Description</th>' +
      '</tr></thead><tbody>';

    // Render grouped: red first, then high, moderate, low
    ['red','high','moderate','low'].forEach(function(riskGroup) {
      var tools = groupedTools[riskGroup] || [];
      if (tools.length === 0) return;
      var cat = toolCategories[riskGroup] || {};
      html += '<tr style="background:rgba(' + (riskGroup==='red'?'239,68,68':riskGroup==='high'?'245,158,11':riskGroup==='moderate'?'59,130,246':'34,197,94') + ',0.06)">' +
        '<td colspan="3" style="padding:8px 14px;font-size:12px;font-weight:700;color:' + riskColors[riskGroup] + '">' +
          (cat.icon||'') + ' ' + (cat.label||riskGroup) + ' (' + tools.length + ')' +
        '</td></tr>';
      tools.forEach(function(tool) {
        var risk = toolRisks[tool];
        var desc = toolDescriptions[tool] || tool;
        html += '<tr style="border-top:1px solid rgba(255,255,255,0.04)">' +
          '<td style="padding:7px 14px 7px 28px;font-family:monospace;font-size:12px;color:var(--text)">' + esc(tool) + '</td>' +
          '<td style="padding:7px 14px">' +
            '<select onchange="updateToolRisk(\'' + esc(tool) + '\',this.value)" style="background:var(--surface2);color:' + riskColors[risk] + ';border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;font-weight:600">';
        riskOptions.forEach(function(ro) {
          html += '<option value="' + ro + '" ' + (risk===ro?'selected':'') + '>' + riskLabels[ro] + '</option>';
        });
        html += '</select></td>' +
          '<td style="padding:7px 14px;color:var(--text3);font-size:12px">' + esc(desc) + '</td>' +
        '</tr>';
      });
    });

    html += '</tbody></table></div></div>';

    // ── Role approval authority ──
    html += '<div style="margin-bottom:24px">' +
      '<div style="font-size:14px;font-weight:700;margin-bottom:10px">角色审批权限 Role Approval Authority</div>' +
      '<p style="color:var(--text3);font-size:12px;margin-bottom:10px">定义每个角色可以审批哪些风险级别的操作（勾选 = 该角色可以审批）</p>' +
      '<div style="background:var(--surface);border-radius:12px;border:1px solid var(--border-light);overflow:hidden">' +
      '<table style="width:100%;border-collapse:collapse;font-size:13px">' +
      '<thead><tr style="background:var(--surface2)">' +
        '<th style="text-align:left;padding:10px 14px;font-weight:700">Role</th>';
    riskOptions.forEach(function(ro) {
      html += '<th style="text-align:center;padding:10px 14px;font-weight:700;color:'+riskColors[ro]+'">'+riskLabels[ro]+'</th>';
    });
    html += '</tr></thead><tbody>';

    var priLabels = cfg.priority_labels || {};
    var priAuth = cfg.priority_approval_authority || {};
    ['0','1','2','3'].forEach(function(pri) {
      var label = priLabels[pri] || 'Priority ' + pri;
      var allowed = priAuth[pri] || [];
      html += '<tr style="border-top:1px solid rgba(255,255,255,0.05)">' +
        '<td style="padding:8px 14px;font-weight:600">'+esc(label)+'</td>';
      riskOptions.forEach(function(ro) {
        var checked = allowed.indexOf(ro) >= 0;
        html += '<td style="text-align:center;padding:8px 14px">' +
          '<input type="checkbox" '+(checked?'checked':'')+' onchange="updatePriorityAuth('+pri+',\''+ro+'\',this.checked)" style="cursor:pointer;width:16px;height:16px;accent-color:'+riskColors[ro]+'">' +
        '</td>';
      });
      html += '</tr>';
    });

    html += '</tbody></table></div></div>';

    // ── Global settings ──
    html += '<div style="margin-bottom:24px">' +
      '<div style="font-size:14px;font-weight:700;margin-bottom:10px">全局设置 Global Settings</div>' +
      '<div style="display:flex;flex-direction:column;gap:12px;background:var(--surface);border-radius:12px;border:1px solid var(--border-light);padding:16px">' +
        '<label style="display:flex;align-items:center;gap:10px;cursor:pointer;font-size:13px">' +
          '<input type="checkbox" id="policy-auto-moderate" '+(cfg.auto_approve_moderate?'checked':'')+' onchange="updatePolicyGlobal(\'auto_approve_moderate\',this.checked)" style="width:16px;height:16px;accent-color:var(--primary)">' +
          '<div><span style="font-weight:600">中风险自动通过</span><div style="font-size:11px;color:var(--text3)">开启后，中风险操作无需任何审批自动放行；关闭后，中风险需要有权限的 Agent 或管理员审批</div></div>' +
        '</label>' +
        '<label style="display:flex;align-items:center;gap:10px;font-size:13px">' +
          '<span style="font-weight:600;white-space:nowrap">审批超时</span>' +
          '<input type="number" id="policy-timeout" value="'+Math.round(cfg.approval_timeout)+'" min="30" max="3600" style="width:80px;background:var(--surface2);color:var(--text);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;font-size:13px" onchange="updatePolicyGlobal(\'approval_timeout\',Number(this.value))">' +
          '<span style="font-size:11px;color:var(--text3)">秒 (30-3600)</span>' +
        '</label>' +
      '</div>' +
    '</div>';

    // ── Role delegation graph (allowed_role_edges) ──
    var fp = cfg.fork_policy || {};
    var edges = fp.allowed_role_edges || {};
    var edgeRows = '';
    var roleKeys = Object.keys(edges);
    if (roleKeys.length === 0) {
      edgeRows = '<tr><td colspan="3" style="padding:14px;color:var(--text3);font-size:12px;text-align:center">未配置任何委派规则 — 当前所有角色都可以委派任意子角色（仍受 max_depth/concurrency/cost 限制）</td></tr>';
    } else {
      roleKeys.sort().forEach(function(pr) {
        var children = edges[pr] || [];
        var childStr = children.length ? children.join(', ') : '<span style="color:#ef4444">（不允许委派任何子角色）</span>';
        edgeRows += '<tr style="border-top:1px solid rgba(255,255,255,0.05)">' +
          '<td style="padding:8px 14px;font-family:monospace;font-weight:600">' + esc(pr) + '</td>' +
          '<td style="padding:8px 14px;font-size:12px;color:var(--text2)">' + childStr + '</td>' +
          '<td style="padding:8px 14px;text-align:right">' +
            '<button class="btn btn-ghost btn-sm" style="font-size:11px" onclick="editRoleEdge(\'' + esc(pr) + '\')">编辑</button>' +
            '<button class="btn btn-ghost btn-sm" style="font-size:11px;color:#ef4444" onclick="deleteRoleEdge(\'' + esc(pr) + '\')">删除</button>' +
          '</td></tr>';
      });
    }

    html += '<div style="margin-bottom:24px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
        '<div>' +
          '<div style="font-size:14px;font-weight:700">编排围栏 — 角色委派图 Role Delegation Graph</div>' +
          '<div style="font-size:11px;color:var(--text3);margin-top:2px">定义"父角色 → 允许的子角色"。Agent 仍然自主决定派谁，但只能在你允许的边内活动。空表 = 不限制。</div>' +
        '</div>' +
        '<button class="btn btn-ghost btn-sm" onclick="addRoleEdge()" style="font-size:11px;display:flex;align-items:center;gap:4px"><span class="material-symbols-outlined" style="font-size:14px">add</span>添加规则</button>' +
      '</div>' +
      '<div style="background:var(--surface);border-radius:12px;border:1px solid var(--border-light);overflow:hidden">' +
      '<table style="width:100%;border-collapse:collapse;font-size:13px">' +
        '<thead><tr style="background:var(--surface2)">' +
          '<th style="text-align:left;padding:10px 14px;font-weight:700;width:200px">父角色 Parent Role</th>' +
          '<th style="text-align:left;padding:10px 14px;font-weight:700">允许委派的子角色 Allowed Children</th>' +
          '<th style="width:140px"></th>' +
        '</tr></thead>' +
        '<tbody>' + edgeRows + '</tbody>' +
      '</table></div>' +
    '</div>';

    c.innerHTML = html;
  } catch(e) {
    c.innerHTML = '<div style="color:var(--error);padding:20px">Error loading policy: '+e.message+'</div>';
  }
}

// ── Role delegation graph helpers ──
async function addRoleEdge() {
  var pr = prompt('父角色名 (parent role)，例如: manager / writer / analyst');
  if (!pr || !pr.trim()) return;
  pr = pr.trim();
  var raw = prompt('该父角色允许委派的子角色，逗号分隔，例如: analyst, writer, crawler\n（输入空字符串 = 不允许委派任何子角色）', '');
  if (raw === null) return;
  var children = raw.split(',').map(function(s){return s.trim();}).filter(function(s){return s.length>0;});
  try {
    var cfg = await api('GET', '/api/portal/policy');
    var fp = cfg.fork_policy || {};
    var edges = fp.allowed_role_edges || {};
    edges[pr] = children;
    await api('POST', '/api/portal/policy', { fork_policy: { allowed_role_edges: edges } });
    var sc = document.getElementById('settings-content');
    if (sc) renderPolicyConfig(sc);
  } catch(e) { alert('Error: '+e.message); }
}

async function editRoleEdge(pr) {
  try {
    var cfg = await api('GET', '/api/portal/policy');
    var fp = cfg.fork_policy || {};
    var edges = fp.allowed_role_edges || {};
    var current = (edges[pr] || []).join(', ');
    var raw = prompt('父角色 "'+pr+'" 允许委派的子角色（逗号分隔，留空 = 不允许委派任何子角色）', current);
    if (raw === null) return;
    var children = raw.split(',').map(function(s){return s.trim();}).filter(function(s){return s.length>0;});
    edges[pr] = children;
    await api('POST', '/api/portal/policy', { fork_policy: { allowed_role_edges: edges } });
    var sc = document.getElementById('settings-content');
    if (sc) renderPolicyConfig(sc);
  } catch(e) { alert('Error: '+e.message); }
}

async function deleteRoleEdge(pr) {
  if (!confirm('删除父角色 "'+pr+'" 的所有委派规则？删除后该角色将不再受 role-edge 限制（但仍受其他 fork_policy 限制）。')) return;
  try {
    var cfg = await api('GET', '/api/portal/policy');
    var fp = cfg.fork_policy || {};
    var edges = fp.allowed_role_edges || {};
    delete edges[pr];
    await api('POST', '/api/portal/policy', { fork_policy: { allowed_role_edges: edges } });
    var sc = document.getElementById('settings-content');
    if (sc) renderPolicyConfig(sc);
  } catch(e) { alert('Error: '+e.message); }
}

// ── Policy update helpers ──
async function updateToolRisk(tool, risk) {
  try {
    await api('POST', '/api/portal/policy', { tool_risks: { [tool]: risk } });
    // Refresh
    var sc = document.getElementById('settings-content');
    if (sc) renderPolicyConfig(sc);
  } catch(e) { alert('Error: '+e.message); }
}

async function updatePriorityAuth(priority, risk, checked) {
  try {
    var cfg = await api('GET', '/api/portal/policy');
    var priAuth = cfg.priority_approval_authority || {};
    var current = priAuth[String(priority)] || [];
    if (checked && current.indexOf(risk) < 0) {
      current.push(risk);
    } else if (!checked) {
      current = current.filter(function(r){ return r !== risk; });
    }
    var update = {};
    update[String(priority)] = current;
    await api('POST', '/api/portal/policy', { priority_approval_authority: update });
  } catch(e) { alert('Error: '+e.message); }
}

async function updatePolicyGlobal(key, value) {
  try {
    var body = {};
    body[key] = value;
    await api('POST', '/api/portal/policy', body);
  } catch(e) { alert('Error: '+e.message); }
}

function showAddToolRiskPrompt() {
  var toolName = prompt('Enter custom tool name (e.g. my_custom_tool):');
  if (!toolName || !toolName.trim()) return;
  toolName = toolName.trim().toLowerCase().replace(/[^a-z0-9_]/g, '_');
  var risk = prompt('Risk level for "' + toolName + '":\\n  red = 红线(deny)\\n  high = 高风险(admin approval)\\n  moderate = 中风险(agent/auto)\\n  low = 低风险(auto)', 'moderate');
  if (!risk || ['red','high','moderate','low'].indexOf(risk) < 0) {
    alert('Invalid risk level. Use: red, high, moderate, low');
    return;
  }
  updateToolRisk(toolName, risk);
}

// ============ Admin Management Page ============
async function renderAdminManage() {
  var c = document.getElementById('content');
  if (!_adminCtx.is_super) {
    c.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)"><span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:12px">lock</span>Only Super Admin can access this page</div>';
    return;
  }

  c.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text3)">Loading...</div>';

  try {
    var data = await api('POST', '/api/portal/admins/list', {});
    var admins = (data && data.admins) ? data.admins : [];

    var html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">';
    html += '<div><h3 style="font-size:16px;font-weight:700;font-family:\'Plus Jakarta Sans\',sans-serif">Admin Accounts</h3>';
    html += '<p style="font-size:12px;color:var(--text3);margin-top:4px">Manage admin users and their agent access</p></div>';
    html += '<button class="btn btn-primary btn-sm" onclick="showCreateAdminModal()"><span class="material-symbols-outlined" style="font-size:16px">person_add</span> Add Admin</button>';
    html += '</div>';

    html += '<div style="display:grid;gap:12px">';
    admins.forEach(function(a) {
      var isSuper = a.role === 'superAdmin';
      var roleBadge = isSuper
        ? '<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;background:rgba(203,201,255,0.15);color:var(--primary)">Super Admin</span>'
        : '<span style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;background:rgba(63,185,80,0.15);color:var(--success)">Admin</span>';
      var statusDot = a.active !== false
        ? '<span style="width:8px;height:8px;border-radius:50%;background:#3fb950;display:inline-block"></span>'
        : '<span style="width:8px;height:8px;border-radius:50%;background:var(--text3);display:inline-block"></span>';

      var agentTags = '';
      if (isSuper) {
        agentTags = '<span style="font-size:11px;color:var(--text3)">All agents</span>';
      } else if (a.agent_ids && a.agent_ids.length > 0) {
        agentTags = a.agent_ids.map(function(aid) {
          var ag = agents.find(function(x){return x.id===aid;});
          var name = ag ? ag.name : aid.substring(0,8);
          return '<span style="padding:2px 6px;background:var(--surface3);border-radius:4px;font-size:10px;color:var(--text2)">'+esc(name)+'</span>';
        }).join(' ');
      } else {
        agentTags = '<span style="font-size:11px;color:var(--text3)">No agents assigned</span>';
      }

      html += '<div style="background:var(--surface);border-radius:10px;padding:16px;border:1px solid var(--border-light);display:flex;align-items:center;gap:14px">';
      html += '<div style="width:40px;height:40px;border-radius:10px;background:var(--surface3);display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="material-symbols-outlined" style="font-size:22px;color:'+(isSuper?'var(--primary)':'var(--success)')+'">'+( isSuper ? 'admin_panel_settings' : 'person')+'</span></div>';
      html += '<div style="flex:1;min-width:0">';
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'+statusDot+' <span style="font-weight:700;font-size:14px">'+esc(a.display_name || a.username)+'</span> '+roleBadge+'</div>';
      html += '<div style="font-size:11px;color:var(--text3);margin-bottom:6px">@'+esc(a.username)+'</div>';
      html += '<div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center"><span class="material-symbols-outlined" style="font-size:14px;color:var(--text3)">smart_toy</span> '+agentTags+'</div>';
      html += '</div>';
      if (!isSuper) {
        html += '<div style="display:flex;gap:6px;flex-shrink:0">';
        html += '<button class="btn btn-ghost btn-sm" onclick="showEditAdminModal(\''+a.user_id+'\')"><span class="material-symbols-outlined" style="font-size:16px">edit</span></button>';
        html += '<button class="btn btn-ghost btn-sm" style="color:var(--error)" onclick="deleteAdminUser(\''+a.user_id+'\',\''+esc(a.username)+'\')"><span class="material-symbols-outlined" style="font-size:16px">delete</span></button>';
        html += '</div>';
      }
      html += '</div>';
    });
    html += '</div>';

    c.innerHTML = html;
  } catch(e) {
    c.innerHTML = '<div style="color:var(--error);padding:20px">Failed to load admins: '+e.message+'</div>';
  }
}

function _buildAgentCheckboxes(cssClass, checkedIds) {
  // Group agents by node
  var byNode = {};
  agents.forEach(function(a) {
    var nk = a.node_name || a.node_id || 'local';
    if (!byNode[nk]) byNode[nk] = [];
    byNode[nk].push(a);
  });
  var checked = checkedIds || [];
  var html = '';
  var nodeKeys = Object.keys(byNode);
  nodeKeys.forEach(function(nk) {
    if (nodeKeys.length > 1) {
      html += '<div style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.5px;padding:6px 0 2px;border-bottom:1px solid var(--border-light);margin-bottom:4px">Node: '+esc(nk)+'</div>';
    }
    byNode[nk].forEach(function(a) {
      var isChecked = checked.indexOf(a.id) >= 0 ? 'checked' : '';
      html += '<label style="display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;border-radius:4px;transition:background 0.1s" onmouseenter="this.style.background=\'var(--surface2)\'" onmouseleave="this.style.background=\'none\'">' +
        '<input type="checkbox" value="'+a.id+'" class="'+cssClass+'" '+isChecked+' style="width:16px;height:16px;flex-shrink:0;accent-color:var(--primary)">' +
        '<span style="font-size:13px;color:var(--text)">'+esc(a.name)+'</span>' +
        '<span style="font-size:10px;color:var(--text3);margin-left:auto">('+esc(a.role||'general')+')</span>' +
      '</label>';
    });
  });
  return html || '<span style="color:var(--text3);font-size:12px">No agents available</span>';
}

function showCreateAdminModal() {
  var agentOpts = _buildAgentCheckboxes('new-admin-agent', []);

  var html = '<h3 style="margin-bottom:16px">Create Admin Account</h3>';
  html += '<div class="form-group"><label>Username</label><input id="new-admin-username" placeholder="e.g. zhangsan"></div>';
  html += '<div class="form-group"><label>Password</label><input id="new-admin-password" type="password" placeholder="At least 6 characters"></div>';
  html += '<div class="form-group"><label>Display Name</label><input id="new-admin-display" placeholder="e.g. 张三"></div>';
  html += '<div class="form-group"><label>Managed Agents</label><div style="max-height:240px;overflow-y:auto;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px">'+
    agentOpts +'</div></div>';
  html += '<div class="form-actions"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button>';
  html += '<button class="btn btn-primary" onclick="createAdminUser()">Create</button></div>';
  showCustomModal(html);
}

async function createAdminUser() {
  var username = document.getElementById('new-admin-username').value.trim();
  var password = document.getElementById('new-admin-password').value.trim();
  var display_name = document.getElementById('new-admin-display').value.trim();
  if (!username || !password) { alert('Username and password are required'); return; }
  if (password.length < 6) { alert('Password must be at least 6 characters'); return; }
  var checkedAgents = [];
  document.querySelectorAll('.new-admin-agent:checked').forEach(function(cb) { checkedAgents.push(cb.value); });

  var data = await api('POST', '/api/portal/admins/create', {
    username: username, password: password, display_name: display_name || username, agent_ids: checkedAgents
  });
  if (data && data.ok) {
    closeModal();
    renderAdminManage();
  } else {
    alert((data && data.error) || 'Failed to create admin');
  }
}

function showEditAdminModal(userId) {
  api('POST', '/api/portal/admins/list', {}).then(function(data) {
    var admins = (data && data.admins) || [];
    var admin = admins.find(function(a) { return a.user_id === userId; });
    if (!admin) { alert('Admin not found'); return; }

    var agentOpts = _buildAgentCheckboxes('edit-admin-agent', admin.agent_ids || []);

    var html = '<h3 style="margin-bottom:16px">Edit Admin: '+esc(admin.username)+'</h3>';
    html += '<input type="hidden" id="edit-admin-uid" value="'+admin.user_id+'">';
    html += '<div class="form-group"><label>Display Name</label><input id="edit-admin-display" value="'+esc(admin.display_name||'')+'"></div>';
    html += '<div class="form-group"><label>New Password (leave empty to keep current)</label><input id="edit-admin-password" type="password" placeholder="Leave empty to keep"></div>';
    html += '<div class="form-group"><label>Managed Agents</label><div style="max-height:240px;overflow-y:auto;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px">'+
      agentOpts +'</div></div>';
    html += '<div class="form-actions"><button class="btn btn-ghost" onclick="closeModal()">Cancel</button>';
    html += '<button class="btn btn-primary" onclick="saveAdminEdit()">Save</button></div>';
    showCustomModal(html);
  });
}

async function saveAdminEdit() {
  var uid = document.getElementById('edit-admin-uid').value;
  var display_name = document.getElementById('edit-admin-display').value.trim();
  var password = document.getElementById('edit-admin-password').value.trim();
  var checkedAgents = [];
  document.querySelectorAll('.edit-admin-agent:checked').forEach(function(cb) { checkedAgents.push(cb.value); });

  var body = { user_id: uid, display_name: display_name, agent_ids: checkedAgents };
  if (password) body.password = password;

  var data = await api('POST', '/api/portal/admins/update', body);
  if (data && data.ok) {
    closeModal();
    renderAdminManage();
  } else {
    alert((data && data.error) || 'Failed to update admin');
  }
}

async function deleteAdminUser(userId, username) {
  if (!confirm('Delete admin "'+username+'"? This cannot be undone.')) return;
  var data = await api('POST', '/api/portal/admins/delete', { user_id: userId });
  if (data && data.ok) {
    renderAdminManage();
  } else {
    alert((data && data.error) || 'Failed to delete admin');
  }
}

function showCustomModal(html) {
  // Reuse generic modal approach
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'custom-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) closeModal(); };
  var modal = document.createElement('div');
  modal.className = 'modal';
  modal.innerHTML = html;
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

function closeCustomModal() {
  var el = document.getElementById('custom-modal-overlay');
  if (el) el.remove();
}

// Override closeModal to also close custom modals
var _origCloseModal = typeof closeModal === 'function' ? closeModal : function(){};
closeModal = function(id) {
  closeCustomModal();
  try { _origCloseModal(id); } catch(e){}
};
// ============ Pending Skills / SkillForge Drafts ============
var _pendingSkillsState = { status: '', drafts: [] };

async function renderPendingSkills() {
  var c = document.getElementById('content');
  c.innerHTML = ''
    + '<div style="padding:18px">'
    + '  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;gap:12px">'
    + '    <div><h2 style="margin:0">技能锻造 / SkillForge Drafts</h2>'
    + '      <div style="font-size:12px;color:var(--text3);margin-top:4px">Agent 从经验库中提炼出的技能草稿，等待管理员审核后导入技能商店。</div>'
    + '    </div>'
    + '    <div style="display:flex;gap:8px">'
    + '      <button class="btn btn-sm" onclick="importSkillFromWorkspace()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">upload_file</span> 从 Agent 工作区导入</button>'
    + '      <button class="btn btn-sm" onclick="loadPendingSkills()"><span class="material-symbols-outlined" style="font-size:16px;vertical-align:middle">refresh</span> 刷新</button>'
    + '    </div>'
    + '  </div>'
    + '  <div style="display:flex;gap:10px;margin-bottom:12px">'
    + '    <select id="pending-status-filter" onchange="_pendingSkillsState.status=this.value;loadPendingSkills()" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)">'
    + '      <option value="">全部状态</option>'
    + '      <option value="draft">draft 草稿</option>'
    + '      <option value="exported">exported 已导出</option>'
    + '      <option value="approved">approved 已批准</option>'
    + '      <option value="rejected">rejected 已拒绝</option>'
    + '    </select>'
    + '  </div>'
    + '  <div id="pending-list" style="color:var(--text3)">加载中…</div>'
    + '</div>';
  loadPendingSkills();
}

async function loadPendingSkills() {
  var box = document.getElementById('pending-list');
  if (!box) return;
  var params = [];
  if (_pendingSkillsState.status) params.push('status=' + encodeURIComponent(_pendingSkillsState.status));
  var qs = params.length ? ('?' + params.join('&')) : '';
  try {
    var data = await api('GET', '/api/portal/pending-skills' + qs);
    _pendingSkillsState.drafts = data.drafts || [];
    if (!_pendingSkillsState.drafts.length) {
      box.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text3)">'
        + '<span class="material-symbols-outlined" style="font-size:48px;display:block;margin-bottom:8px;opacity:0.3">auto_fix_high</span>'
        + '暂无技能草稿。Agent 积累足够经验后会通过 propose_skill 工具自动生成。</div>';
      return;
    }
    box.innerHTML = _pendingSkillsState.drafts.map(_renderDraftCard).join('');
  } catch (e) {
    box.innerHTML = '<div style="color:var(--error);padding:20px">加载失败: ' + esc(e.message || String(e)) + '</div>';
  }
}

function _renderDraftCard(d) {
  var statusColors = { draft: '#60a5fa', exported: '#a78bfa', approved: '#10b981', rejected: '#ef4444' };
  var statusColor = statusColors[d.status] || '#94a3b8';
  var runtimeBadge = d.runtime === 'python'
    ? '<span style="padding:2px 6px;font-size:10px;background:rgba(59,130,246,0.15);color:#3b82f6;border-radius:10px">Python</span>'
    : '<span style="padding:2px 6px;font-size:10px;background:rgba(16,185,129,0.15);color:#10b981;border-radius:10px">Markdown</span>';
  var confPct = Math.round((d.confidence || 0) * 100);
  var confColor = confPct >= 80 ? '#10b981' : confPct >= 60 ? '#f59e0b' : '#ef4444';
  var codeCount = d.code_files ? Object.keys(d.code_files).length : 0;
  var dateStr = d.created_at ? new Date(d.created_at * 1000).toLocaleString() : '';
  var actions = '';
  if (d.status === 'draft' || d.status === 'exported') {
    actions = '<button class="btn btn-sm" style="background:var(--primary);color:#fff" onclick="approveDraft(\'' + esc(d.id) + '\',\'' + esc(d.name) + '\')">批准</button>'
      + '<button class="btn btn-sm" style="color:var(--error)" onclick="rejectDraft(\'' + esc(d.id) + '\',\'' + esc(d.name) + '\')">拒绝</button>';
  }
  return '<div style="border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px;background:var(--surface)">'
    + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
    + '  <div style="display:flex;align-items:center;gap:8px">'
    + '    <span style="font-weight:600;font-size:15px">' + esc(d.name) + '</span>'
    + '    <span style="padding:2px 8px;font-size:10px;border-radius:10px;background:' + statusColor + '22;color:' + statusColor + '">' + esc(d.status) + '</span>'
    + '    ' + runtimeBadge
    + '  </div>'
    + '  <span style="font-size:11px;color:var(--text3)">' + esc(d.id) + '</span>'
    + '</div>'
    + '<div style="font-size:13px;color:var(--text2);margin-bottom:8px">' + esc(d.description || '') + '</div>'
    + '<div style="display:flex;gap:16px;font-size:12px;color:var(--text3);margin-bottom:10px">'
    + '  <span>置信度: <span style="color:' + confColor + ';font-weight:600">' + confPct + '%</span></span>'
    + '  <span>来源: ' + (d.id && d.id.indexOf('-SUB-') > -1 ? 'Agent 提交' : d.id && d.id.indexOf('-IMP-') > -1 ? 'Agent 工作区导入' : (d.source_experiences && d.source_experiences.length ? d.source_experiences.length + ' 条经验' : '经验提炼')) + '</span>'
    + '  <span>角色: ' + esc(d.role || 'general') + '</span>'
    + (codeCount ? '  <span>代码文件: ' + codeCount + ' 个</span>' : '')
    + '  <span>' + esc(dateStr) + '</span>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center">'
    + '  <button class="btn btn-sm" onclick="showDraftDetail(\'' + esc(d.id) + '\')"><span class="material-symbols-outlined" style="font-size:14px;vertical-align:middle">visibility</span> 查看详情</button>'
    + actions
    + '</div></div>';
}

async function showDraftDetail(draftId) {
  try {
    var d = await api('GET', '/api/portal/pending-skills/' + encodeURIComponent(draftId));
    var s = [];
    s.push('<h3 style="margin:0 0 12px 0">' + esc(d.name) + ' <span style="font-size:12px;color:var(--text3)">' + esc(d.id) + '</span></h3>');
    s.push('<div style="font-size:13px;color:var(--text2);margin-bottom:12px">' + esc(d.description || '') + '</div>');
    s.push('<details open style="margin-bottom:12px"><summary style="cursor:pointer;font-weight:600;font-size:13px">manifest.yaml</summary>');
    s.push('<pre style="background:var(--bg);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto;max-height:300px;border:1px solid var(--border)">' + esc(d.manifest_yaml || '') + '</pre></details>');
    s.push('<details style="margin-bottom:12px"><summary style="cursor:pointer;font-weight:600;font-size:13px">SKILL.md</summary>');
    s.push('<pre style="background:var(--bg);padding:10px;border-radius:6px;font-size:12px;overflow-x:auto;max-height:400px;border:1px solid var(--border)">' + esc(d.skill_md || '') + '</pre></details>');
    var cf = d.code_files || {};
    Object.keys(cf).forEach(function(fn) {
      s.push('<details style="margin-bottom:12px"><summary style="cursor:pointer;font-weight:600;font-size:13px">' + esc(fn) + '</summary>');
      s.push('<pre style="background:#1e293b;color:#e2e8f0;padding:10px;border-radius:6px;font-size:12px;overflow-x:auto;max-height:400px;border:1px solid var(--border)">' + esc(cf[fn]) + '</pre></details>');
    });
    s.push('<div style="font-size:11px;color:var(--text3);margin-top:8px;border-top:1px solid var(--border);padding-top:8px">'
      + '置信度: ' + Math.round((d.confidence||0)*100) + '% · Runtime: ' + esc(d.runtime||'markdown')
      + ' · 来源: ' + (d.id && d.id.indexOf('-SUB-') > -1 ? 'Agent 提交' : d.id && d.id.indexOf('-IMP-') > -1 ? 'Agent 工作区导入' : (d.source_experiences && d.source_experiences.length ? d.source_experiences.length + ' 条经验' : '经验提炼')) + ' · 触发词: ' + esc((d.triggers||[]).join(', ')) + '</div>');
    if (d.status === 'draft' || d.status === 'exported') {
      s.push('<div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">'
        + '<button class="btn btn-sm" style="background:var(--primary);color:#fff" onclick="closeModal();approveDraft(\'' + esc(d.id) + '\',\'' + esc(d.name) + '\')">批准并导入</button>'
        + '<button class="btn btn-sm" style="color:var(--error)" onclick="closeModal();rejectDraft(\'' + esc(d.id) + '\',\'' + esc(d.name) + '\')">拒绝</button></div>');
    }
    showModalHTML('<div style="max-width:700px;max-height:80vh;overflow-y:auto;padding:4px">' + s.join('') + '</div>');
  } catch (e) { alert('加载详情失败: ' + (e.message || String(e))); }
}

async function approveDraft(draftId, draftName) {
  try {
    var res = await api('POST', '/api/portal/pending-skills/' + encodeURIComponent(draftId) + '/approve', {});
    if (res.ok) {
      window._toast('✓ 已批准: ' + draftName + (res['import']&&res['import'].imported ? '，已自动导入技能商店' : ''), 'ok');
    } else {
      window._toast('批准失败: ' + JSON.stringify(res), 'err');
    }
  } catch (e) {
    window._toast('操作失败: ' + (e.message || String(e)), 'err');
  }
  loadPendingSkills();
}

async function rejectDraft(draftId, draftName) {
  try {
    var res = await api('POST', '/api/portal/pending-skills/' + encodeURIComponent(draftId) + '/reject', {});
    if (res.ok) {
      window._toast('已拒绝: ' + draftName, 'ok');
    } else {
      window._toast('操作失败: ' + JSON.stringify(res), 'err');
    }
  } catch (e) {
    window._toast('操作失败: ' + (e.message || String(e)), 'err');
  }
  loadPendingSkills();
}

async function importSkillFromWorkspace() {
  // Show a dialog to select agent + directory name
  var visibleAgents = (agents||[]).filter(function(a){return !a.parent_id;});
  var agentOpts = visibleAgents.map(function(a){
    return '<option value="'+esc(a.id)+'">'+esc(a.name)+' ('+esc(a.id.slice(0,8))+')</option>';
  }).join('');
  var html = '<div style="max-width:500px">'
    + '<h3 style="margin:0 0 12px">从 Agent 工作区导入技能包</h3>'
    + '<div style="font-size:12px;color:var(--text3);margin-bottom:14px">选择创建了技能的 Agent，并输入技能目录名（如 pptx_skill）</div>'
    + '<div style="margin-bottom:10px"><label style="font-size:12px;font-weight:600">Agent</label>'
    + '<select id="import-agent-id" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);margin-top:4px">'
    + agentOpts + '</select></div>'
    + '<div style="margin-bottom:14px"><label style="font-size:12px;font-weight:600">技能目录名</label>'
    + '<input id="import-dir-name" placeholder="pptx_skill" style="width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);margin-top:4px;box-sizing:border-box" /></div>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end">'
    + '<button class="btn btn-sm" onclick="closeCustomModal()">取消</button>'
    + '<button class="btn btn-sm" style="background:var(--primary);color:#fff" onclick="_doImportSkill()">导入</button>'
    + '</div></div>';
  showModalHTML(html);
}

async function _doImportSkill() {
  var agentId = document.getElementById('import-agent-id').value;
  var dirName = (document.getElementById('import-dir-name').value||'').trim();
  if (!agentId || !dirName) { alert('请选择 Agent 并输入目录名'); return; }
  closeModal();
  try {
    var res = await api('POST', '/api/portal/pending-skills/import', {
      agent_id: agentId, dir_name: dirName
    });
    if (res && res.ok) {
      alert('导入成功: ' + res.name + ' (' + res.draft_id + ')\n代码文件: ' + (res.code_files||[]).join(', '));
      loadPendingSkills();
    } else {
      alert('导入失败: ' + JSON.stringify(res));
    }
  } catch(e) { alert('导入失败: ' + (e.message || String(e))); }
}

// ============ Init ============
refresh();
setInterval(refreshSidebar, 15000);
