# 迁移设计：用 OpenAI Agents SDK 替换 TudouClaw 自研 chat loop

**Status**: Design — not implemented yet  
**Author**: prompt-治理 后续工作  
**Date**: 2026-05-15  
**Trigger**: agent.py 17.5K 行 + 一晚上反复 patch 后用户判定"还不如开源产品"

---

## 1. 为什么要做

### 当前问题

`app/agent.py` 17,502 行 + `app/agent_execution.py` 3,465 行 + `app/llm.py` 4,765 行 = **25,732 行自研 chat 循环 / tool dispatch / streaming / parser / nudge / 错误处理**。

最近一周观察到的代价：

| 类别 | 例子 | 根因 |
|---|---|---|
| 多路径互相耦合 bug | text_delta filter 在 3 条 streaming 路径都要单独打 patch；漏 1 条就漏内容 | 每个 patch 都要在多个 codepath 里同步 |
| 沉默失败 | parser 失败只 `logger.debug(...)`，几小时没人发现 | 自研代码没标准 logging 约定 |
| Patch 互相冲突 | nudge 路径 `_log("message")` 跟主 MESSAGE emit 重复 → chat 显示双气泡 | 互相不知道对方做了什么 |
| 模型适配地狱 | mimo/qwen/glm/deepseek 每家 tool-call 输出格式不同，自己写 parser，BUILTIN_CLASSES 已经 6 个 | 自己跟着每个 vendor 后面跑 |

### 不动的部分

TudouClaw **真正独家**的价值，开源 SDK 没有等价物：

- 多 agent 编排 + portal UI（`team_create` / dispatch / 项目协作）
- 中文 persona + soul_md + cultivation 体系  
- L3 memory + wiki + skill registry
- Tool permissions UI（per-agent 勾选）
- agent state 持久化 + transcript 回放
- Project / Meeting / Channel context 系统

这些**必须保留**。要替换的只是**单 agent chat 循环这一层**。

---

## 2. 选型：OpenAI Agents SDK

| 比较项 | 选 | 备注 |
|---|---|---|
| AutoGen | ❌ | 强项是多 agent 协作，TudouClaw 已经有自己的；用它等于把核心价值丢掉换重复实现 |
| LangGraph | ❌ | 太通用，graph DSL 学习曲线陡 |
| Claude Agent SDK | ❌ | 强绑 Anthropic API，跟 TudouClaw 本地 mimo / qwen 不兼容 |
| **OpenAI Agents SDK** | **✅** | 就是干"单 agent chat 循环"这件事；MIT 免费；通过 LiteLLM / 自定义 ModelProvider 接任意 OpenAI-compat 后端 |

**许可 / 成本**：MIT，SDK 本身完全免费。LLM 后端继续用 TudouClaw 现有的 mimo / qwen / deepseek（OpenAI-compat），**不增加任何 API 账单**。

---

## 3. 现状 vs SDK 能力对照

### TudouClaw 现 chat 循环做的事

定位：`app/agent.py:Agent.chat()` 行 10657-13590（约 3K 行实际逻辑）。

