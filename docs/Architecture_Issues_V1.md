# TudouClaw V1 架构问题盘点

| 项 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 起草日期 | 2026-04-19 |
| 范围 | V1 当前架构在生产使用中暴露的架构级问题 |
| 用途 | 驱动 V2 重构决策；作为 V2 每条设计的"反面教材"依据 |
| 与之关联 | `docs/PRD_AGENT_V2.md` |

---

## 0. 摘要（TL;DR）

V1 在过去数月的迭代中，围绕 `Agent = 对话循环（chat loop）` 这个顶层抽象持续加功能。由于顶层抽象错位，每加一个需求都被迫打补丁，补丁之间又互相纠缠，最终形成如下 18 个架构级问题。

**核心结论**：**顶层抽象错了** —— Agent 应是"任务执行器"而不是"对话循环"。所有 18 个问题都是这个根本错位的衍生。

本文按 **严重度** 分级（P0 = 导致功能失效 / P1 = 导致数据或状态不一致 / P2 = 开发者体验与扩展困难），并对每条问题标注：现象、根因、已打补丁、补丁的副作用、V2 的处置方案。

---

## 1. 问题全景图

```
                  顶层抽象错位（Agent = chat loop）
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
  数据归属混乱          执行不可控            可观测性缺失
       │                    │                    │
  ┌────┼────┐          ┌────┼────┐          ┌────┼────┐
  ▼    ▼    ▼          ▼    ▼    ▼          ▼    ▼    ▼
 #1   #2   #3         #7   #8   #9        #13  #14  #15
 #4   #5   #6         #10  #11  #12       #16  #17  #18
```

每一个分支下的问题，都是同一个根因在不同层面的呈现。

---

## 2. 分类 A — 数据归属混乱（Source of Truth 不唯一）

### 问题 #1：`agent.messages` 顶层 vs `agent.profile.messages` 双份

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | Agent 对象上同时存在 `agent.messages`（顶层）和 `agent.profile.messages`（嵌套）。不同代码路径写入不同字段，持久化/加载不对称，导致同一 agent 在不同入口看到不同的对话历史。 |
| 根因 | 早期为"兼容旧持久化格式"保留了顶层 messages，后续引入 profile 做声明式配置时没有合并。 |
| 已打补丁 | `to_persist_dict` / `from_persist_dict` 里加 fallback 逻辑，读取时优先顶层，没有再读 profile。 |
| 补丁副作用 | 写入路径仍然分散；补丁只修读，未统一写。开发者看不出哪个是权威。 |
| V2 处置 | **消失**。messages 既不在 agent 顶层也不在 profile —— 它属于 `task.context.messages`。Agent 本身不持有运行时会话。 |

---

### 问题 #2：`agent.mcp_servers` 顶层 vs `agent.profile.mcp_servers`

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | 诊断菜小二"没有 MCP" 时，第一反应检查 `agent.mcp_servers` 是空，结论 "agent 没有任何 MCP"。但用户说"这个 agent 之前发过邮件" —— 再查 `agent.profile.mcp_servers`，赫然有 4 个 MCP 含 AgentMail。**两个字段意义一模一样，但值不同步。** |
| 根因 | 顶层字段是旧 API 遗留，profile 是新结构，但同时被 MCPManager 与持久化各写一边。 |
| 已打补丁 | `sync_all_agent_mcps()` 定时把 MCPManager 的绑定推回 `profile.mcp_servers`。顶层字段基本放弃不写了，但代码读取路径很多仍读顶层。 |
| 补丁副作用 | 读写路径对称性差；新开发者每次都要问"该读哪个"。 |
| V2 处置 | **消失**。MCP 绑定权威在 `mcp_manager`（共享层），AgentV2 只存 `capabilities.mcps` 作为**声明**，运行时每次从 mcp_manager 取。 |

---

### 问题 #3：`granted_skills` 三处分叉

