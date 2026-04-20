// TudouClaw V2 — single-file SPA.
// Three panels (Board / Timeline / Console), URL routing via hash,
// SSE live updates with reconnect.

// ── API helpers ───────────────────────────────────────────────────────

function authHeaders() {
  const headers = { "Content-Type": "application/json" };
  // JWT in localStorage is how the portal stores the current token.
  const tok = localStorage.getItem("jwt_token") || localStorage.getItem("access_token");
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  return headers;
}

async function api(method, path, body) {
  const opts = { method, headers: authHeaders(), credentials: "same-origin" };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const msg = (data && data.detail && data.detail.error) || resp.statusText;
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// ── state ────────────────────────────────────────────────────────────

const state = {
  agents: [],
  templates: [],
  tasks: [],                         // combined list
  currentTaskId: "",
  sse: null,
  lastEventTs: 0,
  reconnectAttempt: 0,
  timelineEvents: [],
  activePhase: "",
  phaseStatus: {},                   // phase → "done" | "active" | "failed"
};

// ── URL routing ──────────────────────────────────────────────────────

function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").filter(Boolean);
  // v2 / tasks / <id>  |  v2 / templates  |  v2 / agents / <id>
  // v2 / providers                   → tier bindings page
  if (parts[0] === "v2" && parts[1] === "tasks" && parts[2]) {
    return { route: "task", id: parts[2] };
  }
  if (parts[0] === "v2" && parts[1] === "templates") {
    return { route: "templates", id: parts[2] || "" };
  }
  if (parts[0] === "v2" && parts[1] === "agents" && parts[2]) {
    return { route: "agent", id: parts[2] };
  }
  if (parts[0] === "v2" && parts[1] === "agents") {
    return { route: "agents" };  // list / CRUD
  }
  if (parts[0] === "v2" && parts[1] === "providers") {
    return { route: "providers" };
  }
  return { route: "board" };
}

function go(path) {
  location.hash = path;
}

window.addEventListener("hashchange", applyRoute);
function applyRoute() {
  const r = parseHash();
  if (r.route === "task" && r.id !== state.currentTaskId) {
    selectTask(r.id);
  } else if (r.route === "templates") {
    renderTemplatesPanel(r.id);
  } else if (r.route === "agent") {
    renderAgentDetailPanel(r.id);
  } else if (r.route === "agents") {
    renderAgentsListPanel();
  } else if (r.route === "providers") {
    renderProvidersPanel();
  }
  _highlightActiveTab(r.route);
}

function _highlightActiveTab(route) {
  // Map every route to the tab that owns it.
  const bucket =
    route === "task" || route === "board" ? "tasks" :
    route === "agent" || route === "agents" ? "agents" :
    route === "templates" ? "templates" :
    route === "providers" ? "providers" : "tasks";
  document.querySelectorAll("#v2-tabs .tab").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === bucket);
  });
}

function renderTemplatesPanel(pickedId) {
  // Hijack the Timeline column to show the templates catalogue.
  const head = document.getElementById("timeline-title");
  head.textContent = "📚 Templates";
  document.getElementById("timeline-meta").textContent =
    `${state.templates.length} available`;
  const rows = state.templates.map(t => `
    <div class="phase-row ${pickedId === t.id ? "active" : ""}"
         style="cursor:pointer;display:flex;flex-direction:column;align-items:flex-start"
         data-tid="${t.id}">
      <div style="font-weight:600">${escapeHtml(t.display_name || t.id)} ·
        <span style="color:#64748b;font-weight:normal">${t.id}</span></div>
      <div style="font-size:11px;color:#475569">
        slots: ${(t.required_slots || []).join(", ") || "—"} ·
        tools: ${(t.allowed_tools || []).join(", ") || "—"}
      </div>
    </div>`).join("");
  document.getElementById("timeline-phases").innerHTML = rows;
  document.getElementById("timeline-events").innerHTML = "";
  document.querySelectorAll("#timeline-phases [data-tid]").forEach(el => {
    el.onclick = () => go(`/v2/templates/${el.dataset.tid}`);
  });
}