| # | 子系统 | 行号 | 行数估算 | SDK 替代方案 |
|---|---|---|---|---|
| 1 | LLM 调用 + streaming | 11743-11910 | ~170 | ✅ `Runner.run_streamed()` 内置 |
| 2 | Tool dispatch | 12340-12700 | ~360 | ✅ `Agent(tools=[...])` + `function_tool` 装饰器 |
| 3 | 工具结果回填 LLM | 12500-12600 | ~100 | ✅ SDK 内置 |
| 4 | 多模态 (image_url) | 11645-11680 | ~35 | ✅ Sessions / Input list 支持 |
| 5 | 流式 chunk 累积 | 11695-11750 + agent_execution 整段 | ~600 | ✅ `RunResultStreaming` events |
| 6 | XML tool_call parser | tools_split + llm.py:_postprocess | ~700 | ✅ 用 LiteLLM 适配（mimo/Hermes 在 LiteLLM 里都已支持），或我们写一个 `Model` 子类做 vendor-specific 解析 |
| 7 | 重试 / 错误处理 | scattered | ~200 | ✅ SDK 有 `MaxTurnsExceeded` 等异常 + RunHooks |
| 8 | History summarization | 1140-1530 | ~390 | 🟡 **保留** — Sessions 太薄不够，9 段模板是我们独家 |
| 9 | 工具权限过滤（opt-in / capability skill）| _get_effective_tools | ~300 | 🟡 **保留** — 在塞给 SDK 之前过滤 `tools=[...]` |
| 10 | Persona / soul_md / 静态 prompt | _build_static_system_prompt | ~600 | 🟡 **保留** — 通过 `Agent(instructions=callable)` 动态注入 |
| 11 | Dynamic context (env / kb / plan / scheduled) | _inject_dynamic_context | ~400 | 🟡 **保留** — 同上，instructions callback 里动态拼 |
| 12 | Stall / plan-pending / tool-error / must-verify nudge | scattered nudge sites | ~600 | 🟡 **保留** — 用 `RunHooks.on_tool_end` + 注 input 实现 |
| 13 | Streaming leak filter（XML 漏到 chat）| stream filter @ 3 paths | ~150 | ✅ **不需要** — SDK 不会让 raw chunk 漏到 UI；structured events |
| 14 | MESSAGE 事件去重（双 bubble）| 4 nudge paths 各自 _log | ~30 | ✅ **消失** — SDK 单一 event stream，不会重复 |
| 15 | UI event 转发（text_delta / tool_call_start / etc）| portal task.push_event | ~200 | 🟡 **保留** — 把 SDK 的 stream events 翻译成 TudouClaw UI 协议 |
| 16 | History compaction trigger | _summarize_old_history 调用点 | ~80 | 🟡 **保留** — Sessions API 不做摘要，用现有 `_summarize_old_history` |
| 17 | Manual /compact endpoint | compact_memory | ~80 | 🟡 **保留** — 直接复用 |
| 18 | Skill auto-attach | matched_skills @ chat() | ~100 | 🟡 **保留** — 注入到 instructions callback |
| 19 | Plan / step tracking | plan_update tool | ~150 | 🟡 **保留** — 注册成 SDK 的 function_tool |
| 20 | Memory L3 extraction | flush_action_buffer + extract_facts | ~200 | 🟡 **保留** — RunHooks.on_agent_end 里调 |

**总计**：
- 约 **2,200 行可以删**（#1, 2, 3, 4, 5, 6, 7, 13, 14）—— SDK 内置等价物
- 约 **3,160 行保留为 TudouClaw 独家适配层**（其它 11 项）

---

## 4. 集成架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         Portal HTTP API                          │
│            (POST /api/portal/agent/{id}/chat etc.)              │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│        TudouClaw Agent state (persona/skill/memory/portal)       │
│                          ⟂  保留 ⟂                              │
│  - profile, persona, soul_md, granted_skills                    │
│  - L3 memory store, wiki                                        │
│  - allowed_tools / denied_tools                                 │
│  - transcript, events, project/meeting context                  │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│           ★ NEW: TudouClaw → SDK 适配层 (~500-700 行)            │
│                                                                  │
│  app/agent_runtime/sdk_adapter.py                                │
│    - SDKAgentRunner                                              │
│      .build_agent(tudou_agent) -> agents.Agent                   │
│      .run(user_message, on_event) -> async loop                 │
│                                                                  │
│  app/agent_runtime/instructions_builder.py                       │
│    - 动态生成 instructions (用现 _build_static_system_prompt     │
│      + _inject_dynamic_context, 包成 SDK callable)               │
│                                                                  │
│  app/agent_runtime/tool_registry.py                              │
│    - 把 TudouClaw tools.py 注册成 @function_tool                 │
│    - 用 _get_effective_tools() 决定 SDK Agent 拿到哪些           │
│                                                                  │
│  app/agent_runtime/event_bridge.py                               │
│    - 把 SDK stream events 翻译成 portal UI 协议                  │
│      (text_delta / tool_call_start / artifact_refs / ...)        │
│                                                                  │
│  app/agent_runtime/hooks.py                                      │
│    - RunHooks 实现:                                              │
│      on_tool_end -> 触发 nudge 检查 (stall/verify/error)         │
│      on_agent_end -> flush action buffer, extract L3 facts      │
│      on_llm_start -> inject dynamic context                     │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│             OpenAI Agents SDK (pip install openai-agents)        │
│                          ⟂  替换 ⟂                              │
│  - Agent + Runner                                                │
│  - Streaming via Runner.run_streamed()                          │
│  - Tool dispatch + retries                                       │
│  - OpenAIChatCompletionsModel (任意 OpenAI-compat URL)          │
│  - Built-in tracing (debugging gold)                             │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│          mimo / qwen / deepseek 本地或第三方 OpenAI-compat       │
│                          ⟂  不动 ⟂                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. 文件改动清单

