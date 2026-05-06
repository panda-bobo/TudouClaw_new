// Chinese (default) dictionary.
//
// Keys are dot-separated namespaces (nav.xxx / chat.xxx / action.xxx)
// so ownership is obvious. Missing key → falls back to the provided
// fallback string or the key itself, never undefined.
//
// This file is the SOURCE OF TRUTH for Chinese. The English file
// mirrors every key — if you add a key here, add it there too.
// (Lint: tests/test_i18n_parity.py enforces this.)
window.TUDOU_I18N = window.TUDOU_I18N || {};
window.TUDOU_I18N.zh = {
  // ── Top bar ─────────────────────────────────────────────
  "theme.dark":            "深色主题",
  "theme.light":           "浅色主题",
  "theme.tooltip.toDark":  "当前：浅色主题 — 点击切换到深色主题",
  "theme.tooltip.toLight": "当前：深色主题 — 点击切换到浅色主题",
  "lang.switch":           "中 / EN",
  "lang.tooltip":          "切换中文 / English",

  // ── Sidebar nav ─────────────────────────────────────────
  "nav.dashboard":        "工作台",
  "nav.projects":         "项目与任务",
  "nav.orchestration":    "编排",
  "nav.agents":           "智能体",
  "nav.knowledge":        "知识与记忆",
  "nav.rolesSkills":      "角色与技能",
  "nav.toolsApprovals":   "工具与审批",
  "nav.integrations":     "集成与通知",
  "nav.settings":         "系统设置",
  "nav.admins":           "管理员",

  // ── Sidebar footer ──────────────────────────────────────
  "user.changePassword":  "修改密码",
  "user.logout":          "退出登录",

  // ── Generic action verbs ────────────────────────────────
  "action.save":          "保存",
  "action.cancel":        "取消",
  "action.delete":        "删除",
  "action.create":        "新建",
  "action.edit":          "编辑",
  "action.confirm":       "确认",
  "action.close":         "关闭",
  "action.refresh":       "刷新",
  "action.search":        "搜索",
  "action.import":        "导入",
  "action.export":        "导出",
  "action.add":           "添加",
  "action.remove":        "移除",
  "action.copy":          "复制",
  "action.send":          "发送",
  "action.retry":         "重试",
  "action.connectNode":   "连接节点",
  "action.addProvider":   "添加 Provider",

  // ── Chat header (per-agent) ─────────────────────────────
  "chat.soul":            "SOUL",
  "chat.think":           "Think",
  "chat.think.tooltip":   "自我总结：让 agent 复盘最近对话并把可复用的规则存入经验库",
  "chat.wake":            "Wake",
  "chat.wake.tooltip":    "唤醒：扫描所有项目里分配给该 agent 的未完成任务并继续执行",
  "chat.rag":             "RAG",
  "chat.rag.on":          "RAG 检索模式已开启：仅调 knowledge_lookup。点击关闭以走完整工具集",
  "chat.rag.off":         "RAG 检索模式：仅调 knowledge_lookup。点击开启",
  "chat.settings":        "Settings",
  "chat.inbox":           "收件箱",
  "chat.checkpoints":     "检查点",
  "chat.clear":           "Clear Chat",
  "chat.delete":          "Delete",
  "chat.placeholder":     "Direct Agent Tasking...",
  "chat.online":          "Online",
  "chat.thinking":        "发言中…",
  "chat.slashNewBadge":   "⟲ 新对话 (/new — 本轮不带聊天历史)",
  "chat.slashNewArmed":   "下一条消息将使用新对话（无历史）",
  "chat.slashNewDismiss": "点击取消 / 下一条消息将使用新对话",

  // ── Chat actions on message bubble ──────────────────────
  "bubble.speak":         "朗读此消息",
  "bubble.saveFile":      "保存为文件",
  "bubble.copy":          "复制",

  // ── Status labels ───────────────────────────────────────
  "status.idle":          "空闲",
  "status.busy":          "忙碌",
  "status.error":         "错误",
  "status.online":        "在线",
  "status.offline":       "离线",
  "status.enabled":       "已启用",
  "status.disabled":      "未启用",

  // ── Common panels / card titles ─────────────────────────
  "panel.runtime":         "运行时",
  "panel.modelAndDomain":  "模型 & 专业领域",
  "panel.capabilities":    "能力",
  "panel.growthAnalysis":  "成长 & 分析",
  "panel.taskQueue":       "任务队列",
  "panel.executionLog":    "执行日志",
  "panel.executionSteps":  "执行步骤",
  "panel.todos":           "TODOs",
  "panel.agentMessages":   "Agent 消息",
  "panel.domain":          "专业领域",
  "panel.loaded":          "已加载",
  "panel.off":             "未启用",

  // ── Think stats card (on agent chat page) ─────────────
  "think.reviewCount":     "复盘次数",
  "think.savedExperience": "沉淀经验",
  "think.latest":          "最近",

  // ── Task queue (right panel on chat page) ─────────────
  "tasks.noneActive":      "暂无进行中的任务",
  "tasks.allDone":         "全部完成",
  "tasks.archivedSuffix":  "条已归档",

  // ── Create / Edit Agent dialogs ─────────────────────────
  "agent.createTitle":    "创建新智能体",
  "agent.editTitle":      "编辑智能体",
  "agent.name":           "名称",
  "agent.role":           "角色",
  "agent.class":          "智能体分类",
  "agent.model":          "模型",
  "agent.provider":       "提供方",
  "agent.workdir":        "工作目录",

  // ── Knowledge ───────────────────────────────────────────
  "kb.sharedTitle":        "共享知识库",
  "kb.domainTitle":        "专业领域知识库",
  "kb.newEntry":           "新建知识条目",
  "kb.searchPlaceholder":  "搜索关键词…",
  "kb.noResults":          "知识库中未找到直接答案",

  // ── Page titles (top bar "view-title") ─────────────────
  "page.dashboard":        "工作台",
  "page.nodes":            "节点列表",
  "page.messages":         "消息",
  "page.channels":         "通道",
  "page.auditLog":         "审计日志",
  "page.llmProviders":     "LLM 提供商",
  "page.apiTokens":        "API Tokens",
  "page.config":           "配置",
  "page.nodeConfig":       "节点配置",
  "page.scheduled":        "定时任务",
  "page.mcpConfig":        "MCP 配置",
  "page.workflows":        "Workflow 编排",
  "page.selfImprove":      "Agent 自我改进",
  "page.adminManage":      "管理员管理",

  // ── Project-area tabs + strings ────────────────────────
  "tab.projectList":        "项目列表",
  "tab.meetings":           "群聊会议",
  "tab.taskCenter":         "任务中心",
  "tab.orchestration":      "编排可视化",
  "tab.workflowTemplates":  "Workflow 模板",
  "project.subtitle":       "组织多 agent 协作项目与任务",
  "project.new":            "新建项目",
  "project.create":         "创建项目",
  "project.none":           "还没有项目",
  "project.noneHint":       "创建一个项目，把 agent 组合成协作团队",

  // ── Knowledge & Memory sub-tabs ────────────────────────
  "tab.km.shared":          "共享知识库",
  "tab.km.private":         "专业领域知识库",
  "tab.km.rag":             "RAG 提供方",
  "tab.km.memory":          "Agent 私有记忆",

  // ── Roles & Skills sub-tabs ────────────────────────────
  "tab.rs.templates":       "角色 / 专业领域",
  "tab.rs.skillStore":      "技能商店",
  "tab.rs.skillForge":      "技能锻造",
  "tab.rs.selfImprove":     "学习闭环 / 经验沉淀",

  // ── Tools & Approvals sub-tabs ─────────────────────────
  "tab.tools.approvals":    "待审批 / 历史",
  "tab.tools.denylist":     "工具禁用清单",
  "tab.tools.mcpServers":   "MCP 服务器",

  // ── Settings sub-tabs ──────────────────────────────────
  "tab.settings.globalConfig": "全局配置",
  "tab.settings.providers":    "LLM 提供商",
  "tab.settings.llmTiers":     "LLM 档位",
  "tab.settings.nodeConfig":   "节点配置",
  "tab.settings.nodes":        "节点列表",
  "tab.settings.apiTokens":    "API Tokens",
  "tab.settings.auditLog":     "审计日志",
  "tab.settings.mcp":          "MCP",
  "tab.settings.channels":     "通道",
  "tab.settings.domains":      "专业领域",
  "tab.settings.policy":       "审批策略",
  "tab.settings.permissions":  "权限管理",

  // ── Permissions panel ─────────────────────────────────
  "perm.title":                "权限管理",
  "perm.subtitle":             "superAdmin 管理所有用户；可将特定 Agent / Node 授权给 admin；user 只能管理自己创建的 Agent。",
  "perm.onlySuper":             "此页面仅 superAdmin 可访问",
  "perm.pickUser":              "← 选择左侧的账号以编辑权限",
  "perm.superNote":             "拥有全部权限，无需单独勾选 Agent / Node。",
  "perm.delegatedAgents":       "可管理的 Agent",
  "perm.delegatedNodes":        "可管理的 Node",
  "perm.resetPassword":         "重置密码",
  "perm.resetPasswordPrompt":   "输入新密码（至少 6 位）：",
  "perm.passwordReset":         "密码已重置",
  "perm.saved":                 "权限已更新",
  "perm.saveFailed":             "保存失败",
  "perm.disable":               "禁用账号",
  "perm.enable":                "启用账号",
  "perm.disabled":              "已禁用",
  "perm.enabled":               "已启用",
  "perm.confirmDisable":        "禁用该账号？用户将无法登录。",
  "perm.confirmEnable":         "启用该账号？",
  "perm.newUser":               "新建用户",
  "perm.role":                  "角色",
  "perm.roleUserHint":          "（只能使用 agent）",
  "perm.roleAdminHint":         "（管理被授权节点上的 agent 与配置）",
  "perm.roleSuperHint":         "（管理一切）",
  "perm.userNote":              "user 只能使用 agent，不能管理。无需授权节点。",
  "perm.nodeScopeHint":         "admin 对被授权节点上的 agent 和该节点的配置拥有完整管理权限。",
  "perm.userCreated":           "用户已创建",
  "perm.userDeleted":           "用户已删除",
  "perm.createFailed":          "创建失败",
  "perm.deleteFailed":          "删除失败",
  "perm.confirmDelete":         "确认删除用户 \"{name}\"？此操作不可恢复。",
  "perm.usernamePasswordRequired": "用户名和密码不能为空",
  "perm.passwordTooShort":      "密码至少 6 位",

  // ── Destructive confirm dialogs ───────────────────────
  "abort.title":              "终止当前对话？",
  "abort.message":            "会停止 LLM 循环并 kill 任何正在运行的子进程。当前对话不会保存进度。",
  "abort.meetingTitle":       "终止会议讨论？",
  "abort.meetingMessage":     "会停止讨论循环并 kill 任何正在运行的子进程。",
  "abort.projectTitle":       "终止项目中的 Agent 运行？",
  "abort.projectMessage":     "会停止响应循环并 kill 任何正在运行的子进程。",
  "abort.confirm":            "终止",

  // ── Login page ───────────────────────────────────────
  "login.tagline":             "Multi-Agent AI Coordination Hub",
  "login.adminTab":            "管理员登录",
  "login.tokenTab":            "Token 登录",
  "login.username":            "用户名",
  "login.password":            "密码",
  "login.usernamePh":          "输入管理员用户名",
  "login.passwordPh":          "输入密码",
  "login.signIn":              "登录",
  "login.forgotToken":         "忘记 Token？",

  // ── System / generic ────────────────────────────────────
  "common.loading":        "加载中…",
  "common.noData":         "暂无数据",
  "common.empty":          "空",
  "common.error":          "出错了",
  "common.success":        "成功",
  "common.unknown":        "未知",

  // ── Inbox modal ────────────────────────────────────────
  "inbox.showRead":        "显示已读",
  "inbox.showAcked":       "显示已确认",
  "inbox.summary":         "共 {n} 条 (未读 {u})",
  "inbox.noMessages":      "暂无 agent，无法查看收件箱。",

  // ── Checkpoints modal ─────────────────────────────────
  "ckpt.scope":            "范围",
  "ckpt.status":           "状态",
  "ckpt.pickAgent":        "请在左上选择一个 Agent 来查看其检查点",
  "ckpt.totalCount":       "共 {n} 个检查点",
  "ckpt.confirmRestore":   "确定要恢复这个检查点吗？恢复后 digest 会在该 agent 下一轮对话里自动注入。",
  "ckpt.confirmArchive":   "归档这个检查点？归档后不再出现在默认列表。",
  "ckpt.anyStatus":        "(全部)",

  // ── Orchestration ─────────────────────────────────────
  "orch.openAgent":        "进入 Agent",

  // ── Agent Memory View modal ───────────────────────────
  "mem.title":             "记忆视图",
  "mem.subtitle":          "L1 短时 / L2 工作 / L3 长期 · 执行计划 · Transcript",
  "mem.compactBtn":        "压缩记忆",
  "mem.loading":           "加载中…",
  "mem.overview":          "记忆层级概览",
  "mem.l1Label":           "L1 短时记忆",
  "mem.l1Desc":            "当前对话窗口中的消息",
  "mem.l2Label":           "L2 工作记忆",
  "mem.l2Desc":            "历史对话压缩摘要",
  "mem.l3Label":           "L3 长期记忆",
  "mem.l3Desc":            "结构化语义知识",
  "mem.cat.contact":       "联系",
  "mem.cat.preference":    "偏好",
  "mem.cat.intent":        "意图",
  "mem.cat.reasoning":     "推理",
  "mem.cat.outcome":       "结果",
  "mem.cat.rule":          "规则",
  "mem.cat.reflection":    "反思",
  "mem.compactStrategy":   "<b>压缩策略：</b>L1（最近对话）超出窗口后 → 自动压缩为 L2 摘要（渐进式 Level 0→1→2，信息保留递减）→ 对话中提取结构化事实写入 L3（分 6 类：👤偏好 / 🎯意图 / 🧠推理 / ✅结果 / 📏规则 / 💡反思；👤偏好永久注入，不受检索 TopK 限制）",
  "mem.l2Title":           "L2 工作记忆 — 对话摘要 ({n})",
  "mem.compLevelDetailed": "详细",
  "mem.compLevelMedium":   "中等",
  "mem.compLevelBrief":    "精简",
  "mem.keywords":          "关键词",
  "mem.l3Title":           "L3 长期记忆 — 语义知识 ({n})",
  "mem.confidence":        "置信度",
  "mem.deleteOne":         "删除这条记忆",
  "mem.selectAll":         "全选",
  "mem.selectedCount":     "已选 {n}",
  "mem.deleteSelected":    "删除选中",
  "mem.clearAll":          "清空全部",
  "mem.engineOverview":    "引擎概览",
  "mem.currentPlan":       "当前执行计划",
  "mem.noSteps":           "无步骤",
  "mem.transcriptTitle":   "最近 Transcript (显示 {n} 条)",
  "mem.transcriptFiltered":" · 已过滤 {n} 条基础设施错误",
  "mem.noContent":         "无可展示内容",
  "mem.empty":             "该 agent 暂无记忆数据",
  "mem.loadFailed":        "加载失败：",
  "mem.openBtn":           "记忆视图",
};