async function renderAgentDetailPanel(agentId) {
  const ag = state.agents.find(a => a.id === agentId);
  const head = document.getElementById("timeline-title");
  head.textContent = ag ? `🤖 ${ag.name}` : `agent ${agentId} not found`;
  document.getElementById("timeline-meta").textContent = ag
    ? `role=${escapeHtml(ag.role)} · tier=${escapeHtml(ag.capabilities.llm_tier || "default")} · skills=${(ag.capabilities.skills||[]).length} · mcps=${(ag.capabilities.mcps||[]).length}`
    : "";

  // Fetch live queue + task list so Active / Queued / Done stay accurate.
  let queue = { active: null, queued: [] };
  let tasks = [];
  try {
    const [qr, tr] = await Promise.all([
      api("GET", `/api/v2/agents/${agentId}/queue`),
      api("GET", `/api/v2/tasks?agent_id=${encodeURIComponent(agentId)}&limit=200`),
    ]);
    queue = { active: qr.active, queued: qr.queued || [] };
    tasks = tr.tasks || [];
  } catch (e) {
    toast("加载 agent 详情失败：" + e.message, "error");
  }

  const done = tasks.filter(t => t.status !== "running"
                                  && t.status !== "paused"
                                  && t.status !== "queued");

  const renderList = (items, withCancel) => {
    if (!items.length) return `<div style="color:#94a3b8;font-size:11px">（无）</div>`;
    return items.map(t => `
      <div class="phase-row" data-tid="${t.id}">
        <span class="phase-name">
          ${escapeHtml(taskIcon(t))} ${escapeHtml(t.intent || "(no intent)")}
        </span>
        <span class="phase-sub">
          ${escapeHtml(t.phase)} · ${humanAge(t.updated_at || t.created_at)}
          ${withCancel ? ` <button class="inline-cancel" data-tid="${t.id}" style="margin-left:8px">取消</button>` : ""}
        </span>
      </div>`).join("");
  };

  const html = `
    <h4 style="margin:0 0 4px">🔄 Active</h4>
    ${queue.active
      ? renderList([queue.active], false)
      : `<div style="color:#94a3b8;font-size:11px">（空闲）</div>`}
    <h4 style="margin:12px 0 4px">⏳ Queued (${queue.queued.length})</h4>
    ${renderList(queue.queued, true)}
    <h4 style="margin:12px 0 4px">✅ Done (${done.length})</h4>
    ${renderList(done.slice(0, 50), false)}
  `;
  document.getElementById("timeline-phases").innerHTML = html;
  document.getElementById("timeline-events").innerHTML = "";

  // Wire click-to-timeline on each task row.
  document.querySelectorAll("#timeline-phases [data-tid]").forEach(row => {
    if (row.tagName === "BUTTON") return;
    row.onclick = (e) => {
      // Don't swallow the cancel-button click.
      if (e.target.classList.contains("inline-cancel")) return;
      go(`/v2/tasks/${row.dataset.tid}`);
    };
  });
  document.querySelectorAll(".inline-cancel").forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const tid = btn.dataset.tid;
      try {
        await api("POST", `/api/v2/tasks/${tid}/cancel`);
        toast("已取消");
        renderAgentDetailPanel(agentId);  // refresh
      } catch (err) {
        toast("取消失败：" + err.message, "error");
      }
    };
  });
}

function taskIcon(t) {
  return ({ running: "🔄", paused: "⏸", queued: "⏳",
            succeeded: "✅", failed: "❌", abandoned: "⊘" })[t.status] || "·";
}

// ── Agent CRUD list page ─────────────────────────────────────────────