### 新增（约 6 个文件 / ~700 行）

| 路径 | 作用 | 估算行数 |
|---|---|---|
| `app/agent_runtime/__init__.py` | 包初始化 | 20 |
| `app/agent_runtime/sdk_adapter.py` | 核心适配类 `SDKAgentRunner` | 250 |
| `app/agent_runtime/instructions_builder.py` | 动态 instructions callback (复用现 `_build_static_system_prompt`) | 100 |
| `app/agent_runtime/tool_registry.py` | TudouClaw tools → SDK function_tool 注册 | 150 |
| `app/agent_runtime/event_bridge.py` | SDK events → portal UI events 翻译 | 120 |
| `app/agent_runtime/hooks.py` | nudge / memory hooks | 100 |
| `tests/test_agent_runtime/*` | 单元测试 | ~300 |

### 修改（约 4 个文件）

| 路径 | 改什么 | 风险 |
|---|---|---|
| `app/agent.py:Agent.chat()` | 加一个 `use_sdk_runtime: bool` 开关，True 时调 `SDKAgentRunner.run()`，False 走旧路径 | 低（双 codepath 共存）|
| `app/agent.py:Agent.chat_async()` | 同上 | 低 |
| `app/server/handlers/agents.py` | 加 query param `?runtime=sdk` 让前端选择新旧 runtime | 极低 |
| `app/agent.py:Agent` dataclass | 加 `runtime_mode: str = "legacy"` 字段，per-agent 配置 | 低 |
| `requirements.txt` / `pyproject.toml` | 加 `openai-agents>=0.14.0` | 极低 |

### 删除（迁移完成、稳定运行 N 周后）

| 路径 | 原因 |
|---|---|
| `app/agent_execution.py:_stream_chat_to_response` 整段 | SDK Runner 替代 |
| `app/agent.py` 中 streaming chunk 累积 + 3 条 fallback 流式路径 | 同上 |
| `app/llm.py:_postprocess_xml_tool_calls` + 整个 `app/v2/bridges/tool_parsers/` | LiteLLM 适配 mimo/Hermes/etc |
| `app/agent.py:Agent.chat()` 的旧 codepath | 整个旧路径 |
| 估算可删：**~5,500 行** |

---

## 6. TudouClaw 独家、SDK 没有的、需要抽离保留的

**这些是真正的核心价值，不能丢**。迁移过程中要把它们从 `agent.py` 里**抽出来**做成独立模块，新旧 runtime 都能用：

### 6.1 高价值，必须保留

| 子系统 | 当前位置 | 抽到哪里 | 为什么 SDK 没有 |
|---|---|---|---|
| **9 段 history summary** | `app/agent.py:_summarize_old_history` | `app/memory/compaction.py` | SDK 的 Sessions 只是 K-V，不会自动摘要；Claude Code 9 段模板是我们独家 |
| **STRUCTURED_FACTS extractor** | `_extract_structured_facts` | 同上 | 任何 SDK 都没有"代码确定性事实抽取"概念 |
| **Persona + soul_md + cultivation** | `_build_static_system_prompt` + `system_prompt.py` | `app/persona/builder.py` | SDK instructions 是字符串，没有多层 persona 体系 |
| **Skill registry / auto-attach** | `prompt_packs` + `granted_skills.md` 流程 | `app/skills/registry.py`（已存在，扩功能）| SDK 工具是静态注册，没有 per-message skill 匹配 |
| **Tool permissions UI + per-agent allowed_tools** | `_get_effective_tools` + portal `_eaInfraTools` | `app/permissions/filter.py` | SDK 没有 per-agent UI 概念 |
| **explicit-opt-in 检测** (`_user_explicitly_requests_retrieval` / `_wiki_write`) | `agent.py:151+` | `app/permissions/intent.py` | SDK 不区分用户 intent |
| **Stall / verify / tool-error nudge** | 4 个 nudge 块在 chat loop 里 | `app/runtime/nudges.py` | SDK 有 hooks，但具体策略是我们独家 |
| **L3 memory + flush_action_buffer** | `app/core/memory.py` + agent.py 调用点 | 现位置即可，只改触发点 | SDK Sessions 不做事实抽取 |
| **Project / Meeting / Channel context** | scattered | `app/context/builder.py`（已部分存在）| SDK 不知道这些概念 |
| **MCP integration** | mcp_call 工具 + auth | 现位置 | SDK 有自己的 MCP 客户端，可以并行 |
| **Multi-agent dispatch (team_create / handoff)** | scattered | `app/orchestration/dispatch.py` | SDK 的 Handoffs 太弱（单线 delegate），我们要的是异步多 agent 协作 |
| **Transcript / events / artifact 持久化** | `agent.events`, `transcript.py` | 现位置 | SDK 有 tracing 但不是持久化 store |