| 项 | 内容 |
|---|---|
| 严重度 | **P0** |
| 现象 | Skill 授权状态同时存在三处：<br>① `agent.granted_skills`（list of skill_id，agent 自身持有）<br>② `skill_registry._installs[sid].granted_to`（registry 权威）<br>③ 物理文件 `agent_workspace/.claw/granted_skills/*.json`（runtime 发现用）<br>三者会不同步，导致 "Skill 撤销按钮点了没反应"、"Skill 数量卡片显示 5 实际只有 3" 等问题。 |
| 根因 | Skill 系统成熟过程中逐步加的三层存储，没有明确 single source of truth。 |
| 已打补丁 | ① 启动时 `hub._core.py` L174 做 orphan GC<br>② grant/revoke 两个 handler 加 orphan GC<br>③ 今天刚抽出 `hub.apply_skill_grant/revoke` 统一入口 |
| 补丁副作用 | 统一入口降低了未来分叉风险，但三处存储的事实仍在；一致性靠"每个写入都记得三处都改"这种约束，脆弱。 |
| V2 处置 | **权威唯一为 registry**。AgentV2 运行时从 registry 实时取，不缓存。pointer 文件作为"独立进程发现用"的派生视图，不是权威。 |

---

### 问题 #4：`skill_capabilities` vs 实际 skill 文件 vs granted_skills 三方失配

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | 撤销 skill 后，`agent.profile.skill_capabilities` 残留该 skill 的能力描述，导致提示词里仍包含 "你可以使用 pptx 工具"，但工具实际已不可调 —— agent 行为诡异。 |
| 根因 | `skill_capabilities` 是派生字段（从 granted_skills 产生的能力摘要），但无自动重算机制，靠每个修改入口手动 `remove_skill_from_workspace()` 清理。有入口漏调。 |
| 已打补丁 | 今天在 `portal_routes_post.py` 的 skill-pkgs revoke handler 加上 `remove_skill_from_workspace()`；又抽到 `hub.apply_skill_revoke()` 统一。 |
| 补丁副作用 | 如果未来有新的撤销入口不走 `apply_skill_revoke`，问题会重现。 |
| V2 处置 | **不存在** `skill_capabilities` 字段。能力描述在每次 Task 启动时由 capabilities_snapshot 实时生成，不作为持久化状态。 |

---

### 问题 #5：事件按 agent_id 聚合，不能按任务聚合

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | 问"这次发邮件做了哪些事"，系统答不上来。`AgentEvent` 按 agent_id 索引，一个 agent 所有对话、所有 task 混在一条时间线上。回放某次具体任务需要扫全表 + 靠时间窗口猜边界。 |
| 根因 | V1 没有 "task" 概念，事件自然只有 agent 一个聚合键。 |
| 已打补丁 | 无。这是设计层问题。 |
| 补丁副作用 | — |
| V2 处置 | **直接解决**。`task_events_v2` 按 task_id 索引；任意任务可 replay 完整过程。 |

---

### 问题 #6：工作流（WorkflowEngine）与 chat 的数据打架

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | `WorkflowInstance` 有自己的 state 机 + 上下文；`agent.chat()` 有自己的 messages。当一个 agent 既参与 workflow 又接用户直聊时，两个状态互相覆盖，出现"workflow 走到第 3 步，但用户直聊把 messages 截断了，workflow 再跑就找不到历史"。 |
| 根因 | Workflow 和 chat 是两套平行的"对话推进器"，共享同一个 `agent.messages`。 |
| 已打补丁 | 只能约定"workflow 中的 agent 不要给用户直聊"。代码未约束。 |
| 补丁副作用 | 约定容易破，且没有报错机制。 |
| V2 处置 | **统一为 Task**。Workflow 编排多个 Task；每个 Task 有独立 context.messages；不存在"工作流和对话混用同一 messages"的可能。 |

---

## 3. 分类 B — 执行不可控

### 问题 #7：agent 宣告意图后不执行（本次菜小二问题）

