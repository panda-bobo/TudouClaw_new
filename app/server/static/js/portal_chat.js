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
    // Reconnect to active task stream AFTER history is loaded.
    // Must run after loadAgentChat completes — otherwise the
    // "thinking" bubble it adds causes loadAgentChat to skip
    // history rendering (hasMessages check sees the bubble).
    _reconnectActiveStream(agentId);
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
    // Thinking bubbles (from _reconnectActiveStream) don't count — they're
    // transient UI indicators, not persisted chat history.
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
  // The pattern works on HTML where newlines are already <br> tags
  return html.replace(/\[File:\s*([^\]]+)\](?:<br\s*\/?>)([\s\S]*?)(?=\[File:\s*[^\]]+\](?:<br)|$)/gi, function(m, fname, body) {
    if (!body || !body.trim()) {
      return '<strong>[File: ' + fname.trim() + ']</strong>';
    }
    var uid = 'fb-' + Math.random().toString(36).substr(2, 8);
    // Count <br> to determine if collapsible is needed (threshold: 8 lines)
    var brCount = (body.match(/<br\s*\/?>/gi) || []).length;
    if (brCount < 8 && body.length < 500) {
      // Short content — no need to collapse
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
  var siteName = esc(evt.site_name||'Website');
  var url = esc(evt.url||'');
  var loginUrl = esc(evt.login_url || evt.url || '');
  var reason = esc(evt.reason||'Agent needs authenticated access');
  // Unique ID for this card's iframe
  var iframeId = 'login-iframe-' + reqId.slice(0,8);
  div.innerHTML = '' +
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">' +
      '<span class="material-symbols-outlined" style="font-size:22px;color:#3b82f6">login</span>' +
      '<span style="font-weight:700;font-size:14px;color:#3b82f6">Login Required · 需要登录</span>' +
    '</div>' +
    '<div style="font-size:13px;margin-bottom:4px"><strong>' + siteName + '</strong></div>' +
    '<div style="font-size:11px;color:var(--text3);margin-bottom:8px">' + reason + '</div>' +
    // Three tabs: iframe login | credentials | cookie
    '<div style="display:flex;gap:0;margin-bottom:8px;border-bottom:1px solid var(--border)">' +
      '<button class="login-tab active" data-tab="iframe" onclick="_switchLoginTab(this,\'iframe\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text);cursor:pointer;border-bottom:2px solid #3b82f6;font-weight:600">🌐 网页登录</button>' +
      '<button class="login-tab" data-tab="cred" onclick="_switchLoginTab(this,\'cred\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent">🔑 账号密码</button>' +
      '<button class="login-tab" data-tab="cookie" onclick="_switchLoginTab(this,\'cookie\')" style="padding:6px 14px;font-size:11px;border:none;background:none;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent">🍪 Cookie / Token</button>' +
    '</div>' +
    // ── iframe panel (default) ──
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
    // ── Credentials panel ──
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
    // ── Cookie panel ──
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
    // ── Skip button (always visible) ──
    '<div style="margin-top:8px;text-align:right">' +
      '<button class="btn btn-sm" onclick="_skipLoginRequest(\'' + agentId + '\',this)" style="font-size:11px;padding:4px 12px;color:var(--text3)">跳过登录</button>' +
    '</div>';
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  // Detect iframe load failure (X-Frame-Options / CSP block)
  _detectIframeBlock(div, iframeId);
  return div;
}

function _detectIframeBlock(card, iframeId) {
  // Many sites block iframe embedding. We detect this by:
  // 1. iframe onerror (rare)
  // 2. Timeout: if iframe contentWindow is inaccessible after load, show fallback
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
  // After 4 seconds, check if we can access iframe content
  setTimeout(function() {
    try {
      // If cross-origin, accessing contentDocument throws
      var doc = iframe.contentDocument || iframe.contentWindow.document;
      // If doc is empty/blank after 4s, likely blocked
      if (!doc || !doc.body || doc.body.innerHTML === '') showBlocked();
    } catch(e) {
      // Cross-origin = loaded successfully (good), don't block
      // The site loaded but we can't read it due to same-origin policy,
      // which is expected — the user can still interact with the iframe
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
  // User clicked "已登录完成" — tell agent to capture session from its browser
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
      cookies: '__BROWSER_SESSION__',  // Signal: agent should read cookies from its own browser
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
  card.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:4px 0">' +
    '<span class="material-symbols-outlined" style="color:var(--text3)">skip_next</span>' +
    '<span style="color:var(--text3);font-size:13px">已跳过登录请求</span></div>';
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
      // API returned error — show inline
      if (card) {
        var errSpan = document.createElement('div');
        errSpan.style.cssText = 'font-size:11px;color:var(--warning);margin-top:4px;';
        errSpan.textContent = '⚠ ' + result.error;
        card.appendChild(errSpan);
      }
    }
    // Sync global approval panel so it reflects the decision
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
var _agentAttachments = {};  // { [agentId]: [{name, mime, size, data_base64, preview_url}] }

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
             '<button onclick="removeAgentAttach(\''+agentId+'\','+idx+')" title="Remove" style="background:none;border:none;color:var(--text3);cursor:pointer;padding:2px"><span class="material-symbols-outlined" style="font-size:14px">close</span></button>' +
           '</div>';
  }).join('');
}

function removeAgentAttach(agentId, idx) {
  var list = _agentAttachList(agentId);
  if (idx >= 0 && idx < list.length) {
    list.splice(idx, 1);
    _renderAgentAttachPreview(agentId);
  }
}

function handleAgentAttach(agentId, fileInput) {
  if (!fileInput || !fileInput.files || fileInput.files.length === 0) return;
  var list = _agentAttachList(agentId);
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
  // Clear attachments
  _agentAttachments[agentId] = [];
  _renderAgentAttachPreview(agentId);

  // Build display text with attachment indicators
  var displayText = text || '';
  if (attachments.length) {
    var attNames = attachments.map(function(a){ return '📎'+a.name; }).join(' ');
    displayText = displayText ? displayText + '\n' + attNames : attNames;
  }
  addChatBubble(agentId, 'user', displayText);

  // Show progress bar (robot avatar with status)
  const progressBar = _createProgressBar(agentId);
  // No separate thinkDiv — progress bar already shows "Thinking/Preparing" status
  const thinkDiv = document.createElement('div');
  thinkDiv.style.display = 'none';  // hidden placeholder, used only as reference in _streamTaskEvents

  try {
    // Step 1: Submit task with optional attachments (returns immediately)
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

    // Step 2: Stream task events via SSE long-poll
    await _streamTaskEvents(agentId, result.task_id, thinkDiv, progressBar);
    // Light refresh: update sidebar status without re-rendering chat content
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
