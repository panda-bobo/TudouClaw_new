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

  // Upsert: only touch V1 rows. Leave V2 rows ([data-v2-row]) untouched
  // so loadV2TasksIntoQueue() doesn't flicker between "Queued" and
  // "Analyzing". Also preserve any empty-state placeholder so we don't
  // re-render when content didn't actually change.
  el.querySelectorAll('[data-v1-row]').forEach(function(n) { n.remove(); });
  el.querySelectorAll('[data-v1-empty]').forEach(function(n) { n.remove(); });
  if (tasks.length === 0) {
    // Only show "No tasks yet" if there are ALSO no V2 rows. Otherwise
    // the placeholder fights with the V2 separator for space.
    var hasV2 = el.querySelector('[data-v2-row]');
    if (!hasV2) {
      var ph = document.createElement('div');
      ph.setAttribute('data-v1-empty', '1');
      ph.style.cssText = 'color:var(--text3);padding:8px';
      ph.textContent = 'No tasks yet';
      el.insertBefore(ph, el.firstChild);
    }
    return;
  }
  const statusIcon = {todo:'⬜', in_progress:'🔄', done:'✅', blocked:'🚫', cancelled:'❌'};
  const priLabel = {0:'', 1:'🔶', 2:'🔴'};
  const sourceIcon = {admin:'👤', agent:'🤖', system:'⚙️', user:'👤'};
  const sourceColor = {admin:'var(--warning)', agent:'var(--primary)', system:'var(--text3)', user:'var(--success)'};
  var v1Html = tasks.map(t => {
    const dlTime = t.deadline ? new Date(t.deadline*1000) : null;
    const dlStr = dlTime ? dlTime.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
    const isOverdue = dlTime && dlTime < new Date() && t.status !== 'done' && t.status !== 'cancelled';
    const src = t.source || 'admin';
    const srcAgent = t.source_agent_id ? ` (${esc(t.source_agent_id.slice(0,8))})` : '';
    return `
    <div data-v1-row="1" data-task-id="${t.id}" style="padding:8px;border-bottom:1px solid var(--border);cursor:pointer${isOverdue?';background:rgba(255,80,80,0.08)':''}"
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
  // Insert V1 rows BEFORE any V2 rows — V1 Task Queue on top, 🚀 V2 below.
  var firstV2 = el.querySelector('[data-v2-row]');
  var tmp = document.createElement('div');
  tmp.innerHTML = v1Html;
  var frag = document.createDocumentFragment();
  while (tmp.firstChild) frag.appendChild(tmp.firstChild);
  if (firstV2) el.insertBefore(frag, firstV2);
  else el.appendChild(frag);
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