| 项 | 内容 |
|---|---|
| 严重度 | **P0** |
| 现象 | 用户发 "重新制作 PPTX 发邮件"，agent 回 "好的，我立即进行更深入的调查..." 然后**一个 token 也不调工具，整轮结束**。EXECUTION STEPS 面板显示 "Waiting for agent to start a task..." —— 系统视角任务已完成（无 tool_calls = 结束），用户视角任务完全没干。 |
| 根因 | `agent.chat()` 的推进力**只有 LLM 的 token 流**。LLM 停笔 → 系统停。没有"任务完成"的独立判据。加上本地 Qwen 35B 4-bit 在 agentic 场景的能力不足（易陷入"只宣告不执行"），这个架构缺陷就暴露。 |
| 已打补丁 | 无（本次问题仍未修复）。之前讨论过的 nudge 方案是补丁思路。 |
| 补丁副作用 | nudge 即使上线，下次换一种"停笔方式"（例如 LLM 开始提问而非宣告）又会失效。 |
| V2 处置 | **根治**。TaskLoop 是外层驱动，LLM 停笔不等于任务完成，exit 条件没满足 TaskLoop 会主动推；硬重试耗尽走 soft_fail → Report，**永远有可见输出**。 |

---

### 问题 #8：上下文压缩破坏执行记忆（肌肉记忆）

| 项 | 内容 |
|---|---|
| 严重度 | **P0** |
| 现象 | 菜小二之前另一次卡死：已经发过邮件，但压缩把 tool_use/tool_result 记录压没了，再跑时 agent "忘了之前干过" 开始重做，再次"我立即..."无限循环。 |
| 根因 | `_compress_context()` 的压缩策略对所有中间消息一视同仁地摘要化，不区分"执行事实"和"叙述性文本"。执行事实（"调用了 X 工具，返回了 Y"）一被压缩就丢失，agent 无历史参照。 |
| 已打补丁 | **已修复**：今天改了 `agent_llm.py` L2094 和 `agent.py` L3494（byte-identical）—— 区分 execution（tool_use/tool_result）vs narrative，execution 保留最近 20 条 inline，narrative 才摘要。加了日志。 |
| 补丁副作用 | 补丁本身正确，但问题是：**压缩逻辑分散在 agent.py 和 agent_llm.py 两处**（这又是旧的 V1 代码复制问题）；未来若只改一处，两边会漂移。 |
| V2 处置 | 压缩是 `TaskExecutor.on_context_pressure()` 内部方法，只存在一处；数据源是 `task.context.messages` 不是 `agent.messages`。 |

---

### 问题 #9：agent.py 和 agent_llm.py 大量函数重复实现

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | `_compress_context()` 在 `agent.py` L3494 和 `agent_llm.py` L2094 两处有字节级完全相同的实现。类似的还有若干方法。改一处忘改另一处 → bug 复现。 |
| 根因 | 历史重构过程中从 agent.py 切出 agent_llm.py 没切干净，留下双份。 |
| 已打补丁 | 修压缩时手动同步两处，用 `diff` 校验。 |
| 补丁副作用 | 每次改都要手动同步，依赖开发者记忆；迟早再次漂移。 |
| V2 处置 | V2 没有这个历史包袱；且 V2 禁止 import V1 这两个文件，未来 V1 漂移也伤不到 V2。 |

---

### 问题 #10：Skill revoke 不生效（方法名打错被 try/except 吞了）

| 项 | 内容 |
|---|---|
| 严重度 | **P0** |
| 现象 | 用户在 agent 的 skill 界面点"撤销"没反应，只有在 Skill Store 界面能撤。 |
| 根因 | `portal_routes_post.py` 的 skill-pkgs revoke handler 调的是 `hub.save_agents()` —— **这个方法不存在**（正确名是 `hub._save_agents()`，带下划线）。调用抛 AttributeError，但被外层 `try/except Exception` 吞掉，agent.granted_skills 的改动根本没落盘。刷新界面又读到老数据 → 用户以为"没反应"。 |
| 已打补丁 | **已修复**：方法名改对 + 统一 `hub.apply_skill_revoke()`。 |
| 补丁副作用 | 无，但暴露了一个更深的问题：**V1 代码充斥"静默 try/except Exception"**，吞错误让 bug 极难定位。 |
| V2 处置 | V2 编码规约：禁止裸 `except Exception: pass`；必须要么 log 要么 re-raise。具体错误类要在 docstring 声明。 |

---

### 问题 #11：没有任务级别的失败恢复

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | agent 正在跑一个多步任务，中途服务器重启。重启后 agent 对话历史还在，但"它上次做到哪一步、还差什么"的上下文完全丢失。用户得重新发指令。 |
| 根因 | 任务不是一等公民，没有显式状态机可以恢复。 |
| 已打补丁 | 无。 |
| 补丁副作用 | — |
| V2 处置 | `tasks_v2.phase + status` 是显式状态；重启扫 `status='running'` 的任务，可恢复或标记 `abandoned_on_restart`；用户能看到有哪些任务被中断。 |

