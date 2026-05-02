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
      api('GET', '/api/portal/canvas-workflows').catch(function(){ return {workflows: []}; }),
    ]);
    var jobsResp = results[0] || {};
    var stResp = results[1] || {};
    var agentsResp = results[2] || {};
    var wfResp = results[3] || {};
    var canvasWfResp = results[4] || {};
    var jobs = jobsResp.jobs || [];
    var standaloneTasks = (stResp.tasks || []).map(function(t){ t._src='standalone'; return t; });
    var agentsList = agentsResp.agents || [];
    var wfList = wfResp.workflows || [];
    var canvasWfList = canvasWfResp.workflows || [];
    var agentNameById = {};
    agentsList.forEach(function(a){ agentNameById[a.id] = (a.role?a.role+'-':'')+a.name; });
    var wfNameById = {};
    wfList.forEach(function(w){ wfNameById[w.id||w.template_id] = w.name||'(unnamed)'; });
    var canvasWfNameById = {};
    canvasWfList.forEach(function(w){ canvasWfNameById[w.id] = w.name||'(unnamed)'; });

    // 3 categories of scheduled jobs:
    // - chat: target an agent directly
    // - workflow: legacy state-machine workflow (固定流程)
    // - canvas_workflow: dynamic DAG via canvas executor (动态编排)
    var chatJobs = [];
    var workflowJobs = [];
    var canvasWorkflowJobs = [];
    jobs.forEach(function(j){
      var tt = j.target_type || 'chat';
      if (tt === 'canvas_workflow' || j.canvas_workflow_id) canvasWorkflowJobs.push(j);
      else if (tt === 'workflow' || j.workflow_id) workflowJobs.push(j);
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
      if (kind === 'canvas_workflow') {
        var cwName = canvasWfNameById[job.canvas_workflow_id] || job.canvas_workflow_id || '(未指定)';
        targetLine =
          '<div style="font-size:11px;color:var(--text2);margin-top:4px">' +
            '🎨 Canvas: <strong>'+esc(cwName)+'</strong>' +
          '</div>' +
          '<div style="font-size:10px;color:var(--text3);margin-top:2px">动态编排 · agent 由画布节点定义</div>';
      } else if (kind === 'workflow') {
        var wfName = wfNameById[job.workflow_id] || job.workflow_id || '(未指定)';
        var defaultAgent = job.agent_id ? (agentNameById[job.agent_id] || job.agent_id) : '(全部默认)';
        targetLine =
          '<div style="font-size:11px;color:var(--text2);margin-top:4px">' +
            '🔀 Workflow: <strong>'+esc(wfName)+'</strong>' +
          '</div>' +
          '<div style="font-size:10px;color:var(--text3);margin-top:2px">固定流程 · 默认 Agent: '+esc(defaultAgent)+'</div>';
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
      || '<div style="color:var(--text3);font-size:12px;padding:12px;text-align:center">无固定流程 Workflow 定时任务</div>';
    var canvasJobsHtml = canvasWorkflowJobs.map(function(j){ return renderJobCard(j, 'canvas_workflow'); }).join('')
      || '<div style="color:var(--text3);font-size:12px;padding:12px;text-align:center">无动态编排定时任务<br><span style="font-size:10px">需要先到"编排"页创建 + 标记为可执行</span></div>';

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

    // 4 columns now (was 3): added 🎨 canvas workflow column.
    // Use auto-fit so on narrow viewports it wraps to 2x2 instead of
    // squishing each column to 25%.
    area.innerHTML =
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px">' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">⏱ Agent 定时 ('+chatJobs.length+')</div>'+chatJobsHtml+'</div>' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">🔀 Workflow 定时 · 固定流程 ('+workflowJobs.length+')</div>'+wfJobsHtml+'</div>' +
        '<div><div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text3);margin-bottom:10px">🎨 编排定时 · 动态画布 ('+canvasWorkflowJobs.length+')</div>'+canvasJobsHtml+'</div>' +
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