async function renderAgentsListPanel() {
  document.getElementById("timeline-title").textContent = "🤖 Agents";
  document.getElementById("timeline-meta").textContent =
    "创建、编辑、删除 V2 agent。删除会清除该 agent 的所有任务、记忆、" +
    "MCP 绑定、skill 授权等关联数据。";

  // Pull fresh agent list + tier catalog + V1 agents (for clone).
  let agents = [], tiers = [], v1Agents = [];
  try {
    const [ar, tr] = await Promise.all([
      api("GET", "/api/v2/agents?include_archived=false"),
      api("GET", "/api/v2/tiers"),
    ]);
    agents = ar.agents || [];
    tiers = tr.tiers || [];
  } catch (e) {
    toast("加载 agents 失败：" + e.message, "error");
  }
  try {
    // V1 list uses the legacy portal API; best-effort.
    const r = await fetch("/api/portal/agents", {
      headers: authHeaders(), credentials: "same-origin",
    });
    if (r.ok) {
      const d = await r.json();
      v1Agents = (d.agents || []).filter(a => a.location === "local");
    }
  } catch (_e) { /* v1 optional */ }

  state.agents = agents;  // refresh caches used by other panels
  renderAgentFilter();
  renderConsoleForm();

  const tierOptions = tiers.map(t =>
    `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");

  const agentRows = agents.map(a => `
    <div class="phase-row" style="flex-direction:column;align-items:stretch;padding:10px"
         data-aid="${a.id}">
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:4px">
        <div style="font-weight:600">${escapeHtml(a.name)}</div>
        <code style="color:#64748b;font-size:11px">${escapeHtml(a.id)}</code>
        <span class="count">${escapeHtml(a.role)}</span>
        <span class="count">tier: ${escapeHtml(a.capabilities.llm_tier)}</span>
      </div>
      <div style="font-size:11px;color:#475569">
        skills: ${(a.capabilities.skills || []).length > 0
                    ? a.capabilities.skills.map(escapeHtml).join(", ")
                    : "—"} ·
        mcps: ${(a.capabilities.mcps || []).length > 0
                    ? a.capabilities.mcps.map(escapeHtml).join(", ")
                    : "—"} ·
        templates: ${(a.task_template_ids || []).join(", ") || "—"}
      </div>
      <div style="margin-top:8px;display:flex;gap:6px">
        <button data-act="open" data-aid="${a.id}">打开详情</button>
        <button data-act="edit" data-aid="${a.id}">编辑</button>
        <button data-act="delete-soft" data-aid="${a.id}"
                style="background:#64748b">归档</button>
        <button data-act="delete-hard" data-aid="${a.id}"
                style="background:#dc2626">永久删除</button>
      </div>
      <form data-aid="${a.id}" class="edit-form" style="display:none;margin-top:8px;gap:4px">
        <input name="name" value="${escapeHtml(a.name)}" placeholder="name" />
        <input name="role" value="${escapeHtml(a.role)}" placeholder="role" />
        <select name="llm_tier">
          ${tiers.map(t =>
            `<option value="${escapeHtml(t)}" ${t === a.capabilities.llm_tier ? "selected" : ""}>${escapeHtml(t)}</option>`
          ).join("")}
        </select>
        <input name="skills" value="${escapeHtml((a.capabilities.skills||[]).join(","))}"
               placeholder="skill ids (逗号分隔)" />
        <input name="mcps" value="${escapeHtml((a.capabilities.mcps||[]).join(","))}"
               placeholder="mcp ids (逗号分隔)" />
        <input name="templates" value="${escapeHtml((a.task_template_ids||[]).join(","))}"
               placeholder="template ids (逗号分隔)" />
        <div style="display:flex;gap:6px">
          <button type="submit">保存</button>
          <button type="button" data-act="cancel-edit" data-aid="${a.id}">取消</button>
        </div>
      </form>
    </div>
  `).join("");

  // Clone-from-V1 section
  const cloneSection = v1Agents.length
    ? `<div class="phase-row" style="flex-direction:column;padding:10px;background:#fef9c3">
         <h4 style="margin:0 0 6px">📋 从 V1 克隆 agent</h4>
         <select id="v1-clone-pick" style="font-size:12px">
           <option value="">(选择一个 V1 agent)</option>
           ${v1Agents.map(a =>
             `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)} · ${escapeHtml(a.role)} (${escapeHtml(a.id)})</option>`
           ).join("")}
         </select>
         <button id="v1-clone-do" style="margin-top:6px;align-self:flex-start">克隆到 V2</button>
         <div style="font-size:11px;color:#78350f;margin-top:4px">
           只克隆身份（name/role/skills/mcps）。不复制消息、system_prompt、工作目录等。
         </div>
       </div>`
    : "";

  const html = `
    <div class="phase-row" style="flex-direction:column;padding:10px;background:#ecfdf5">
      <h4 style="margin:0 0 6px">➕ 创建新 V2 agent</h4>
      <form id="new-agent-form" style="display:flex;flex-direction:column;gap:4px">
        <input name="name" placeholder="name" required />
        <input name="role" placeholder="role (e.g. assistant)" required />
        <select name="llm_tier">
          <option value="default" selected>default</option>
          ${tierOptions}
        </select>
        <input name="skills" placeholder="skill ids (逗号分隔)" />
        <input name="mcps" placeholder="mcp ids (逗号分隔)" />
        <input name="templates" value="conversation"
               placeholder="template ids (逗号分隔)" />
        <button type="submit" style="align-self:flex-start">创建</button>
      </form>
    </div>
    ${cloneSection}
    <h4 style="margin:12px 0 4px">已有 V2 agents (${agents.length})</h4>
    ${agentRows || `<div style="color:#94a3b8;font-size:11px">（还没有 agent）</div>`}
  `;
  document.getElementById("timeline-phases").innerHTML = html;
  document.getElementById("timeline-events").innerHTML = "";

  // Wire create form.
  const createForm = document.getElementById("new-agent-form");
  createForm.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(createForm);
    try {
      await api("POST", "/api/v2/agents", {
        name: (fd.get("name") || "").trim(),
        role: (fd.get("role") || "").trim(),
        capabilities: {
          llm_tier: fd.get("llm_tier") || "default",
          skills: _csv(fd.get("skills")),
          mcps:   _csv(fd.get("mcps")),
        },
        task_template_ids: _csv(fd.get("templates")),
      });
      toast("已创建");
      renderAgentsListPanel();
    } catch (err) {
      toast("创建失败：" + err.message, "error");
    }
  };

  // Clone from V1.
  const cloneBtn = document.getElementById("v1-clone-do");
  if (cloneBtn) {
    cloneBtn.onclick = async () => {
      const pick = document.getElementById("v1-clone-pick").value;
      if (!pick) { toast("请先选择 V1 agent", "error"); return; }
      try {
        await api("POST", "/api/v2/agents/_clone/clone_from_v1", { v1_agent_id: pick });
        toast("已克隆");
        renderAgentsListPanel();
      } catch (err) {
        toast("克隆失败：" + err.message, "error");
      }
    };
  }

  // Wire action buttons per agent.
  document.querySelectorAll("#timeline-phases [data-act]").forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const aid = btn.dataset.aid;
      const act = btn.dataset.act;
      if (act === "open") {
        go(`/v2/agents/${aid}`);
      } else if (act === "edit") {
        const form = document.querySelector(`form.edit-form[data-aid="${aid}"]`);
        if (form) form.style.display = "flex";
        form.style.flexDirection = "column";
      } else if (act === "cancel-edit") {
        const form = document.querySelector(`form.edit-form[data-aid="${aid}"]`);
        if (form) form.style.display = "none";
      } else if (act === "delete-soft") {
        if (!confirm("归档该 agent？可以恢复。")) return;
        try {
          await api("DELETE", `/api/v2/agents/${aid}`);
          toast("已归档");
          renderAgentsListPanel();
        } catch (err) { toast("归档失败：" + err.message, "error"); }
      } else if (act === "delete-hard") {
        const a = agents.find(x => x.id === aid);
        const name = a ? a.name : aid;
        if (!confirm(
          `永久删除 ${name}？\n\n将清除：\n` +
          `• 该 agent 的所有 V2 任务和事件\n` +
          `• 记忆（episodic/semantic）\n` +
          `• MCP 绑定和环境变量\n` +
          `• Skill 授权\n` +
          `• 工作目录\n\n此操作不可撤销。`
        )) return;
        try {
          const r = await api("DELETE", `/api/v2/agents/${aid}?hard=true`);
          const rep = r.purge || {};
          toast(`已永久删除（清理 ${Object.values(rep).reduce((a,b)=>a+(b||0),0)} 条关联数据）`);
          renderAgentsListPanel();
        } catch (err) { toast("删除失败：" + err.message, "error"); }
      }
    };
  });

  // Wire edit form submissions.
  document.querySelectorAll("form.edit-form").forEach(f => {
    f.onsubmit = async (e) => {
      e.preventDefault();
      const aid = f.dataset.aid;
      const fd = new FormData(f);
      try {
        await api("PATCH", `/api/v2/agents/${aid}`, {
          name: (fd.get("name") || "").trim(),
          role: (fd.get("role") || "").trim(),
          capabilities: {
            llm_tier: fd.get("llm_tier") || "default",
            skills: _csv(fd.get("skills")),
            mcps:   _csv(fd.get("mcps")),
          },
          task_template_ids: _csv(fd.get("templates")),
        });
        toast("已保存");
        renderAgentsListPanel();
      } catch (err) {
        toast("保存失败：" + err.message, "error");
      }
    };
  });
}


function _csv(v) {
  return String(v || "").split(",").map(s => s.trim()).filter(Boolean);
}


async function renderProvidersPanel() {
  document.getElementById("timeline-title").textContent = "🔌 LLM Providers · Tier Bindings";
  document.getElementById("timeline-meta").textContent =
    "Bind capability tiers (e.g. coding_strong) to concrete provider + model, " +
    "and declare multimodal support. V2 never creates providers — manage them in V1.";

  let providers = [];
  let tiers = [];
  try {
    const [pr, tr] = await Promise.all([
      api("GET", "/api/v2/providers"),
      api("GET", "/api/v2/tiers"),
    ]);
    providers = pr.providers || [];
    tiers = tr.tiers || [];
  } catch (e) {
    toast("加载 provider 列表失败：" + e.message, "error");
    return;
  }

  const rows = providers.map(p => {
    const tm = p.tier_models || {};
    const tierBindings = tiers.map(t => {
      const current = tm[t] || "";
      const modelOptions = ["<option value=''>(未绑定)</option>"]
        .concat((p.models || []).map(m =>
          `<option value="${escapeHtml(m)}" ${m === current ? "selected" : ""}>${escapeHtml(m)}</option>`
        ));
      // Allow current value even if it's not in the detected list
      // (e.g. manually-entered model names).
      if (current && !(p.models || []).includes(current)) {
        modelOptions.push(`<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (custom)</option>`);
      }
      return `
        <div style="display:flex;gap:8px;align-items:center;margin:2px 0">
          <code style="min-width:140px;font-size:11px;color:#475569">${escapeHtml(t)}</code>
          <select class="tier-binding" data-pid="${p.id}" data-tier="${t}"
                  style="flex:1;font-size:11px">
            ${modelOptions.join("")}
          </select>
        </div>`;
    }).join("");

    return `
      <div class="phase-row" style="flex-direction:column;align-items:stretch;padding:12px">
        <div style="display:flex;gap:12px;align-items:center">
          <div style="font-weight:600">${escapeHtml(p.name)}</div>
          <code style="color:#64748b;font-size:11px">${escapeHtml(p.kind)} · ${escapeHtml(p.base_url)}</code>
          <span class="count" style="${p.enabled ? "background:#dcfce7;color:#166534" : ""}">
            ${p.enabled ? "enabled" : "disabled"}
          </span>
          <button class="detect-models" data-pid="${p.id}"
                  style="margin-left:auto">检测模型</button>
        </div>
        <label style="display:flex;gap:6px;align-items:center;margin-top:6px;font-size:12px">
          <input type="checkbox" class="mm-toggle" data-pid="${p.id}"
                 ${p.supports_multimodal ? "checked" : ""} />
          支持多模态（图像 / 音频）
        </label>
        <div style="margin-top:6px;border-top:1px dashed #e5e7eb;padding-top:6px">
          ${tierBindings}
        </div>
        <div style="margin-top:8px">
          <button class="save-tiers" data-pid="${p.id}">保存绑定</button>
        </div>
      </div>
    `;
  }).join("");

  document.getElementById("timeline-phases").innerHTML =
    rows || `<div style="color:#94a3b8;font-size:11px">（V1 Providers 页面还没有 provider）</div>`;
  document.getElementById("timeline-events").innerHTML = "";

  // Wire buttons.
  document.querySelectorAll(".detect-models").forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await api("POST", `/api/v2/providers/${btn.dataset.pid}/detect-models`);
        toast(`检测到 ${(r.models || []).length} 个模型`);
        renderProvidersPanel();
      } catch (e) {
        toast("检测失败：" + e.message, "error");
      } finally {
        btn.disabled = false;
      }
    };
  });

  document.querySelectorAll(".save-tiers").forEach(btn => {
    btn.onclick = async () => {
      const pid = btn.dataset.pid;
      const tm = {};
      document.querySelectorAll(`.tier-binding[data-pid="${pid}"]`).forEach(sel => {
        if (sel.value) tm[sel.dataset.tier] = sel.value;
      });
      const mm = document.querySelector(`.mm-toggle[data-pid="${pid}"]`);
      try {
        await api("PATCH", `/api/v2/providers/${pid}/tiers`, {
          tier_models: tm,
          supports_multimodal: !!(mm && mm.checked),
        });
        toast("已保存");
      } catch (e) {
        toast("保存失败：" + e.message, "error");
      }
    };
  });
}

// ── initial load ─────────────────────────────────────────────────────

async function init() {
  try {
    const [agentsResp, tmplsResp, tasksResp] = await Promise.all([
      api("GET", "/api/v2/agents"),
      api("GET", "/api/v2/templates"),
      api("GET", "/api/v2/tasks?limit=200"),
    ]);
    state.agents = agentsResp.agents || [];
    state.templates = tmplsResp.templates || [];
    state.tasks = tasksResp.tasks || [];
  } catch (e) {
    toast("加载失败：" + e.message, "error");
    return;
  }
  renderAgentFilter();
  renderConsoleForm();
  renderBoard();
  applyRoute();
  wireConsole();
  // Refresh tasks list every 15s — SSE covers live timeline updates
  // but Board needs the full list for other tasks.
  setInterval(refreshBoard, 15000);
}

async function refreshBoard() {
  try {
    const r = await api("GET", "/api/v2/tasks?limit=200");
    state.tasks = r.tasks || [];
    renderBoard();
  } catch (_e) { /* silent */ }
}

// ── Board ────────────────────────────────────────────────────────────

function renderAgentFilter() {
  const sel = document.getElementById("agent-filter");
  sel.innerHTML = '<option value="">All agents</option>' +
    state.agents.map(a =>
      `<option value="${a.id}">${escapeHtml(a.name)} (${escapeHtml(a.role)})</option>`
    ).join("");
  sel.onchange = renderBoard;
}

function renderBoard() {
  const filter = document.getElementById("agent-filter").value;
  const groups = { running: [], paused: [], done: [] };
  for (const t of state.tasks) {
    if (filter && t.agent_id !== filter) continue;
    if (t.status === "running") groups.running.push(t);
    else if (t.status === "paused") groups.paused.push(t);
    else groups.done.push(t);
  }
  for (const key of ["running", "paused", "done"]) {
    document.querySelector(`[data-count="${key}"]`).textContent = groups[key].length;
    const ul = document.querySelector(`[data-list="${key}"]`);
    ul.innerHTML = groups[key].map(t => taskCardHtml(t)).join("");
    ul.querySelectorAll("li").forEach(li => {
      li.addEventListener("click", () => go(`/v2/tasks/${li.dataset.id}`));
      if (li.dataset.id === state.currentTaskId) li.classList.add("active");
    });
  }
}

function taskCardHtml(t) {
  const mark = { running: "●", paused: "⏸", succeeded: "✓",
                  failed: "✗", abandoned: "⊘" }[t.status] || "·";
  const age = humanAge(t.updated_at || t.created_at);
  return `<li data-id="${t.id}">
    <span class="intent">${mark} ${escapeHtml(t.intent || "(no intent)")}</span>
    <span class="meta">${t.phase} · ${age}</span>
  </li>`;
}

function humanAge(ts) {
  const d = Math.max(0, Date.now() / 1000 - Number(ts || 0));
  if (d < 60) return `${Math.floor(d)}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

// ── Console (submit / clarify / actions) ─────────────────────────────

function renderConsoleForm() {
  const ag = document.getElementById("console-agent");
  ag.innerHTML = state.agents.map(a =>
    `<option value="${a.id}">${escapeHtml(a.name)}</option>`
  ).join("");

  const tp = document.getElementById("console-template");
  tp.innerHTML = '<option value="">(default)</option>' +
    state.templates.map(t =>
      `<option value="${t.id}">${escapeHtml(t.display_name || t.id)}</option>`
    ).join("");
}

// Pending attachment descriptors (from the upload endpoint) that will
// be sent along with the next task submission. Cleared after Submit.
state.pendingAttachments = [];

function renderAttachmentList() {
  const ul = document.getElementById("attachment-list");
  ul.innerHTML = state.pendingAttachments.map((a, i) =>
    `<li style="display:flex;gap:6px;align-items:center">
       <span style="font-size:10px">[${a.kind}]</span>
       <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
         ${escapeHtml(a.name)}
       </span>
       <button data-idx="${i}" class="remove-att"
               style="background:transparent;color:#dc2626;padding:0 4px">×</button>
     </li>`
  ).join("");
  ul.querySelectorAll(".remove-att").forEach(btn => {
    btn.onclick = () => {
      state.pendingAttachments.splice(Number(btn.dataset.idx), 1);
      renderAttachmentList();
    };
  });
}

async function uploadAttachment(file, agentId) {
  const form = new FormData();
  form.append("file", file);
  const headers = {};
  const tok = localStorage.getItem("jwt_token") || localStorage.getItem("access_token");
  if (tok) headers["Authorization"] = `Bearer ${tok}`;
  const resp = await fetch(`/api/v2/agents/${agentId}/attachments`, {
    method: "POST", headers, credentials: "same-origin", body: form,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const msg = (data && data.detail && data.detail.error) || resp.statusText;
    throw new Error(msg);
  }
  return data.attachment;
}

function wireConsole() {
  document.getElementById("attachment-input").onchange = async (e) => {
    const agentId = document.getElementById("console-agent").value;
    if (!agentId) { toast("请先选择 agent", "error"); return; }
    for (const file of Array.from(e.target.files || [])) {
      try {
        const a = await uploadAttachment(file, agentId);
        state.pendingAttachments.push(a);
      } catch (err) {
        toast("上传失败：" + err.message, "error");
      }
    }
    e.target.value = "";
    renderAttachmentList();
  };

  document.getElementById("open-agent-detail").onclick = (e) => {
    e.preventDefault();
    const agentId = document.getElementById("console-agent").value;
    if (agentId) go(`/v2/agents/${agentId}`);
  };

  document.getElementById("console-submit").onclick = async () => {
    const agentId = document.getElementById("console-agent").value;
    const tmpl = document.getElementById("console-template").value;
    const intent = document.getElementById("console-intent").value.trim();
    if (!agentId) { toast("请先选择 agent", "error"); return; }
    if (!intent)  { toast("意图不能为空", "error"); return; }
    try {
      const r = await api("POST", `/api/v2/agents/${agentId}/tasks`, {
        intent, template_id: tmpl,
        attachments: state.pendingAttachments,
      });
      document.getElementById("console-intent").value = "";
      state.pendingAttachments = [];
      renderAttachmentList();
      toast(r.task.status === "queued" ? "已排队" : "任务已提交");
      state.tasks.unshift(r.task);
      renderBoard();
      go(`/v2/tasks/${r.task.id}`);
    } catch (e) {
      toast("提交失败：" + e.message, "error");
    }
  };

  document.getElementById("clarify-send").onclick = async () => {
    if (!state.currentTaskId) return;
    const answer = document.getElementById("clarify-answer").value.trim();
    if (!answer) return;
    try {
      await api("POST", `/api/v2/tasks/${state.currentTaskId}/clarify`, { answer });
      document.getElementById("clarify-answer").value = "";
      document.getElementById("clarify-panel").hidden = true;
      toast("已提交答案，任务继续");
    } catch (e) {
      toast("提交答案失败：" + e.message, "error");
    }
  };

  document.querySelectorAll("#actions-panel button").forEach(btn => {
    btn.onclick = async () => {
      if (!state.currentTaskId) return;
      const action = btn.dataset.action;
      try {
        await api("POST", `/api/v2/tasks/${state.currentTaskId}/${action}`);
        toast(action + " 成功");
        refreshBoard();
        selectTask(state.currentTaskId, /*force*/ true);
      } catch (e) {
        toast(action + " 失败：" + e.message, "error");
      }
    };
  });
}

// ── Timeline (task detail + SSE) ─────────────────────────────────────

async function selectTask(taskId, force = false) {
  if (taskId === state.currentTaskId && !force) return;
  state.currentTaskId = taskId;
  state.timelineEvents = [];
  state.phaseStatus = {};
  state.activePhase = "";
  state.lastEventTs = 0;

  renderBoard();  // highlight active card
  document.getElementById("timeline-title").textContent = "loading…";

  let task;
  try {
    const r = await api("GET", `/api/v2/tasks/${taskId}`);
    task = r.task;
  } catch (e) {
    toast("加载任务失败：" + e.message, "error");
    return;
  }
  renderTimelineHead(task);
  renderArtifacts(task);
  renderLessons(task);
  toggleClarifyPanel(task);
  openSSE(taskId);
}

function renderTimelineHead(task) {
  document.getElementById("timeline-title").textContent =
    `${{ running: "🔄", succeeded: "✅", failed: "❌",
         paused: "⏸", abandoned: "⊘" }[task.status] || "·"} ` +
    (task.intent || "(no intent)");

  const meta = document.getElementById("timeline-meta");
  meta.textContent = `agent=${task.agent_id} · template=${task.template_id || "auto"}` +
                     ` · status=${task.status} · phase=${task.phase}`;

  // Phase rows.
  const phases = ["intake", "plan", "execute", "verify", "deliver", "report"];
  const status = task.phase;
  const statusIdx = phases.indexOf(status);
  const html = phases.map((p, i) => {
    let cls = "";
    if (task.status === "failed" && i === statusIdx) cls = "failed";
    else if (i < statusIdx || task.status === "succeeded") cls = "done";
    else if (i === statusIdx) cls = "active";
    state.phaseStatus[p] = cls;
    const sub = (task.retries && task.retries[p]) ? `retries=${task.retries[p]}` : "";
    return `<div class="phase-row ${cls}" data-phase="${p}">
      <span class="phase-name">${phaseLabel(p, cls)}</span>
      <span class="phase-sub">${sub}</span>
    </div>`;
  }).join("");
  document.getElementById("timeline-phases").innerHTML = html;
}

function phaseLabel(p, cls) {
  const icon = { done: "✅", active: "🔄", failed: "❌" }[cls] || "⬜";
  const names = {
    intake: "Intake", plan: "Plan", execute: "Execute",
    verify: "Verify", deliver: "Deliver", report: "Report",
  };
  return `${icon} ${names[p]}`;
}

function renderArtifacts(task) {
  const ul = document.getElementById("artifacts-list");
  ul.innerHTML = (task.artifacts || []).map(a =>
    `<li>[${a.kind}] <b>${escapeHtml(a.handle || "-")}</b>
     <span style="color:#64748b">${escapeHtml(a.summary || "")}</span></li>`
  ).join("") || "<li style='color:#94a3b8'>(暂无)</li>";
}

function renderLessons(task) {
  const ul = document.getElementById("lessons-list");
  ul.innerHTML = (task.lessons || []).map(le =>
    `<li>[${le.phase}] ${escapeHtml(le.issue)} → ${escapeHtml(le.fix || "-")}</li>`
  ).join("") || "<li style='color:#94a3b8'>(暂无)</li>";
}

function toggleClarifyPanel(task) {
  // Show clarify panel when phase=intake + status=paused (server set
  // clarification_pending on the task context — we infer from status+phase).
  const show = task.phase === "intake" && task.status === "paused";
  document.getElementById("clarify-panel").hidden = !show;
  if (show) {
    // Find the latest intake_clarification event for the question.
    const evt = [...state.timelineEvents].reverse()
      .find(e => e.type === "intake_clarification");
    document.getElementById("clarify-question").textContent =
      (evt && evt.payload && evt.payload.question) || "请补充信息。";
  }
}

// ── SSE ──────────────────────────────────────────────────────────────

function openSSE(taskId) {
  if (state.sse) { state.sse.close(); state.sse = null; }
  const url = `/api/v2/tasks/${taskId}/events` +
              (state.lastEventTs ? `?since=${state.lastEventTs}` : "");
  // EventSource doesn't support custom headers; auth falls back to the
  // same-origin session cookie (td_sess). For JWT-only setups this
  // requires a cookie fallback on the server.
  const es = new EventSource(url, { withCredentials: true });
  state.sse = es;
  es.addEventListener("error", () => {
    es.close();
    state.sse = null;
    const delay = Math.min(60000, 2000 * Math.pow(1.5, state.reconnectAttempt++));
    setTimeout(() => {
      if (state.currentTaskId === taskId) openSSE(taskId);
    }, delay);
  });
  es.addEventListener("open", () => { state.reconnectAttempt = 0; });
  es.addEventListener("stream_end", () => {
    es.close();
    state.sse = null;
    // Reload task summary to get final state.
    selectTask(taskId, /*force*/ true);
  });

  // Wildcard: attach the same handler to every known V2 event type.
  const eventTypes = [
    "task_submitted", "phase_enter", "phase_exit", "phase_retry", "phase_error",
    "intake_slots_filled", "intake_clarification",
    "plan_draft", "plan_approved",
    "step_enter", "step_exit", "tool_call", "tool_result", "progress",
    "artifact_created", "verify_check", "verify_retry", "lesson_recorded",
    "task_completed", "task_failed", "task_paused", "task_resumed", "heartbeat",
  ];
  for (const t of eventTypes) {
    es.addEventListener(t, e => onEvent(t, e));
  }
}

function onEvent(type, e) {
  let data;
  try { data = JSON.parse(e.data); } catch (_) { return; }
  state.lastEventTs = Math.max(state.lastEventTs, data.ts || 0);
  const row = { type, ts: data.ts, phase: data.phase, payload: data.payload };
  state.timelineEvents.push(row);
  renderEventLine(row);

  // Side effects per type.
  if (type === "phase_enter") {
    markPhase(data.payload.phase, "active");
  } else if (type === "phase_exit" && data.payload.ok) {
    markPhase(data.payload.phase, "done");
  } else if (type === "artifact_created") {
    // Pull fresh task to update artifacts panel.
    refreshCurrentTask();
  } else if (type === "intake_clarification") {
    document.getElementById("clarify-panel").hidden = false;
    document.getElementById("clarify-question").textContent =
      (data.payload && data.payload.question) || "";
  } else if (type === "task_completed" || type === "task_failed") {
    refreshCurrentTask();
  } else if (type === "lesson_recorded") {
    refreshCurrentTask();
  }
}

function markPhase(phase, cls) {
  const row = document.querySelector(`[data-phase="${phase}"]`);
  if (!row) return;
  row.classList.remove("active", "done", "failed");
  row.classList.add(cls);
  row.querySelector(".phase-name").textContent = phaseLabel(phase, cls);
}

async function refreshCurrentTask() {
  if (!state.currentTaskId) return;
  try {
    const r = await api("GET", `/api/v2/tasks/${state.currentTaskId}`);
    renderTimelineHead(r.task);
    renderArtifacts(r.task);
    renderLessons(r.task);
    toggleClarifyPanel(r.task);
    // Also update the board card.
    const idx = state.tasks.findIndex(t => t.id === r.task.id);
    if (idx >= 0) state.tasks[idx] = r.task; else state.tasks.unshift(r.task);
    renderBoard();
  } catch (_e) { /* silent */ }
}

function renderEventLine(row) {
  const box = document.getElementById("timeline-events");
  const div = document.createElement("div");
  div.className = "event-line";
  const ts = new Date((row.ts || 0) * 1000).toLocaleTimeString();
  const isErr = (row.type === "phase_error" || row.type === "task_failed");
  div.innerHTML =
    `<span class="ts">${ts}</span>` +
    `<span class="type ${isErr ? "err" : ""}">${row.type}</span>` +
    `<pre>${escapeHtml(oneLine(row.payload))}</pre>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function oneLine(obj) {
  if (!obj || typeof obj !== "object") return String(obj || "");
  try {
    const s = JSON.stringify(obj);
    return s.length > 200 ? s.slice(0, 197) + "…" : s;
  } catch (_) { return String(obj); }
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

init();