---

### 问题 #12：LLM 能力档位耦合在 provider 配置

| 项 | 内容 |
|---|---|
| 严重度 | **P2** |
| 现象 | agent 想"对简单任务用便宜快速模型，对复杂规划用强模型"，无法在 V1 中声明性配置 —— 必须在 provider 层面切换。 |
| 根因 | agent 和 provider 一对一绑定，没有"能力档位"的中间抽象。 |
| 已打补丁 | 规划中的 RolePresetV2 有提到档位。 |
| 补丁副作用 | — |
| V2 处置 | `llm_tier` 是 capability 的一部分；每个 phase 可以选不同 tier（Intake 用便宜，Plan/Report 用强）。 |

---

## 4. 分类 C — 可观测性缺失

### 问题 #13：中间过程沉默

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | 用户视角，从发送指令到收到最终回复，中间**什么都看不到**。10 秒以内还能忍，几分钟的任务就会让用户以为卡死了。"正在分析 → 调用工具 → 发送邮件"这种进度叙述完全没有。 |
| 根因 | chat loop 只输出最终文本；中间 tool_use/tool_result 虽有 AgentEvent，但没有"人类可读的进度叙述"语义。前端也没地方渲染。 |
| 已打补丁 | EXECUTION STEPS 面板显示了 tool_call 名，但过于机械，用户依然看不懂。 |
| 补丁副作用 | — |
| V2 处置 | TaskTimeline UI 按 phase / step 展开；每个 phase 有进度叙述；机械事件（tool_call）和叙述事件（progress）并存。 |

---

### 问题 #14：Skill 数量统计与实际不符

| 项 | 内容 |
|---|---|
| 严重度 | **P1** |
| 现象 | Agent Dashboard 卡片显示 "SKILLS 5"，但右侧面板只列 3 条。数字来自 `len(agent.granted_skills)`，面板按 `registry` 过滤了已不存在的。Orphan 没清理。 |
| 根因 | #3 的衍生物。 |
| 已打补丁 | orphan GC + 启动时 reconcile。 |
| 补丁副作用 | 需要重启才清老数据。 |
| V2 处置 | 数量直接 `registry.grants_for(agent_id)` 取，不存在"agent 内缓存 vs 权威"的分叉。 |

---

### 问题 #15：事件没有层级，UI 无法展开/折叠

| 项 | 内容 |
|---|---|
| 严重度 | **P2** |
| 现象 | `AgentEvent` 是扁平列表，没有 phase / step 归属。UI 只能瀑布流展示，无法"按阶段折叠"或"按 step 分组"。长任务事件百条，用户找不到重点。 |
| 根因 | 事件模型是扁平的 `{ts, type, payload}`，没有层级字段。 |
| 已打补丁 | 无。 |
| 补丁副作用 | — |
| V2 处置 | TaskEvent 有 `phase` 字段；Execute 阶段内的事件都带 `step_id` payload；UI 自然形成层级树。 |

---

### 问题 #16：KPI 无法按任务聚合

| 项 | 内容 |
|---|---|
| 严重度 | **P2** |
| 现象 | 想统计"研究报告任务的平均耗时、平均重试次数、平均 tool_call 数"，V1 做不到 —— 因为任务本身没有实体。只能看"agent 级别的聚合指标"，粒度太粗。 |
| 根因 | #5 的衍生物。 |
| 已打补丁 | 无。 |
| 补丁副作用 | — |
| V2 处置 | `task_events_v2` 本身就是 KPI 数据源；聚合视图按需生成。 |

---

### 问题 #17：失败学习无闭环（ExperienceLibrary 写了没人读）