### 6.2 可以"借助 SDK 增强"的部分

| 现 TudouClaw 机制 | 用 SDK 能力增强成 | 收益 |
|---|---|---|
| 自研 XML parser (FunctionXMLParser 等 6 个) | LiteLLM provider — 已支持 mimo/qwen/glm/hermes/deepseek 全套 | 删 ~700 行 + 不再追每个 vendor 的 quirk |
| 自研 streaming chunk 累积 + 3 条 fallback path | `Runner.run_streamed()` event stream | 删 ~600 行，永远不会再发生"3 条路径不同步" |
| 自研 retry / context overflow 处理 | SDK MaxTurnsExceeded + RunHooks | 标准化错误模式 |
| 调试日志 (PAYLOAD-BREAKDOWN / TOOL_SET / XML_PARSE) | SDK 内置 tracing UI（OpenAI 平台上可视化看每次 run）| Debug 速度上一个台阶 |
| Tool dispatch 的并行控制 (PARALLEL_SAFE_TOOLS) | SDK 自动并行 tool calls | 不用自己写线程池 |

### 6.3 需要新建（旧代码没做对的部分）

| 新模块 | 作用 |
|---|---|
| `app/runtime/intent_classifier.py` | 用户消息 intent 分类（action / question / verify / retrieval / wiki-write）— 把现散落各处的检测器统一 |
| `app/runtime/observability.py` | 收敛所有 logger 输出到统一格式，配 SDK tracing |

---

## 7. 迁移步骤（渐进，可中断）

### Phase 0 — 准备（1 天）
- [ ] `pip install openai-agents` 加进 `requirements.txt`
- [ ] 跑 SDK 自带 `examples/model_providers/custom_example_provider.py` 接通 mimo，确认 200% 可用
- [ ] 写一份"SDK API 使用 cheatsheet"放 `docs/SDK_CHEATSHEET.md`

### Phase 1 — 适配层骨架（2 天）
- [ ] 新建 `app/agent_runtime/` 目录 + 6 个空文件 + tests 目录
- [ ] 写 `SDKAgentRunner.run(tudou_agent, user_text, on_event)` 最小可跑版本
  - 用现有 `_build_static_system_prompt` 生成 instructions
  - 用现有 `_get_effective_tools` 决定 SDK 拿哪些工具
  - 把现有 `tools.py` 里的工具用 `@function_tool` 包一遍
  - 流式 events 转 portal `text_delta` / `tool_call_start` 等
- [ ] 单元测试：mock 一个简单 user → assistant → tool → result 流，验证 portal 看到的事件序列跟旧 runtime 一致

### Phase 2 — 单 agent PoC（1 天）
- [ ] `Agent` dataclass 加 `runtime_mode: str = "legacy"` 字段
- [ ] `Agent.chat()` 入口分流：`runtime_mode == "sdk"` → 调 `SDKAgentRunner`，否则旧路径
- [ ] 给一个**测试 agent**（不是刘老师）打开 `runtime_mode = "sdk"`，跑 5 个典型场景手测
- [ ] 对比新旧两个 runtime 的输出 / 行为 / token 用量

### Phase 3 — 迁移核心子系统（3-5 天，并行）
- [ ] 把 nudge 逻辑从 `chat()` 里抽到 `app/agent_runtime/hooks.py`，新 runtime 用 RunHooks 调；旧 runtime 也改成调这里
- [ ] L3 memory hook 同样
- [ ] history compaction 触发逻辑迁移
- [ ] artifact / FileCard 事件桥接

