/**
 * portal_v2.js — V2 feature surface rendered inside V1 portal.
 *
 * Design rules:
 *   - Every function here is a plain V1-style render helper. It fills a
 *     DOM container that the caller provides; it does NOT own the
 *     sidebar, top bar, or the ``content`` root.
 *   - Styling uses the V1 CSS variables (``var(--primary)``,
 *     ``var(--surface)``, ``.btn``, ``.card``) — no independent CSS file.
 *   - ``isV2Mode()`` gates enhancements; when off, V1 render functions
 *     short-circuit and skip the V2 branches entirely.
 *
 * Exposed globals (used by portal_navigation.js / portal_bundle.js):
 *   toggleV2Mode(on)                  — flip the mode and reload
 *   isV2Mode()                        — read the mode flag
 *   renderV2TasksSubTab(containerEl, agentId?)
 *   renderV2AgentQueueTab(containerEl, agentId)
 *   renderV2TemplatesSubTab(containerEl)
 *   renderV2TierBindingsSubTab(containerEl)
 *   v2EnhanceAgentCreateForm(containerEl)
 */

(function() {
  "use strict";

  // ── mode flag ──────────────────────────────────────────────────────

  function isV2Mode() {
    try { return localStorage.getItem('tudou_mode') === 'v2'; }
    catch(_e) { return false; }
  }

  function toggleV2Mode(on) {
    try {
      if (on) localStorage.setItem('tudou_mode', 'v2');
      else localStorage.removeItem('tudou_mode');
    } catch(_e) {}
    location.reload();
  }

  // Apply the current flag to the sidebar toggle on boot.
  function _syncModeToggle() {
    var cb = document.getElementById('mode-toggle');
    if (cb) cb.checked = isV2Mode();
    var lbl = document.getElementById('mode-toggle-label');
    if (lbl) {
      if (isV2Mode()) {
        lbl.style.background = 'rgba(249,115,22,0.1)';
        lbl.style.borderColor = 'rgba(249,115,22,0.5)';
      }
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _syncModeToggle);
  } else {
    _syncModeToggle();
  }

  // ── small utilities ────────────────────────────────────────────────

  function _esc(s) {
    return String(s == null ? '' : s)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  }

  function _age(ts) {
    var d = Math.max(0, Date.now() / 1000 - Number(ts || 0));
    if (d < 60) return Math.floor(d) + 's';
    if (d < 3600) return Math.floor(d / 60) + 'm';
    if (d < 86400) return Math.floor(d / 3600) + 'h';
    return Math.floor(d / 86400) + 'd';
  }

  function _statusChip(status) {
    var color = ({
      running: '#22c55e', queued: 'var(--text3)',
      succeeded: '#22c55e', failed: '#ef4444',
      paused: '#f59e0b', abandoned: '#6b7280',
    })[status] || 'var(--text3)';
    var label = ({
      running: '运行中', queued: '排队中',
      succeeded: '已完成', failed: '失败',
      paused: '已暂停', abandoned: '已取消',
    })[status] || status;
    return '<span style="font-size:10px;padding:2px 8px;border-radius:10px;' +
      'background:rgba(255,255,255,0.08);color:' + color + ';font-weight:600">' +
      _esc(label) + '</span>';
  }

  // Shared toast fallback; reuses V1's if defined.
  function _toast(msg, kind) {
    if (typeof window.toast === 'function') { window.toast(msg); return; }
    var el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = 'position:fixed;top:60px;right:20px;padding:10px 16px;' +
      'background:' + (kind === 'error' ? '#ef4444' : '#1e293b') +
      ';color:#fff;border-radius:8px;font-size:12px;z-index:10000;box-shadow:0 4px 12px rgba(0,0,0,0.2)';
    document.body.appendChild(el);
    setTimeout(function() { el.remove(); }, 3000);
  }

  // REST helper — uses V1's auth headers (cookie / JWT in localStorage).
  async function _v2api(method, path, body) {
    var headers = { 'Content-Type': 'application/json' };
    var tok = localStorage.getItem('jwt_token') || localStorage.getItem('access_token');
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    var opts = { method: method, headers: headers, credentials: 'same-origin' };
    if (body !== undefined) opts.body = JSON.stringify(body);
    var resp = await fetch(path, opts);
    var data = await resp.json().catch(function(){ return {}; });
    if (!resp.ok) {
      var msg = (data && data.detail && data.detail.error) || data.detail || resp.statusText;
      throw new Error(msg);
    }
    return data;
  }

  // ── renderV2TasksSubTab ────────────────────────────────────────────
  //
  // Renders the "状态机任务" sub-tab content into ``containerEl``. When
  // ``agentId`` is provided, the list is filtered to that agent.

  async function renderV2TasksSubTab(containerEl, agentId) {
    if (!containerEl) return;
    containerEl.innerHTML = '<div style="color:var(--text3);padding:24px">Loading V2 tasks...</div>';
    try {
      var qs = '?limit=200' + (agentId ? '&agent_id=' + encodeURIComponent(agentId) : '');
      var r = await _v2api('GET', '/api/v2/tasks' + qs);
      var tasks = r.tasks || [];
      var groups = { running: [], queued: [], paused: [], done: [] };
      tasks.forEach(function(t) {
        var s = t.status;
        if (s === 'running') groups.running.push(t);
        else if (s === 'queued') groups.queued.push(t);
        else if (s === 'paused') groups.paused.push(t);
        else groups.done.push(t);
      });

      var header = '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px">' +
        '<div><h3 style="margin:0;font-size:15px">状态机任务</h3>' +
        '<p style="font-size:11px;color:var(--text3);margin-top:4px">' +
          (agentId ? '当前 agent 的 状态机任务' : '所有 状态机任务') + ' · 6-phase 状态机 · FIFO 队列' +
        '</p></div>' +
        '<div style="display:flex;gap:8px">' +
          '<button class="btn btn-primary btn-sm" onclick="v2ShowSubmitTaskModal(\'' + _esc(agentId || '') + '\')">' +
            '<span class="material-symbols-outlined" style="font-size:16px">add</span> 提交任务' +
          '</button>' +
        '</div>' +
      '</div>';

      var section = function(title, items, colorDot) {
        if (!items.length) return '';
        return '<div class="card" style="margin-bottom:14px"><div class="card-header">' +
          '<span style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text2)">' +
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + colorDot + ';margin-right:6px"></span>' +
            _esc(title) + ' <span style="color:var(--text3);font-weight:500">(' + items.length + ')</span>' +
          '</span>' +
        '</div>' +
        items.map(function(t) {
          return '<div onclick="v2OpenTaskDetail(\'' + _esc(t.id) + '\')" ' +
            'style="padding:10px 12px;border-top:1px solid var(--border);cursor:pointer;transition:background 0.15s" ' +
            'onmouseover="this.style.background=\'var(--surface2)\'" ' +
            'onmouseout="this.style.background=\'transparent\'">' +
            '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">' +
              '<div style="flex:1;min-width:0">' +
                '<div style="font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
                  _esc(t.intent || '(no intent)') + '</div>' +
                '<div style="font-size:10px;color:var(--text3);margin-top:3px">' +
                  _esc(t.phase) + ' · ' + _age(t.updated_at || t.created_at) + ' · agent ' + _esc(t.agent_id.slice(0, 8)) +
                '</div>' +
              '</div>' +
              '<div style="display:flex;gap:6px;align-items:center">' +
                _statusChip(t.status) +
                (t.status === 'running' || t.status === 'paused' || t.status === 'queued' ?
                  '<button onclick="event.stopPropagation();v2CancelTask(\'' + _esc(t.id) + '\')" ' +
                  'class="btn btn-ghost btn-sm" style="padding:2px 8px;color:#ef4444" title="取消">' +
                  '<span class="material-symbols-outlined" style="font-size:14px">close</span></button>'
                  : '') +
              '</div>' +
            '</div>' +
          '</div>';
        }).join('') +
        '</div>';
      };

      var body = section('运行中', groups.running, '#22c55e') +
                 section('排队中', groups.queued, 'var(--primary)') +
                 section('已暂停', groups.paused, '#f59e0b') +
                 section('已完成', groups.done.slice(0, 30), 'var(--text3)');
      if (!body) body = '<div class="card" style="text-align:center;padding:40px;color:var(--text3);font-size:13px">' +
        '暂无 状态机任务。点击右上角"提交任务"创建第一个。</div>';

      containerEl.innerHTML = header + body;
    } catch (e) {
      containerEl.innerHTML = '<div style="color:#ef4444;padding:24px">加载状态机任务失败：' + _esc(e.message) + '</div>';
    }
  }

  // ── Submit Task Modal ──────────────────────────────────────────────

  async function v2ShowSubmitTaskModal(defaultAgentId) {
    var agents = [], templates = [];
    try {
      var ar = await _v2api('GET', '/api/v2/agents');
      agents = ar.agents || [];
      var tr = await _v2api('GET', '/api/v2/templates');
      templates = tr.templates || [];
    } catch (e) {
      _toast('加载失败：' + e.message, 'error');
      return;
    }

    var agentOpts = agents.map(function(a) {
      return '<option value="' + _esc(a.id) + '"' + (a.id === defaultAgentId ? ' selected' : '') + '>' +
        _esc(a.name) + ' · ' + _esc(a.role) + '</option>';
    }).join('');
    if (!agents.length) {
      agentOpts = '<option value="">(无 状态机 agent — 请先在 状态机任务管理下创建)</option>';
    }
    var tmplOpts = '<option value="">(让系统选默认)</option>' +
      templates.map(function(t) {
        return '<option value="' + _esc(t.id) + '">' + _esc(t.display_name || t.id) + '</option>';
      }).join('');

    var html = '<div style="padding:24px;max-width:520px"><h3 style="margin:0 0 16px">提交状态机任务</h3>' +
      '<div class="form-group"><label>Agent</label>' +
        '<select id="v2-submit-agent" style="width:100%;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">' +
        agentOpts + '</select></div>' +
      '<div class="form-group"><label>任务模板</label>' +
        '<select id="v2-submit-tmpl" style="width:100%;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">' +
        tmplOpts + '</select></div>' +
      '<div class="form-group"><label>意图 / 任务描述</label>' +
        '<textarea id="v2-submit-intent" placeholder="描述你想完成的事情..." style="width:100%;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;min-height:80px;resize:vertical"></textarea>' +
      '</div>' +
      '<div style="display:flex;gap:8px;justify-content:flex-end">' +
        '<button class="btn btn-ghost" onclick="closeModal()">取消</button>' +
        '<button class="btn btn-primary" onclick="v2DoSubmitTask()">提交</button>' +
      '</div></div>';
    if (typeof window.showModalHTML === 'function') window.showModalHTML(html);
    else alert('V1 modal helper missing — please check portal_bundle.js');
  }

  async function v2DoSubmitTask() {
    var aid = document.getElementById('v2-submit-agent').value;
    var tmpl = document.getElementById('v2-submit-tmpl').value;
    var intent = document.getElementById('v2-submit-intent').value.trim();
    if (!aid) { _toast('请选择 agent', 'error'); return; }
    if (!intent) { _toast('意图不能为空', 'error'); return; }
    try {
      var r = await _v2api('POST', '/api/v2/agents/' + aid + '/tasks',
        { intent: intent, template_id: tmpl });
      _toast(r.task.status === 'queued' ? '已排队' : '任务已提交');
      if (typeof window.closeModal === 'function') window.closeModal();
      // Refresh current view if it's showing V2 tasks.
      if (typeof window.renderCurrentView === 'function') {
        window.renderCurrentView();
      }
    } catch (e) {
      _toast('提交失败：' + e.message, 'error');
    }
  }

  async function v2CancelTask(taskId) {
    if (!confirm('终止该任务？运行中的步骤会立即停止。')) return;
    try {
      await _v2api('POST', '/api/v2/tasks/' + taskId + '/cancel');
      _toast('已终止');
      if (typeof window.renderCurrentView === 'function') window.renderCurrentView();
    } catch (e) {
      _toast('终止失败：' + e.message, 'error');
    }
  }

  async function v2ResumeTask(taskId) {
    try {
      await _v2api('POST', '/api/v2/tasks/' + taskId + '/resume');
      _toast('已继续');
      if (typeof window.renderCurrentView === 'function') window.renderCurrentView();
    } catch (e) {
      _toast('继续失败：' + e.message, 'error');
    }
  }

  async function v2PauseTask(taskId) {
    try {
      await _v2api('POST', '/api/v2/tasks/' + taskId + '/pause');
      _toast('已暂停');
      if (typeof window.renderCurrentView === 'function') window.renderCurrentView();
    } catch (e) {
      _toast('暂停失败：' + e.message, 'error');
    }
  }

  async function v2DeleteTask(taskId) {
    if (!confirm('删除该任务？事件日志一同清除，不可恢复。')) return;
    try {
      await _v2api('DELETE', '/api/v2/tasks/' + taskId);
      _toast('已删除');
      if (typeof window.renderCurrentView === 'function') window.renderCurrentView();
    } catch (e) {
      // If it's not terminal, offer to cancel-then-delete in one step.
      if (/INVALID_STATE_TRANSITION|409/.test(e.message)) {
        if (confirm('任务仍在运行/暂停。先终止再删除？')) {
          try {
            await _v2api('POST', '/api/v2/tasks/' + taskId + '/cancel');
            await _v2api('DELETE', '/api/v2/tasks/' + taskId);
            _toast('已终止并删除');
            if (typeof window.renderCurrentView === 'function') window.renderCurrentView();
            return;
          } catch (e2) {
            _toast('删除失败：' + e2.message, 'error');
            return;
          }
        }
      }
      _toast('删除失败：' + e.message, 'error');
    }
  }

  // ── V2 Task Detail Modal — simplified Timeline ─────────────────────

  async function v2OpenTaskDetail(taskId) {
    try {
      var r = await _v2api('GET', '/api/v2/tasks/' + taskId);
      var t = r.task;
      // Error events come back inline on the GET response (see
      // _task_to_dict → errors[]). Keeps us off the SSE /events
      // stream which can't be consumed by a plain fetch().json().
      var events = (r.errors || []).map(function(e) {
        return { type: 'phase_error', phase: e.phase, payload: e };
      });
      var phases = ['intake', 'plan', 'execute', 'verify', 'deliver', 'report'];
      var cur = phases.indexOf(t.phase);
      var phasesHtml = phases.map(function(p, i) {
        var done = (i < cur) || t.status === 'succeeded';
        var active = (i === cur && t.status === 'running');
        var failed = (i === cur && t.status === 'failed');
        var icon = failed ? '❌' : done ? '✅' : active ? '🔄' : '⬜';
        var color = failed ? '#ef4444' : done ? '#22c55e' : active ? 'var(--primary)' : 'var(--text3)';
        var bg = failed ? 'rgba(239,68,68,0.1)' : active ? 'rgba(203,201,255,0.08)' : 'transparent';
        return '<div style="display:flex;align-items:center;padding:6px 10px;margin-bottom:4px;background:' + bg +
          ';border-radius:6px;color:' + color + ';font-size:12px">' +
          '<span style="margin-right:8px">' + icon + '</span>' + _esc(p) +
          '</div>';
      }).join('');

      var arts = (t.artifacts || []).map(function(a) {
        return '<li style="padding:4px 0;border-bottom:1px dotted var(--border);font-size:11px">' +
          '[' + _esc(a.kind) + '] <b>' + _esc(a.handle || '-') + '</b> ' +
          '<span style="color:var(--text3)">' + _esc(a.summary || '') + '</span></li>';
      }).join('');

      var lessons = (t.lessons || []).map(function(le) {
        return '<li style="padding:4px 0;font-size:11px">[' + _esc(le.phase) + '] ' +
          _esc(le.issue) + ' → ' + _esc(le.fix || '-') + '</li>';
      }).join('');

      // Pull every phase_error event (most useful diagnostic when task
      // is failed). Render the error string + any raw_content hint.
      var errorEvents = (events || []).filter(function(e){
        return e.type === 'phase_error';
      });
      var errorsHtml = '';
      if (errorEvents.length) {
        errorsHtml = '<div style="margin-top:16px;padding:12px;' +
          'background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.3);' +
          'border-radius:6px">' +
          '<h4 style="margin:0 0 8px;font-size:11px;color:#ef4444;' +
          'text-transform:uppercase">❌ Errors (' + errorEvents.length + ')</h4>' +
          errorEvents.map(function(e) {
            var p = e.payload || {};
            var block = '<div style="margin-bottom:8px;padding:6px 0;' +
              'border-bottom:1px dotted rgba(239,68,68,0.2);font-size:11px">' +
              '<div style="color:var(--text2)"><b>[' + _esc(p.phase || e.phase) + ']</b> ' +
              _esc(p.error || '(no message)') + '</div>';
            if (p.raw_content) {
              block += '<pre style="margin:4px 0 0;padding:6px;background:var(--bg2);' +
                'border-radius:4px;font-size:10px;color:var(--text3);' +
                'white-space:pre-wrap;word-break:break-all;max-height:120px;overflow-y:auto">' +
                _esc(String(p.raw_content)) + '</pre>';
            }
            if (p.hint) {
              block += '<div style="font-size:10px;color:var(--text3);margin-top:4px">💡 ' +
                _esc(p.hint) + '</div>';
            }
            if (p.skipped && p.skipped.length) {
              block += '<div style="font-size:10px;color:var(--text3);margin-top:4px">skipped: ' +
                _esc(p.skipped.join('; ')) + '</div>';
            }
            return block + '</div>';
          }).join('') +
          '</div>';
      }

      var html = '<div style="padding:24px;max-width:700px;max-height:80vh;overflow-y:auto">' +
        '<h3 style="margin:0 0 8px">' + _esc(t.intent || '(no intent)') + '</h3>' +
        '<div style="font-size:11px;color:var(--text3);margin-bottom:16px">' +
          'task=' + _esc(t.id) + ' · agent=' + _esc(t.agent_id) + ' · template=' + _esc(t.template_id || 'auto') +
          ' · status=' + _esc(t.status) +
        '</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">' +
          '<div><h4 style="margin:0 0 6px;font-size:11px;color:var(--text3);text-transform:uppercase">Phases</h4>' +
          phasesHtml + '</div>' +
          '<div><h4 style="margin:0 0 6px;font-size:11px;color:var(--text3);text-transform:uppercase">Artifacts (' +
            (t.artifacts || []).length + ')</h4>' +
            '<ul style="list-style:none;padding:0;margin:0">' + (arts || '<li style="color:var(--text3);font-size:11px">(none)</li>') + '</ul>' +
          (lessons ? '<h4 style="margin:12px 0 6px;font-size:11px;color:var(--text3);text-transform:uppercase">Lessons</h4>' +
            '<ul style="list-style:none;padding:0;margin:0">' + lessons + '</ul>' : '') +
          '</div>' +
        '</div>' +
        errorsHtml +
        '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">' +
          (t.status === 'running' || t.status === 'queued' ?
            '<button class="btn btn-ghost btn-sm" onclick="v2CancelTask(\'' + _esc(t.id) + '\');closeModal()" style="color:#ef4444">取消任务</button>'
            : '') +
          '<button class="btn btn-ghost" onclick="closeModal()">关闭</button>' +
        '</div>' +
      '</div>';
      if (typeof window.showModalHTML === 'function') window.showModalHTML(html);
    } catch (e) {
      _toast('加载任务详情失败：' + e.message, 'error');
    }
  }

  // ── renderV2AgentQueueTab ──────────────────────────────────────────
  // Inside an agent detail page, show its V2 task queue.

  async function renderV2AgentQueueTab(containerEl, agentId) {
    if (!containerEl) return;
    containerEl.innerHTML = '<div style="color:var(--text3);padding:24px">Loading...</div>';
    try {
      var qr = await _v2api('GET', '/api/v2/agents/' + agentId + '/queue');
      var active = qr.active;
      var queued = qr.queued || [];
    } catch (e) {
      // A 404 here just means this V1 agent has no V2 shell yet (i.e.
      // it was created before V2 mode existed or without V2 enhancement).
      // That's NOT an error the user needs to act on — show an inline
      // hint with a button to create the V2 shell, instead of a red toast.
      if (/not found|404/i.test(e.message || '')) {
        containerEl.innerHTML =
          '<section style="padding:16px 20px;border-top:1px solid var(--border);background:var(--bg)">' +
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">' +
              '<span class="material-symbols-outlined" style="font-size:18px;color:var(--text3)">rocket_launch</span>' +
              '<div style="font-size:13px;font-weight:600">状态机任务队列</div>' +
              '<span style="font-size:10px;padding:2px 7px;border-radius:10px;background:var(--surface2);color:var(--text3)">未启用</span>' +
            '</div>' +
            '<p style="font-size:11px;color:var(--text3);margin:4px 0 10px">' +
              '此 agent 没有 V2 shell。启用后可以用 6-phase 任务状态机跑结构化任务（研究报告、会议纪要等）。</p>' +
            '<button class="btn btn-primary btn-sm" onclick="v2EnableForAgent(\'' + _esc(agentId) + '\')">' +
              '启用状态机任务</button>' +
          '</section>';
        return;
      }
      containerEl.innerHTML = '<div style="color:#ef4444;padding:16px;font-size:12px">V2 队列加载失败：' + _esc(e.message) + '</div>';
      return;
    }
    // Render normal content when fetch succeeded.
    try {

      var body = '<div style="padding:16px">' +
        '<h3 style="margin:0 0 4px;font-size:14px">状态机任务队列</h3>' +
        '<p style="font-size:11px;color:var(--text3);margin-bottom:16px">6-phase 状态机 · 一个 agent 一次只跑一个任务，其他排队</p>' +
        '<div class="card" style="margin-bottom:12px"><div class="card-header">' +
          '<span style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--text2)">' +
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px"></span>运行中</span></div>' +
        (active ? '<div style="padding:12px;border-top:1px solid var(--border);cursor:pointer" ' +
          'onclick="v2OpenTaskDetail(\'' + _esc(active.id) + '\')">' +
          '<div style="font-size:13px;font-weight:500">' + _esc(active.intent || '-') + '</div>' +
          '<div style="font-size:10px;color:var(--text3);margin-top:3px">' +
            _esc(active.phase) + ' · ' + _age(active.updated_at) + '</div>' +
          '</div>'
          : '<div style="padding:16px;text-align:center;color:var(--text3);font-size:12px">(空闲)</div>') +
        '</div>' +
        '<div class="card"><div class="card-header">' +
          '<span style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--text2)">' +
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--primary);margin-right:6px"></span>' +
            '排队中 (' + queued.length + ')</span></div>' +
        (queued.length ?
          queued.map(function(t) {
            return '<div style="padding:10px 12px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">' +
              '<div onclick="v2OpenTaskDetail(\'' + _esc(t.id) + '\')" style="flex:1;cursor:pointer">' +
                '<div style="font-size:12px">' + _esc(t.intent || '-') + '</div>' +
                '<div style="font-size:10px;color:var(--text3)">' + _age(t.created_at) + ' 前提交</div>' +
              '</div>' +
              '<button class="btn btn-ghost btn-sm" onclick="v2CancelTask(\'' + _esc(t.id) + '\')" ' +
                'style="color:#ef4444">取消</button>' +
            '</div>';
          }).join('')
          : '<div style="padding:16px;text-align:center;color:var(--text3);font-size:12px">(无排队任务)</div>') +
        '</div>' +
        '<div style="margin-top:12px">' +
          '<button class="btn btn-primary btn-sm" onclick="v2ShowSubmitTaskModal(\'' + _esc(agentId) + '\')">' +
            '<span class="material-symbols-outlined" style="font-size:16px">add</span> 提交新任务' +
          '</button>' +
        '</div>' +
      '</div>';
      containerEl.innerHTML = body;
    } catch (e) {
      containerEl.innerHTML = '<div style="color:#ef4444;padding:24px">加载失败：' + _esc(e.message) + '</div>';
    }
  }

  // ── renderV2TemplatesSubTab ────────────────────────────────────────

  async function renderV2TemplatesSubTab(containerEl) {
    if (!containerEl) return;
    containerEl.innerHTML = '<div style="color:var(--text3);padding:24px">Loading templates...</div>';
    try {
      var r = await _v2api('GET', '/api/v2/templates');
      var items = r.templates || [];
      var html = '<div style="padding:16px">' +
        '<h3 style="margin:0 0 4px;font-size:14px">状态机任务模板</h3>' +
        '<p style="font-size:11px;color:var(--text3);margin-bottom:16px">' +
          '共 ' + items.length + ' 个模板 · 模板声明必填槽位、工具白名单、验收规则、交付方式</p>' +
        items.map(function(t) {
          return '<div class="card" style="margin-bottom:10px;padding:14px">' +
            '<div style="display:flex;justify-content:space-between;align-items:flex-start">' +
              '<div><div style="font-weight:700;font-size:13px">' + _esc(t.display_name || t.id) + '</div>' +
              '<code style="color:var(--text3);font-size:10px">' + _esc(t.id) + ' · v' + _esc(t.version || 1) + '</code></div>' +
            '</div>' +
            '<div style="font-size:11px;color:var(--text2);margin-top:8px">' +
              '<b>槽位</b>：' + ((t.required_slots || []).join(', ') || '（无）') + '<br>' +
              '<b>工具</b>：' + ((t.allowed_tools || []).join(', ') || '（不限）') +
            '</div>' +
          '</div>';
        }).join('') +
      '</div>';
      containerEl.innerHTML = html;
    } catch (e) {
      containerEl.innerHTML = '<div style="color:#ef4444;padding:24px">加载失败：' + _esc(e.message) + '</div>';
    }
  }

  // ── renderV2TierBindingsSubTab ─────────────────────────────────────

  async function renderV2TierBindingsSubTab(containerEl) {
    if (!containerEl) return;
    containerEl.innerHTML = '<div style="color:var(--text3);padding:24px">Loading providers...</div>';
    try {
      var [pr, tr] = await Promise.all([
        _v2api('GET', '/api/v2/providers'),
        _v2api('GET', '/api/v2/tiers'),
      ]);
      var providers = pr.providers || [];
      var tiers = tr.tiers || [];

      var rows = providers.map(function(p) {
        var tm = p.tier_models || {};
        var tierBindings = tiers.map(function(t) {
          var cur = tm[t] || '';
          var opts = ['<option value="">(未绑定)</option>'].concat(
            (p.models || []).map(function(m) {
              return '<option value="' + _esc(m) + '"' + (m === cur ? ' selected' : '') + '>' + _esc(m) + '</option>';
            })
          );
          if (cur && !(p.models || []).includes(cur)) {
            opts.push('<option value="' + _esc(cur) + '" selected>' + _esc(cur) + ' (custom)</option>');
          }
          return '<div style="display:flex;gap:8px;align-items:center;margin:3px 0">' +
            '<code style="min-width:140px;font-size:11px;color:var(--text2)">' + _esc(t) + '</code>' +
            '<select class="v2-tier-binding" data-pid="' + _esc(p.id) + '" data-tier="' + _esc(t) + '" ' +
              'style="flex:1;font-size:11px;padding:4px 6px;background:var(--surface2);border:1px solid var(--border);' +
              'border-radius:4px;color:var(--text)">' +
            opts.join('') +
            '</select></div>';
        }).join('');
        return '<div class="card" style="margin-bottom:12px;padding:14px">' +
          '<div style="display:flex;gap:10px;align-items:center;margin-bottom:6px">' +
            '<div style="font-weight:700;font-size:13px">' + _esc(p.name) + '</div>' +
            '<code style="color:var(--text3);font-size:10px">' + _esc(p.kind) + ' · ' + _esc(p.base_url) + '</code>' +
            '<span style="font-size:10px;padding:2px 7px;border-radius:10px;' +
              'background:' + (p.enabled ? 'rgba(34,197,94,0.15)' : 'rgba(107,114,128,0.15)') + ';' +
              'color:' + (p.enabled ? '#22c55e' : 'var(--text3)') + '">' +
              (p.enabled ? 'enabled' : 'disabled') + '</span>' +
            '<button class="btn btn-ghost btn-sm" onclick="v2DetectModels(\'' + _esc(p.id) + '\')" ' +
              'style="margin-left:auto">检测模型</button>' +
          '</div>' +
          '<label style="display:flex;gap:6px;align-items:center;margin-bottom:8px;font-size:12px">' +
            '<input type="checkbox" class="v2-mm-toggle" data-pid="' + _esc(p.id) + '"' +
              (p.supports_multimodal ? ' checked' : '') + '>' +
            '支持多模态（图像 / 音频）</label>' +
          '<div style="border-top:1px dashed var(--border);padding-top:8px">' + tierBindings + '</div>' +
          '<div style="margin-top:10px">' +
            '<button class="btn btn-primary btn-sm" onclick="v2SaveTiers(\'' + _esc(p.id) + '\')">保存绑定</button>' +
          '</div>' +
        '</div>';
      }).join('');

      containerEl.innerHTML = '<div style="padding:16px">' +
        '<h3 style="margin:0 0 4px;font-size:14px">LLM Tier 绑定</h3>' +
        '<p style="font-size:11px;color:var(--text3);margin-bottom:16px">' +
          '把 V2 的能力档位（coding_strong / vision 等）绑定到具体的 provider + model，' +
          '状态机 agent 通过 llm_tier 自动路由。V2 从不独立创建 provider — 先在 V1 "设置 → Providers" 管理。</p>' +
        (rows || '<div class="card" style="text-align:center;padding:40px;color:var(--text3)">' +
          '还没有 provider。请在 V1 "设置 → Providers" 添加。</div>') +
      '</div>';
    } catch (e) {
      containerEl.innerHTML = '<div style="color:#ef4444;padding:24px">加载失败：' + _esc(e.message) + '</div>';
    }
  }

  async function v2DetectModels(pid) {
    try {
      var r = await _v2api('POST', '/api/v2/providers/' + pid + '/detect-models');
      _toast('检测到 ' + (r.models || []).length + ' 个模型');
      renderV2TierBindingsSubTab(document.getElementById('v2-tier-bindings-container'));
    } catch (e) {
      _toast('检测失败：' + e.message, 'error');
    }
  }

  async function v2SaveTiers(pid) {
    var tm = {};
    document.querySelectorAll('.v2-tier-binding[data-pid="' + pid + '"]').forEach(function(sel) {
      if (sel.value) tm[sel.dataset.tier] = sel.value;
    });
    var mm = document.querySelector('.v2-mm-toggle[data-pid="' + pid + '"]');
    try {
      await _v2api('PATCH', '/api/v2/providers/' + pid + '/tiers', {
        tier_models: tm,
        supports_multimodal: !!(mm && mm.checked),
      });
      _toast('已保存');
    } catch (e) {
      _toast('保存失败：' + e.message, 'error');
    }
  }

  // ── v2EnhanceAgentCreateForm ───────────────────────────────────────
  // Called by V1 agent creation modal — returns HTML to append to the form.

  async function v2EnhanceAgentCreateForm() {
    if (!isV2Mode()) return '';
    var tiers = ['default'];
    var templates = [];
    try {
      var tr = await _v2api('GET', '/api/v2/tiers');
      tiers = tr.tiers || tiers;
      var tpr = await _v2api('GET', '/api/v2/templates');
      templates = tpr.templates || [];
    } catch (_e) { /* silent */ }

    return '<div style="margin-top:16px;padding:12px;background:rgba(249,115,22,0.05);border:1px solid rgba(249,115,22,0.3);border-radius:8px">' +
      '<div style="font-size:11px;font-weight:700;color:#f97316;text-transform:uppercase;margin-bottom:10px">V2 增强</div>' +
      '<div class="form-group">' +
        '<label>LLM Tier（能力档位）</label>' +
        '<select id="v2-agent-tier" style="width:100%;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">' +
        tiers.map(function(t) { return '<option value="' + _esc(t) + '">' + _esc(t) + '</option>'; }).join('') +
        '</select>' +
      '</div>' +
      '<div class="form-group">' +
        '<label>任务模板（可多选，逗号分隔）</label>' +
        '<input type="text" id="v2-agent-templates" value="conversation" ' +
          'placeholder="e.g. conversation,research_report" ' +
          'style="width:100%;padding:8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">' +
        '<div style="font-size:10px;color:var(--text3);margin-top:4px">可用: ' +
          templates.map(function(t) { return '<code>' + _esc(t.id) + '</code>'; }).join(', ') +
        '</div>' +
      '</div>' +
    '</div>';
  }

  // Retrofit an existing V1 agent with a V2 shell so V2 tasks can
  // target it. Called from the "启用状态机任务" button on an agent detail
  // page that has no V2 shell yet.
  async function v2EnableForAgent(v1AgentId) {
    // Pull V1 metadata so we copy name/role.
    var name = v1AgentId, role = 'assistant';
    try {
      var data = await fetch('/api/portal/agent/' + v1AgentId, {
        credentials: 'same-origin',
      }).then(function(r) { return r.ok ? r.json() : {}; });
      if (data && data.name) name = data.name;
      if (data && data.role) role = data.role;
    } catch (_e) { /* fall back to defaults */ }

    try {
      // Pass the V1 agent id through so the V2 shell shares that id.
      // This keeps one id per logical agent and eliminates the "look up
      // V2 by V1 id returns 404" problem that previously caused users
      // to click 启用状态机任务 repeatedly and create redundant av2_* shells.
      await _v2api('POST', '/api/v2/agents', {
        id: v1AgentId,
        v1_agent_id: v1AgentId,
        name: name,
        role: role,
        capabilities: { llm_tier: 'default', skills: [], mcps: [] },
        task_template_ids: ['conversation'],
      });
      _toast('已启用状态机任务能力');
      if (typeof window.renderCurrentView === 'function') {
        window.renderCurrentView();
      }
    } catch (e) {
      // A 409 (ID_CONFLICT) means shell already exists — that's fine,
      // treat as "already enabled" and refresh.
      if (/ID_CONFLICT|already exists/i.test(e.message || '')) {
        _toast('状态机任务能力之前已启用');
        if (typeof window.renderCurrentView === 'function') {
          window.renderCurrentView();
        }
        return;
      }
      _toast('启用失败：' + e.message, 'error');
    }
  }

  // After V1 create-agent finishes, call this to also register the V2 shell
  // (agent body is created in V2 only if V2 mode is on).
  // Passes the V1 agent id explicitly so the V2 shell adopts the same id —
  // one id per logical agent across both systems.
  async function v2AfterAgentCreated(v1AgentId, v1Name, v1Role) {
    if (!isV2Mode()) return;
    var tier = (document.getElementById('v2-agent-tier') || {}).value || 'default';
    var tplInput = (document.getElementById('v2-agent-templates') || {}).value || 'conversation';
    var templates = tplInput.split(',').map(function(s) { return s.trim(); }).filter(Boolean);
    try {
      await _v2api('POST', '/api/v2/agents', {
        id: v1AgentId,
        v1_agent_id: v1AgentId,
        name: v1Name,
        role: v1Role || 'assistant',
        capabilities: { llm_tier: tier, skills: [], mcps: [] },
        task_template_ids: templates,
      });
      _toast('状态机 agent shell 已创建');
    } catch (e) {
      if (/ID_CONFLICT|already exists/i.test(e.message || '')) return;
      _toast('状态机 agent 创建失败：' + e.message, 'error');
    }
  }

  // ── publish ────────────────────────────────────────────────────────

  window.isV2Mode = isV2Mode;
  window.toggleV2Mode = toggleV2Mode;
  window.renderV2TasksSubTab = renderV2TasksSubTab;
  window.renderV2AgentQueueTab = renderV2AgentQueueTab;
  window.renderV2TemplatesSubTab = renderV2TemplatesSubTab;
  window.renderV2TierBindingsSubTab = renderV2TierBindingsSubTab;
  window.v2EnhanceAgentCreateForm = v2EnhanceAgentCreateForm;
  window.v2AfterAgentCreated = v2AfterAgentCreated;
  window.v2OpenTaskDetail = v2OpenTaskDetail;
  window.v2CancelTask = v2CancelTask;
  window.v2ResumeTask = v2ResumeTask;
  window.v2PauseTask = v2PauseTask;
  window.v2DeleteTask = v2DeleteTask;
  window.v2ShowSubmitTaskModal = v2ShowSubmitTaskModal;
  window.v2DoSubmitTask = v2DoSubmitTask;
  window.v2DetectModels = v2DetectModels;
  window.v2SaveTiers = v2SaveTiers;
  window.v2EnableForAgent = v2EnableForAgent;

  // ── loadV2TasksIntoQueue ───────────────────────────────────────────
  // Append V2 tasks into the existing V1 Task Queue list (tasks-list-<id>)
  // so users see both kinds in ONE place. Each V2 item gets a 🚀 prefix
  // + status chip to distinguish it from V1 tasks without visual clutter.
  async function loadV2TasksIntoQueue(agentId) {
    if (!agentId) return;
    var host = document.getElementById('tasks-list-' + agentId);
    if (!host) return;

    var qr;
    try {
      qr = await _v2api('GET', '/api/v2/agents/' + agentId + '/queue');
    } catch (e) {
      // 404 = no V2 shell — show a lightweight one-liner with "启用" action.
      if (/not found|404/i.test(e.message || '')) {
        var hint = document.getElementById('v2-queue-hint-' + agentId);
        if (hint) hint.remove();
        var notice = document.createElement('div');
        notice.id = 'v2-queue-hint-' + agentId;
        notice.style.cssText = 'padding:8px;margin-top:8px;border-top:1px dashed var(--border);' +
          'font-size:10px;color:var(--text3)';
        notice.innerHTML = '🚀 状态机任务未启用 · ' +
          '<a href="#" onclick="event.preventDefault();v2EnableForAgent(\'' + _esc(agentId) + '\')" ' +
          'style="color:var(--primary);cursor:pointer">启用</a>';
        host.appendChild(notice);
      }
      // For ANY error we still need to drop stale rows — otherwise a
      // transient 5xx / network blip leaves a dead "运行中" row in the
      // DOM forever. The old behavior silently returned which is how
      // failed tasks appeared to "stick" after an API hiccup.
      return;
    }

    // Upsert by task.id instead of wipe-and-rebuild — that wipe cycle is
    // what made each card flicker between "Queued" and "运行中" on every
    // poll. We now reconcile: keep rows that still exist, update their
    // inner text, insert new rows at the bottom, remove stale rows.
    var existingHint = document.getElementById('v2-queue-hint-' + agentId);
    if (existingHint) existingHint.remove();

    var items = [];
    if (qr.active) items.push(qr.active);
    (qr.queued || []).forEach(function(t) { items.push(t); });

    var liveIds = {};
    items.forEach(function(t) { liveIds[t.id] = true; });

    // Ensure separator label exists at the correct position (first V2 row).
    var sep = host.querySelector('[data-v2-sep]');
    if (items.length) {
      if (!sep) {
        sep = document.createElement('div');
        sep.setAttribute('data-v2-row', '1');
        sep.setAttribute('data-v2-sep', '1');
        sep.style.cssText = 'margin:10px 0 4px;font-size:9px;font-weight:700;' +
          'color:#f97316;text-transform:uppercase;letter-spacing:0.5px';
        sep.textContent = '🚀 状态机任务';
        host.appendChild(sep);
      }
    } else if (sep) {
      sep.remove();
    }

    // Remove rows whose task vanished from the queue.
    host.querySelectorAll('[data-v2-task-id]').forEach(function(el) {
      var id = el.getAttribute('data-v2-task-id');
      if (!liveIds[id]) el.remove();
    });

    // Upsert each task row.
    items.forEach(function(t) {
      var isActive = (t === qr.active);
      var statusLabel = isActive ? '运行中' : '排队中';
      var statusColor = isActive ? '#22c55e' : 'var(--primary)';
      // Show total task lifetime (created_at), not "time since last
      // update". The latter resets to 0s on every phase retry, which
      // misled users into thinking the task had restarted. Also surface
      // retry counts so a stuck phase is visible instead of hidden
      // behind a perpetual "0s 运行中".
      var ageStr = _age(t.created_at);
      var retrySuffix = '';
      var retries = t.retries || {};
      var retryEntries = Object.keys(retries).filter(function(k){ return retries[k] > 0; });
      if (retryEntries.length) {
        retrySuffix = ' · ⟳' + retryEntries.map(function(k){
          return k + ':' + retries[k];
        }).join(' ');
      }
      var innerHtml =
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:6px">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:11px;font-weight:600;overflow:hidden;' +
              'text-overflow:ellipsis;white-space:nowrap">🚀 ' +
              _esc(t.intent || '(no intent)') + '</div>' +
            '<div style="font-size:9px;color:var(--text3);margin-top:2px">' +
              'phase=' + _esc(t.phase) + ' · ' + ageStr + retrySuffix + '</div>' +
          '</div>' +
          '<span style="font-size:9px;padding:2px 6px;border-radius:8px;' +
            'background:rgba(255,255,255,0.08);color:' + statusColor + ';font-weight:600">' +
            statusLabel + '</span>' +
        '</div>';
      var row = host.querySelector('[data-v2-task-id="' + t.id + '"]');
      if (row) {
        // update in place — this is what kills the flicker
        if (row.innerHTML !== innerHtml) row.innerHTML = innerHtml;
      } else {
        row = document.createElement('div');
        row.setAttribute('data-v2-row', '1');
        row.setAttribute('data-v2-task-id', t.id);
        row.style.cssText = 'background:var(--surface3);border-radius:6px;padding:8px 10px;' +
          'border:1px solid rgba(249,115,22,0.2);cursor:pointer;margin-bottom:4px';
        row.onclick = function() { v2OpenTaskDetail(t.id); };
        row.innerHTML = innerHtml;
        host.appendChild(row);
      }
    });

    // Quick-add button — keep a single instance at the very bottom.
    var addBtn = host.querySelector('[data-v2-addbtn]');
    if (!addBtn) {
      addBtn = document.createElement('div');
      addBtn.setAttribute('data-v2-row', '1');
      addBtn.setAttribute('data-v2-addbtn', '1');
      addBtn.style.cssText = 'margin-top:6px;padding:6px 10px;text-align:center;' +
        'background:transparent;border:1px dashed rgba(249,115,22,0.4);border-radius:6px;' +
        'color:#f97316;font-size:10px;cursor:pointer';
      addBtn.textContent = '+ 新建状态机任务';
      addBtn.onclick = function() { v2ShowSubmitTaskModal(agentId); };
    }
    host.appendChild(addBtn);  // move to end if it already exists
  }
  window.loadV2TasksIntoQueue = loadV2TasksIntoQueue;
})();