| 项 | 内容 |
|---|---|
| 严重度 | **P2** |
| 现象 | ExperienceLibrary 能记录 agent 的失败经验，但读取时机模糊 —— 系统没有在 "下次同类任务" 自动注入。开发者要手动配置"在 system_prompt 里加经验"。 |
| 根因 | 失败记录和任务没有关联 —— 只知道"某 agent 失败过"，不知道"什么情境下失败"。下次注入时也没有"类似任务"的匹配键。 |
| 已打补丁 | 无实质闭环。 |
| 补丁副作用 | — |
| V2 处置 | `task.lessons` 按 phase 记录；下次启动同 template_id 的任务时，Intake 阶段自动查询同 agent+template_id 的历史 lessons 注入上下文。 |

---

### 问题 #18：角色配置只有 prompt 一维（RolePreset 不够）

| 项 | 内容 |
|---|---|
| 严重度 | **P2** |
| 现象 | `coder` / `reviewer` / `architect` 等角色的差异只在 `system_prompt`。想要"会议助理不能 exec shell"、"研究员默认绑定 web_search MCP"等能力差异，V1 只能靠手动配置 —— 创建 agent 后一个个改 capabilities。 |
| 根因 | Role 只是 prompt 前缀，不是"配置模板"。 |
| 已打补丁 | 规划中的 RolePresetV2 要做 7 维配置，尚未落地。 |
| 补丁副作用 | — |
| V2 处置 | V2 用 YAML 任务模板（TaskTemplate）直接声明所需能力；Agent 在创建时按角色模板一次性装配；不需要独立的"RolePreset"层。 |

---

## 5. 根因归纳（为什么这 18 个问题都指向同一个方向）

### 5.1 一个根因的三种表现

```
            ┌──────────────────────────────────────────┐
            │  根因：Agent = 对话循环（chat loop）        │
            │  而不是                                    │
            │         Agent = 任务执行器                  │
            └──────────────────┬───────────────────────┘
                               │
       ┌───────────────────────┼───────────────────────┐
       ▼                       ▼                       ▼
  消息是 agent 的属性      LLM token 流是唯一驱动     事件按 agent 聚合
  （不是任务的属性）       （没有外层意志）             （不按任务聚合）
       │                       │                       │
 #1 #2 #3 #4 #6                #7 #8 #10 #11           #5 #13 #15 #16 #17
```

### 5.2 为什么打补丁解不了

- 问题 #1/#2/#3/#4 都是 **"一个概念多处存储"** —— 在不改变 "agent 持有会话状态" 这个前提下，每加一个状态就多一处分叉
- 问题 #7/#8/#11 都是 **"LLM 停笔等于系统停"** —— 不建立外层驱动（TaskLoop），nudge 之类的补丁只能 case-by-case 救
- 问题 #5/#13/#15/#16/#17 都是 **"没有任务实体"** —— 补丁只能在 agent 层做聚合，永远做不到"按任务聚合"

### 5.3 补丁的系统性副作用

