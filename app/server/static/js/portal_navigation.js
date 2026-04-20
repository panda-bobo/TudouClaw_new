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
      // V1/V2 badge: start as "V1", flip to "V1+V2" if V2 shell exists.
      // esc() is defined in portal_bundle.js; titleEl uses innerHTML now
      // because we want the badge chip styled next to the name.
      var _nm = agentObj ? ((agentObj.role||'general') + '-' + agentObj.name) : 'Agent';
      var _safeNm = (typeof esc === 'function') ? esc(_nm) : _nm;
      titleEl.innerHTML = _safeNm +
        ' <span id="v1v2-badge" style="display:inline-flex;align-items:center;' +
        'font-size:10px;padding:2px 8px;border-radius:10px;margin-left:8px;' +
        'background:var(--surface2);color:var(--text3);font-weight:600;vertical-align:middle">' +
        'V1</span>';
      // Async probe — if V2 shell exists, upgrade the badge.
      try {
        fetch('/api/v2/agents/' + encodeURIComponent(currentAgent), {
          credentials: 'same-origin',
        }).then(function(r) {
          var badge = document.getElementById('v1v2-badge');
          if (!badge) return;
          if (r.ok) {
            badge.textContent = 'V1 + V2';
            badge.style.background = 'rgba(249,115,22,0.15)';
            badge.style.color = '#f97316';
            badge.title = '已启用状态机任务能力（6-phase 状态机）';
          } else {
            badge.title = '仅 V1 聊天；可在下方 状态机任务队列面板启用状态机任务 能力';
          }
        }).catch(function() { /* keep default V1 badge */ });
      } catch (_e) { /* silent */ }
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
