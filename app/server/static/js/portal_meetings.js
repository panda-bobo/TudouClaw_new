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