| 补丁模式 | 代价 |
|---|---|
| "加一个同步函数保证两处状态一致" (#2) | 同步是定时任务，有窗口期；新的写入入口容易漏调 |
| "读时 fallback，优先 A 再读 B" (#1) | 读修了写没修；开发者不知道写哪个 |
| "启动时 reconcile" (#3 #14) | 必须重启才生效；线上长进程错过一次就累积脏数据 |
| "静默 try/except" (#10) | 错误被吞，bug 极难定位 |
| "文档 / 约定" (#6) | 没有代码层面约束，约定会被破坏 |
| "手动同步双份代码" (#9) | 依赖开发者记忆；迟早再次漂移 |

**这六种补丁模式，V2 都有意禁用或根治**（见下表）。

---

## 6. V2 对应处置一览表

| 编号 | 问题 | V1 补丁 | V2 处置 | 是否根治 |
|---|---|---|---|---|
| #1 | messages 双份 | 读时 fallback | messages 属 task，agent 无此字段 | ✅ |
| #2 | mcp_servers 双份 | 定时同步函数 | MCP 权威在 mcp_manager，agent 只声明 | ✅ |
| #3 | granted_skills 三份 | 三层 GC + 统一入口 | registry 权威，agent 运行时取 | ✅ |
| #4 | skill_capabilities 失配 | 入口补 remove | 无此派生字段，snapshot 生成 | ✅ |
| #5 | 事件按 agent 聚合 | 无 | task_events_v2 按 task_id | ✅ |
| #6 | workflow 与 chat 打架 | 约定 | 统一为 Task；Workflow 只编排 Task | ✅ |
| #7 | 宣告不执行 | 无（本次未修） | TaskLoop 外层驱动 + Report 兜底 | ✅ |
| #8 | 压缩吃记忆 | 区分 muscle/narrative | 同一策略内置于 Executor | ✅ |
| #9 | agent.py/agent_llm.py 双份 | 手动 diff 同步 | V2 无此历史，单一实现 | ✅ |
| #10 | revoke 方法名错 | 改名 + 统一入口 | 编码规约禁止静默 except | ✅ |
| #11 | 重启任务上下文丢失 | 无 | tasks_v2 持久状态可恢复 | ✅ |
| #12 | LLM 档位耦合 | 规划中 | capability.llm_tier，phase 级切换 | ✅ |
| #13 | 进度沉默 | 无 | TaskTimeline + progress 事件 | ✅ |
| #14 | Skill 数字不一致 | GC + reconcile | 直接读 registry，不缓存 | ✅ |
| #15 | 事件无层级 | 无 | phase + step_id 层级 | ✅ |
| #16 | KPI 无法按任务 | 无 | task_events_v2 聚合 | ✅ |
| #17 | 失败学习无闭环 | 无 | task.lessons 自动注入 | ✅ |
| #18 | Role 仅 prompt | 规划 RolePresetV2 | TaskTemplate YAML 直接声明 | ✅ |

---

## 7. 本次会话中已落地的 V1 补丁清单（供 V2 参考）

以下是在本次会话中对 V1 施加的补丁。V2 上线时这些补丁可以保留在 V1 代码里（维持 V1 稳定），**但 V2 代码不继承任何一条** —— V2 从根本上不需要它们。

| 文件 | 行号 | 补丁内容 | 对应问题 |
|---|---|---|---|
| `app/agent_llm.py` | ~2094 | `_compress_context` 区分 muscle/narrative | #8 |
| `app/agent.py` | ~3494 | 同上（byte-identical 同步） | #8 + #9 |
| `app/hub/_core.py` | ~174 | 启动 reconcile granted_skills | #3 #14 |
| `app/hub/_core.py` | ~1339 | 新增 `apply_skill_grant/revoke` 统一入口 | #3 #4 #10 |
| `app/server/portal_routes_post.py` | ~1614 | skill-store grant/revoke 委派 | #3 #10 |
| `app/server/portal_routes_post.py` | ~4497 | skill-pkgs grant/revoke 委派 | #3 #4 #10 |
| `app/server/static/js/portal_skill.js` | — | revoke 后刷新 agent 视图 | #14 |
| `app/templates/portal.html` | — | 部门字段选择器 | — |

---

## 8. 结论 & 建议

### 8.1 结论

1. V1 的 18 个架构问题都是**一个根因**的衍生：`Agent` 的顶层抽象错了
2. 已打 / 规划中的补丁能缓解 70% 的现象，**但不能根治任何一个**
3. 继续加补丁的边际成本越来越高；每个补丁都要了解前序补丁的耦合
4. V2 并行方案（见 `PRD_AGENT_V2.md`）对 18 个问题全部根治；代价是 7 天开发 + 双轨期

### 8.2 建议

- **停止给 V1 加新架构补丁**（`apply_skill_grant/revoke` 是最后一批；之后只修安全漏洞和数据修复）
- **启动 V2 并行开发**，按 PRD_AGENT_V2 的 7 天计划
- **定毕业标准**：M2 评审通过则新 agent 默认 V2；M8 V1 冻结
- **本次菜小二 "不执行" 问题不单独修**；等 V2 上线后让用户把菜小二克隆为 V2，该问题自动消失

### 8.3 如果不走 V2 的后果（风险预测）

- 每个新需求都要走一轮"加功能 → 暴露架构问题 → 打补丁 → 打补丁的补丁"，开发速度线性下降
- 数据分叉类问题（#1–#4）会继续以新的形式出现，每个都要 case-by-case 修
- 可观测性类问题（#13–#17）靠补丁只能做到 50% 效果，用户体验无法达标
- 1 年后重构成本 >> 现在重构成本 2–3 倍

---

*— 文档结束 —*