### Phase 4 — 灰度 + 切流量（1-2 周）
- [ ] portal UI 加"runtime mode"开关（admin per-agent 选）
- [ ] 默认所有新 agent 用 SDK runtime，旧 agent 保持 legacy
- [ ] 跑 1 周，观察 / 对比
- [ ] 全切换 SDK，legacy 路径保留作 fallback 1 个月

### Phase 5 — 删旧代码（迁移稳定 4 周后）
- [ ] 删 `_stream_chat_to_response` 全部
- [ ] 删 `app/v2/bridges/tool_parsers/` 全部
- [ ] 删 `_postprocess_xml_tool_calls` + 相关测试
- [ ] 删 chat() 的旧 codepath
- [ ] 估算最终 LOC：`agent.py` 17.5K → ~12K，`agent_execution.py` 3.5K → ~500，`llm.py` 4.7K → ~2K

**累计可删 ~7-8K 行 + 砍掉一整套 vendor parser 维护负担**。

---

## 8. 风险 + 应对

| 风险 | 概率 | 应对 |
|---|---|---|
| LiteLLM 对 mimo 支持不完美 | 中 | Phase 0 必须验证；不行就用 `OpenAIChatCompletionsModel` + 自己包一个 `Model` 子类（仍比当前 6 个 parser 简单） |
| SDK API 在演进，breaking changes | 中 | pin 版本（>=0.14.0,<0.15.0），关注 release notes |
| SDK 不支持某个 TudouClaw 特性（多 agent dispatch 跨进程）| 高 | 那个特性留在 TudouClaw 适配层不下沉；SDK 只管 single agent |
| 新旧 runtime 行为微妙差异 → 用户体感倒退 | 高 | 灰度 + per-agent 开关，可任何时候回滚 |
| 自己写的 nudge 逻辑迁移过去走样 | 中 | 先把 nudge 抽成独立模块（旧路径也用），迁移时只换调用方 |
| 文档 / 测试要重写 | 必然 | 计划里包含了 |

---

## 9. 决策点 + 下一步

**做不做的判断标准**（可以现在就答）：

1. **如果 TudouClaw 是认真要长期演进的产品** → 应该做。当前 25K 行自研 chat loop 的维护成本会持续吞噬迭代速度。
2. **如果 TudouClaw 是个 demo / 短期项目** → 不做。打补丁继续。
3. **如果不确定** → 做 Phase 0 + Phase 1 + Phase 2 PoC（4 天），看效果再决定后续。Phase 0-2 是低风险投入，最坏情况是浪费 4 天 + 学到 SDK，没有破坏性。

**我的建议**：做 Phase 0-2 的 PoC，用一个**新建的测试 agent**（不动刘老师 / 小新等线上 agent）做 spike，1 周内拿出能跑的 demo，让你实际对比新旧体验，再决定是否推 Phase 3+。

---

## 10. 立即可做的小事（PoC 之前的预备）

不需要等 SDK 决策，**这些动作本身就是好的卫生**，做了就受益：

- [ ] 把 `_user_asked_for_verification` / `_user_explicitly_requests_retrieval` / `_user_explicitly_requests_wiki_write` 三个 detector 抽到 `app/runtime/intent.py` 单独一个文件（现在散在 `agent.py` 里）
- [ ] 把 4 个 nudge 块（stall / plan-pending / tool-error / must-verify）抽到 `app/runtime/nudges.py`，`chat()` 里只调 `nudges.evaluate(state) -> Optional[Nudge]`
- [ ] 把 streaming text_delta 过滤抽到 `app/runtime/stream_filters.py`（现 3 条 fallback 路径各自有一份重复代码）

这些 refactor 完成后，无论后续走 SDK 还是不走，`agent.py` 都会瘦 ~1500 行 + 这几块逻辑可独立测试。**这是无后悔投入。**

---

## 11. 不做的话

承认现状：mimo / qwen 这种弱 planner 在自研 chat loop 上会持续暴露问题，每次 patch 都有可能引入新 bug（这周已经出现 3 次回滚）。

不动的成本是 **持续 patch / 持续回滚 / 用户信心持续磨损**。

动的成本是 **3-4 周 focused 投入，期间业务功能放慢**。

你判断哪个划算。
