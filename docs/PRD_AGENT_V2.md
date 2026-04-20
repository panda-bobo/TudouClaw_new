# Agent V2 Core — 顶层设计 PRD

| 项 | 值 |
|---|---|
| 文档版本 | v0.1 (Draft) |
| 起草日期 | 2026-04-19 |
| 状态 | 待评审 |
| 作用范围 | TudouClaw 多 Agent 框架的下一代核心 |
| 实施策略 | **并行开发 V2**，V1 零改动，前端切换，毕业后废弃 V1 |

---

## 0. TL;DR

当前 V1 架构把 `Agent = 对话循环` 作为顶层抽象，导致所有需求（多步任务、进度可视、质检、失败学习）都变成补丁。

V2 以 **Task（任务）为一等公民** 重建核心，`Agent` 瘦身为"任务执行器身份"，`对话`降级为 Task 内部的上下文机制。

整套 V2 只引入 **5 个核心类 + 3 张 SQLite 表 + 6 个阶段**，物理隔离在 `app/v2/` 目录。前端加一个 V1/V2 Tab 切换。工期 7 天。定义明确的毕业标准，避免永久双轨。

---

## 1. 背景与问题陈述

### 1.1 V1 的根因问题

V1 的顶层 API 是 `Agent.chat(user_message) -> assistant_message`。所有能力都围绕这个"一问一答循环"添加：

- **多步任务** → 靠 chat loop 连续调用 tool，LLM 不调 tool 就结束整轮
- **进度可视** → 靠 ad-hoc 的 `AgentEvent` 列表，前端被动订阅
- **上下文管理** → 靠 `_compress_context()` 修剪 `agent.messages`
- **能力配置** → 靠 `agent.profile.mcp_servers` / `granted_skills` 等字段
- **质检** → 规划中要加 `QualityGate` 外挂

这造成：

1. **任务不是一等公民** —— 没有"这个用户诉求是否已完成"的概念；LLM 停笔就等于系统停工
2. **消息错位** —— `messages` 是 agent 顶层属性，跨任务污染，压缩策略两难
3. **事件零散** —— 事件按 agent 聚合，不按任务聚合，无法回放一次任务的全过程
4. **补丁堆叠** —— 今天打 orphan GC、明天打 skill helper、后天要打 nudge，每个都不错但整体没有统一主线

### 1.2 为什么选"并行 V2"而不是"重构 V1"

| 路径 | 回归风险 | 回滚能力 | 设计自由度 | 合计工期 |
|---|---|---|---|---|
| 重构 V1（破坏性） | 高 | 难 | 被兼容拖累 | 8 天 |
| **并行 V2（本方案）** | **零** | **关 Tab 即可** | **完全干净** | **7 天** |
| 继续打补丁 | 中 | N/A | 零 | 永远做不完 |

并行 V2 的核心代价是"短期代码翻倍 + 双轨化陷阱"。本 PRD 用两个机制消解：
- 第 13 节的**共享边界规矩**限制 V2 向 V1 偷懒
- 第 14 节的**毕业标准**强制 V2 最终独立并替换 V1

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 验收 |
|---|---|---|
| G1 | Task 是一等公民，从接收到汇报全程可追溯 | 任意任务可按 task_id 回放完整事件流 |
| G2 | 阶段显式、exit 条件机器可判定 | 不靠"LLM 自觉停笔"推进流程 |
| G3 | 进度对用户实时可见 | 每个 phase/step 进出、每个 tool_call/result 都有事件推前端 |
| G4 | 失败自动重试 + 软兜底，不会沉默死掉 | 硬重试 3 次后必有用户可见输出 |
| G5 | V1 零改动、可并行运行、可前端切换 | 同一 portal 同时看两套 agent，互不影响 |
| G6 | 毕业后 V2 完全独立，V1 可冻结 | 6 个月内所有新 agent 走 V2 |

### 2.2 非目标（V2 Core 不做）

- ❌ 不重做 LLM Provider 层（复用 V1）
- ❌ 不重做 Skill Registry / MCP Manager（复用 V1）
- ❌ 不新增独立的 QualityGate / KPI / ExperienceLibrary 子系统（全部内聚到 Task/Phase 内）
- ❌ 不新增独立的 RolePresetV2（V2 agent 直接用 YAML 声明能力，没有"preset"双层概念）
- ❌ 不做多租户隔离（单用户本地部署仍是首选场景）
- ❌ 不做分布式任务调度（单机单进程足够）

---

## 3. 设计原则（7 条）

| # | 原则 | 落地含义 |
|---|---|---|
| P1 | **Task 一等公民** | `messages`、`events`、`artifacts` 全部属于 Task，不属于 Agent |
| P2 | **阶段显式** | 6 个固定 phase，状态机；不允许隐式推进 |
| P3 | **Exit 条件机器可判定** | 不用自然语言判断"是否完成"；用 schema、规则、句柄 |
| P4 | **失败有限且有兜底** | 每个 phase 硬重试 ≤ N 次；耗尽后软失败而非沉默 |
| P5 | **事件是主线程，日志是副产品** | 前端 UI 直接由事件流驱动 |
| P6 | **物理隔离，共享底层** | V2 在 `app/v2/` 目录，禁止 import V1 顶层对象 |
| P7 | **极简核心** | 5 个类、3 张表、6 个 phase，不再新增顶层抽象 |

---

## 4. 术语表

| 术语 | 定义 |
|---|---|
| **Task** | 一个用户意图单元。从接收到汇报的完整闭环。V2 的一等公民。 |
| **Phase** | Task 的阶段。共 6 个：Intake / Plan / Execute / Verify / Deliver / Report |
| **Step** | Plan 里的一个子动作，Execute 阶段按 step 推进 |
| **Artifact** | Task 产出物的句柄。文件路径、邮件 ID、RAG 条目 ID 等。 |
| **Lesson** | 失败复盘条目。写入 task.lessons，供后续同意图任务查询。 |
| **Template** | YAML 声明的任务模板，规定 phase 内的 prompts / tools_hint / exit_check |
| **TaskLoop** | 推进 Task 状态机的执行器。不是 LLM 驱动，是阶段驱动。 |
| **TaskExecutor** | Execute 阶段内部的 LLM + tool 循环封装。 |
| **TaskEventBus** | 单向事件流，SSE 推送前端。 |
| **Capability** | V2 agent 声明的能力集合：skills / mcps / tools / llm_tier |

---

## 5. 逻辑架构

### 5.1 整体分层图

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 4  —  前端 UI                                                 │
│                                                                      │
│   ┌──────────────────┐   ┌──────────────────┐                       │
│   │  V1 Classic Tab  │   │  V2 Tasks Tab    │  ← portal.html 顶部切换│
│   │  (原聊天窗口)     │   │  TaskBoard +     │                       │
│   │                  │   │  TaskTimeline +  │                       │
│   │                  │   │  TaskConsole     │                       │
│   └──────────────────┘   └──────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
           ▲                            ▲
           │ /api/agents/*              │ /api/v2/*  +  SSE
           │                            │
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 3  —  HTTP 接入                                               │
│                                                                      │
│   ┌──────────────────┐   ┌──────────────────────────────────────┐   │
│   │  V1 Routes        │   │  V2 Routes (app/v2/api/)            │   │
│   │  portal_routes_* │   │    routes.py   (REST)                │   │
│   │                  │   │    sse.py      (事件流)                │   │
│   └──────────────────┘   └──────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
           ▲                            ▲
           │                            │
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 2  —  领域核心                                                │
│                                                                      │
│  ┌───────────────────────┐    ┌──────────────────────────────────┐  │
│  │  V1  (冻结)            │    │  V2 Core (app/v2/core/)          │  │
│  │                       │    │                                  │  │
│  │  Agent (chat loop)    │    │   ┌──────────────────────────┐   │  │
│  │  Hub (coordination)   │    │   │  AgentV2 (瘦壳)           │   │  │
│  │  WorkflowEngine       │    │   └──────┬───────────────────┘   │  │
│  │                       │    │          │ submit_task()         │  │
│  │                       │    │          ▼                       │  │
│  │                       │    │   ┌──────────────────────────┐   │  │
│  │                       │    │   │  Task (数据一等公民)       │   │  │
│  │                       │    │   └──────┬───────────────────┘   │  │
│  │                       │    │          │ 喂给                   │  │
│  │                       │    │          ▼                       │  │
│  │                       │    │   ┌──────────────────────────┐   │  │
│  │                       │    │   │  TaskLoop (推进器)         │   │  │
│  │                       │    │   │  intake→plan→execute→    │   │  │
│  │                       │    │   │  verify→deliver→report   │   │  │
│  │                       │    │   └───┬───────────┬──────────┘   │  │
│  │                       │    │       │ execute   │ emit         │  │
│  │                       │    │       ▼           ▼              │  │
│  │                       │    │   ┌─────────┐ ┌────────────┐    │  │
│  │                       │    │   │ Task-   │ │ TaskEvent  │    │  │
│  │                       │    │   │Executor │ │ Bus        │    │  │
│  │                       │    │   │ (LLM+  │ │            │    │  │
│  │                       │    │   │  tool) │ │            │    │  │
│  │                       │    │   └────┬────┘ └─────┬──────┘    │  │
│  └───────────┬───────────┘    └────────┼────────────┼──────────┘   │
│              │                         │            │               │
│              │                         │            │               │
│              ▼                         ▼            ▼               │
│  ┌──────────────────┐        ┌──────────────────────────────────┐  │
│  │  V1 持久化        │        │  V2 持久化 (TaskStore)            │  │
│  │  agents 表        │        │  agents_v2 / tasks_v2 /           │  │
│  │  agent_events 表  │        │  task_events_v2 表                │  │
│  └──────────────────┘        └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
           ▲                                       ▲
           │                                       │
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1  —  共享服务层（V1 / V2 都用）                               │
│                                                                      │
│   app.llm          app.skills.registry    app.skills.store          │
│   app.mcp.manager  app.auth               app.runtime_paths         │
└─────────────────────────────────────────────────────────────────────┘
           ▲                                       ▲
           │                                       │
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 0  —  基础设施                                                 │
│                                                                      │
│   SQLite 文件   ·   本地文件系统   ·   LLM Providers                  │
│   （~/.tudou_claw/tudou.db — 同文件，表前缀区分 V1/V2）               │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 模块依赖图（V2 内部）

```
               ┌────────────────┐
               │  v2/api/       │
               │  routes.py     │
               │  sse.py        │
               └────────┬───────┘
                        │ 依赖
          ┌─────────────┼───────────────┐
          ▼             ▼               ▼
  ┌──────────────┐ ┌──────────┐ ┌──────────────┐
  │ v2/agent/    │ │ v2/core/ │ │ v2/core/     │
  │ agent_v2.py  │ │ task.py  │ │ task_events  │
  └──────┬───────┘ └────┬─────┘ └──────┬───────┘
         │              │               │
         │ 创建          │ 推进           │ 发布
         ▼              ▼               ▼
     ┌─────────────────────────────┐
     │   v2/core/task_loop.py      │
     │   （唯一的业务编排中枢）       │
     └──────────────┬──────────────┘
                    │ 调用
          ┌─────────┴──────────┐
          ▼                    ▼
  ┌──────────────────┐  ┌──────────────────┐
  │ v2/core/         │  │ v2/core/         │
  │ task_executor.py │  │ task_store.py    │
  │ （LLM + 工具）    │  │ （SQLite 读写）   │
  └────────┬─────────┘  └────────┬─────────┘
           │                     │
           │ 借用（仅限 Layer 1）  │ 借用
           ▼                     ▼
  ┌──────────────────────────────────────────┐
  │  Layer 1 共享：app.llm / app.skills /    │
  │              app.mcp / app.auth          │
  └──────────────────────────────────────────┘
```

**依赖规则**：
- 上层只能依赖下层，下层不得反向依赖
- 同层模块尽量不互相依赖（`task.py` 是纯数据，被所有人依赖；其他模块水平独立）
- V2 全部模块**不得**依赖 V1 的 Layer 2（`app.agent` / `app.hub._core` 等），违反则 pre-commit 拒绝（见 §13.2）

### 5.3 关键流程 — 时序图

#### 5.3.1 任务提交 & 推进

```
User          Frontend        API         AgentV2      TaskLoop     TaskStore    EventBus
 │              │              │            │            │             │            │
 │─提交意图─────▶│              │            │            │             │            │
 │              │─POST /tasks─▶│            │            │             │            │
 │              │              │─submit_task▶│            │             │            │
 │              │              │            │─new Task──▶│             │            │
 │              │              │            │            │────save────▶│            │
 │              │              │            │            │────────publish task_submitted▶│
 │              │              │            │            │─run()──▶                 │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=intake─────────────▶│
 │              │              │            │            │─call LLM (Intake prompt) │
 │              │              │            │            │─fill slots               │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=plan───────────────▶│
 │              │              │            │            │─call LLM (Plan prompt)   │
 │              │              │            │            │─validate JSON schema      │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=execute────────────▶│
 │              │              │            │  ┌─循环 step─────────────┐            │
 │              │              │            │  │ TaskExecutor.run_step│            │
 │              │              │            │  │ ─ LLM + tool × N     │            │
 │              │              │            │  │ ─ emit tool_call/res │            │
 │              │              │            │  │ ─ check step.exit    │            │
 │              │              │            │  └──────────────────────┘            │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=verify─────────────▶│
 │              │              │            │            │─对每条 verify_rule 检查   │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=deliver────────────▶│
 │              │              │            │            │─落盘/发邮件/入 RAG         │
 │              │              │            │            │            │            │
 │              │              │            │            │─phase=report─────────────▶│
 │              │              │            │            │─LLM 生成汇报              │
 │              │              │            │            │─publish task_completed───▶│
 │              │              │            │            │────save────▶│            │
 │              │              │            │            │            │            │
 │              │◀───SSE events (全过程) ─────────────────────────────────────────────│
 │◀─展示时间线───│              │            │            │            │            │
```

#### 5.3.2 阶段失败 & 硬重试 & 软兜底

```
TaskLoop              PhaseHandler         Store         EventBus
   │                       │                 │              │
   │──dispatch(execute)───▶│                 │              │
   │                       │                 │              │
   │                       │──step 2 fail    │              │
   │                       │                 │              │
   │◀──return False (exit not met)           │              │
   │                                                         │
   │──record_retry(execute)─────────────────▶│              │
   │   retries[execute] = 1 (≤ 3)                            │
   │──publish phase_retry────────────────────────────────────▶│
   │                                                         │
   │──dispatch(execute) (重跑)─▶│           (带 feedback 注入)│
   │                           │             │              │
   │                           │──step 2 fail 再一次         │
   │◀──return False                                          │
   │                                                         │
   │  ... 重试到 retries[execute] = 3 耗尽                    │
   │                                                         │
   │──soft_fail(execute)                                     │
   │   status = FAILED                                       │
   │   add_lesson(phase=execute, issue=...)                  │
   │   phase = REPORT  ← 关键：仍然去 Report，保证用户有可见输出 │
   │                                                         │
   │──dispatch(report)────────▶│                             │
   │                           │  用失败模板生成"本次任务失败 + 原因"│
   │◀──True                                                  │
   │──publish task_failed─────────────────────────────────────▶│
```

**关键设计**：**Report 阶段永远会执行**，即使前面任何阶段耗尽重试 —— 用户永远不会看到"agent 沉默不动"。

#### 5.3.3 事件流分发

```
Phase 内部                TaskEventBus         TaskStore         SSE 订阅者
   │                          │                   │                  │
   │──publish(evt)───────────▶│                   │                  │
   │                          │──append_event────▶│                  │
   │                          │                  (先落库)             │
   │                          │──for h in subs──▶ handler_1─────────▶│
   │                          │                   handler_2─────────▶│
   │                          │                   handler_N─────────▶│
   │                                                                 │
   │                                               ┌─SSE 断线重连────┐│
   │                                               │                ││
   │                                               │ /events?since=ts││
   │                                               │ store.load_events(since)
   │                                               │                ▼│
   │                                               │ replay 到客户端  │
```

**设计要点**：事件**先落库再分发**，订阅者崩了事件也不丢；断线重连用 `?since=ts` 从库里回放。

### 5.4 V1 / V2 共存关系图

```
                  ┌─────────────────────────┐
                  │   portal.html (同一页面) │
                  │  ┌──────┐  ┌─────────┐  │
                  │  │V1 Tab│  │V2 Tab   │  │
                  │  └──┬───┘  └────┬────┘  │
                  └─────┼───────────┼───────┘
                        │           │
              /api/agents/*    /api/v2/*
                        │           │
              ┌─────────▼──┐   ┌────▼────────┐
              │ V1 Routes  │   │ V2 Routes   │
              └─────┬──────┘   └──────┬──────┘
                    │                 │
              ┌─────▼──────┐   ┌──────▼──────┐
              │ V1 Domain  │   │ V2 Domain   │
              │ Agent/Hub  │   │ AgentV2/    │
              │            │   │ Task/Loop   │
              └─────┬──────┘   └──────┬──────┘
                    │                 │
                    ├──────┬──────────┤
                    ▼      ▼          ▼
        ┌──────────────┐ ┌──────────┐ ┌───────────────┐
        │ agents 表     │ │ 共享服务  │ │ agents_v2 表  │
        │ agent_events │ │ LLM/Skill│ │ tasks_v2 表   │
        │              │ │ MCP/Auth │ │ task_events_v2│
        └──────────────┘ └──────────┘ └───────────────┘
           V1 独占            共用          V2 独占
```

### 5.2 共享 vs 独立的判定规则

**共享**（V2 允许直接调用 V1 的这些模块）：

- `app/llm.py` — LLM Provider 抽象
- `app/skills/store.py`, `app/skills/registry.py` — Skill 系统
- `app/mcp/manager.py` — MCP 系统
- `app/auth.py` — 认证
- `app/runtime_paths.py` — 路径管理
- SQLite 物理文件（但各自的表前缀/后缀不同）

**独立**（V2 禁止 import V1 的这些模块）：

- `app/agent.py`（整个 Agent 类、ROLE_PRESETS）
- `app/agent_llm.py`（chat loop、compression）
- `app/agent_execution.py`
- `app/hub/_core.py`（整个 Hub 类）
- `app/workflow.py`
- `app/persona.py`

**强制机制**：pre-commit hook 扫描 `app/v2/**/*.py`，如发现 `from app.agent import` / `from app.hub` / `from app.agent_llm` 等禁用 import，拒绝提交。详见附录 D。

---

## 6. 核心抽象（5 个类）

### 6.1 Task（数据类）

**职责**：保存一个任务的全部状态。是纯数据 + 极薄方法。

```python
# app/v2/core/task.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class TaskPhase(str, Enum):
    INTAKE   = "intake"
    PLAN     = "plan"
    EXECUTE  = "execute"
    VERIFY   = "verify"
    DELIVER  = "deliver"
    REPORT   = "report"
    DONE     = "done"

class TaskStatus(str, Enum):
    RUNNING    = "running"
    SUCCEEDED  = "succeeded"
    FAILED     = "failed"
    PAUSED     = "paused"
    ABANDONED  = "abandoned"

@dataclass
class PlanStep:
    id: str                              # "s1", "s2"
    goal: str                            # 自然语言目标
    tools_hint: list[str] = field(default_factory=list)
    exit_check: dict = field(default_factory=dict)   # {type: regex|contains|tool_used|artifact_created, spec: ...}
    completed: bool = False
    result_summary: str = ""

@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    expected_artifact_count: int = 0
    schema_version: int = 1

@dataclass
class Artifact:
    id: str
    kind: str                            # "file" | "email" | "rag_entry" | "api_call" | "message"
    handle: str                          # 文件路径 / email ID / rag ID / 等
    summary: str = ""
    created_at: float = 0.0
    produced_by_tool: str = ""

@dataclass
class Lesson:
    id: str
    phase: TaskPhase
    issue: str                           # 失败描述
    fix: str                             # 下次应该怎么做
    created_at: float = 0.0
    dedup_key: str = ""                  # 去重键：sha1(phase + issue_normalized)
                                         #        相同 key 只保留最新一条 + 计数 +1
    occurrence_count: int = 1            # 同类错误出现次数
    last_seen_at: float = 0.0

@dataclass
class TaskContext:
    """Task 运行时的可变上下文。替代 V1 的 agent.messages。"""
    messages: list[dict] = field(default_factory=list)   # OpenAI-style messages
    filled_slots: dict = field(default_factory=dict)
    clarification_pending: bool = False
    scratch: dict = field(default_factory=dict)          # 阶段间传递的临时数据

@dataclass
class Task:
    # 身份
    id: str
    agent_id: str
    parent_task_id: str = ""
    template_id: str = ""

    # 用户层
    intent: str = ""                     # 用户原始输入或 Intake 提炼的意图

    # 状态机
    phase: TaskPhase = TaskPhase.INTAKE
    status: TaskStatus = TaskStatus.RUNNING

    # 运行控制（优先级 / 超时 / 并发）
    priority: int = 5                    # 1=最高，10=最低；同 agent 队列按此排序
    timeout_s: int = 1800                # 墙钟超时（秒）；从 started_at 算起
    finished_reason: str = ""            # completed|failed|timeout|cancelled|abandoned

    # 运行时
    plan: Plan = field(default_factory=Plan)
    context: TaskContext = field(default_factory=TaskContext)
    artifacts: list[Artifact] = field(default_factory=list)
    lessons: list[Lesson] = field(default_factory=list)
    retries: dict = field(default_factory=dict)          # {phase: count}

    # 时间
    created_at: float = 0.0
    started_at: Optional[float] = None   # TaskLoop 真正开始跑的时间（用于 timeout 计算）
    updated_at: float = 0.0
    completed_at: Optional[float] = None

    # 方法（极薄）
    def advance_phase(self, next_phase: TaskPhase) -> None: ...
    def record_retry(self, phase: TaskPhase) -> int: ...
    def add_artifact(self, artifact: Artifact) -> None: ...
    def add_lesson(self, lesson: Lesson) -> None:
        """
        带去重：按 lesson.dedup_key 查重。
        - key 已存在 → occurrence_count +=1, last_seen_at 更新, 不新增条目
        - key 不存在 → 追加
        - dedup_key 为空时按 phase+issue 前 200 字 sha1 生成
        """
        ...
    def to_persist_dict(self) -> dict: ...
    @classmethod
    def from_persist_dict(cls, d: dict) -> "Task": ...
```

**设计要点**：

- `context.messages` 属于 Task 而不是 Agent —— 这是与 V1 的根本区别
- 所有 JSON 字段通过 `to_persist_dict` 扁平化为 SQLite 可存字段
- 方法只做赋值和 JSON 转换，不做业务逻辑（业务逻辑在 TaskLoop）
- **`add_lesson` 内置去重**：相同 `dedup_key` 不重复写入，只累加 `occurrence_count` 并刷新 `last_seen_at`。避免同一错误被反复记录污染 lessons 列表。

---

### 6.2 TaskLoop（推进器）

**职责**：拥有 Task 的推进意志。实现 6 阶段状态机，调用相应的阶段处理器。

```python
# app/v2/core/task_loop.py

from .task import Task, TaskPhase, TaskStatus

MAX_RETRIES_PER_PHASE = {
    TaskPhase.INTAKE:  2,
    TaskPhase.PLAN:    3,
    TaskPhase.EXECUTE: 3,
    TaskPhase.VERIFY:  2,
    TaskPhase.DELIVER: 2,
    TaskPhase.REPORT:  0,   # Report 不重试
}

class TaskLoop:
    def __init__(self, task: Task, agent: "AgentV2",
                 bus: "TaskEventBus", store: "TaskStore",
                 template: "TaskTemplate"):
        self.task = task
        self.agent = agent
        self.bus = bus
        self.store = store
        self.template = template

    def run(self) -> None:
        """主循环。从当前 phase 推进到 DONE。"""
        while self.task.phase != TaskPhase.DONE and \
              self.task.status == TaskStatus.RUNNING:
            phase = self.task.phase
            self._emit("phase_enter", {"phase": phase.value})

            try:
                exit_ok = self._dispatch_phase(phase)
            except Exception as e:
                self._emit("phase_error", {"phase": phase.value, "error": str(e)})
                exit_ok = False

            if exit_ok:
                self._emit("phase_exit", {"phase": phase.value, "ok": True})
                self._advance_next(phase)
            else:
                n = self.task.record_retry(phase)
                if n <= MAX_RETRIES_PER_PHASE[phase]:
                    self._emit("phase_retry", {"phase": phase.value, "attempt": n})
                    continue  # 重跑同一 phase
                # 硬重试耗尽 → 软失败
                self._soft_fail(phase)

        self.store.save(self.task)

    def _dispatch_phase(self, phase: TaskPhase) -> bool:
        """返回 exit 条件是否满足。"""
        handler = {
            TaskPhase.INTAKE:  self._intake,
            TaskPhase.PLAN:    self._plan,
            TaskPhase.EXECUTE: self._execute,
            TaskPhase.VERIFY:  self._verify,
            TaskPhase.DELIVER: self._deliver,
            TaskPhase.REPORT:  self._report,
        }[phase]
        return handler()

    # 每个阶段的实现都以"满足 exit 条件 -> True, 否则 -> False"为唯一契约
    def _intake(self) -> bool: ...
    def _plan(self) -> bool: ...
    def _execute(self) -> bool: ...
    def _verify(self) -> bool: ...
    def _deliver(self) -> bool: ...
    def _report(self) -> bool: ...

    def _advance_next(self, cur: TaskPhase) -> None:
        order = [TaskPhase.INTAKE, TaskPhase.PLAN, TaskPhase.EXECUTE,
                 TaskPhase.VERIFY, TaskPhase.DELIVER, TaskPhase.REPORT,
                 TaskPhase.DONE]
        self.task.phase = order[order.index(cur) + 1]

    def _soft_fail(self, phase: TaskPhase) -> None:
        """硬重试耗尽的软兜底：推进到 Report 阶段写失败汇报。"""
        self.task.status = TaskStatus.FAILED
        self.task.add_lesson(Lesson(
            id=f"L-{len(self.task.lessons)+1}",
            phase=phase,
            issue=f"phase {phase.value} exceeded max retries",
            fix="TBD (human review)",
            created_at=time.time(),
        ))
        self.task.phase = TaskPhase.REPORT  # 仍然去写 Report 让用户看见

    def _emit(self, event_type: str, payload: dict) -> None:
        self.bus.publish(self.task.id, self.task.phase, event_type, payload)
```

**设计要点**：

- **唯一契约**：每个 `_phase()` 方法返回 `bool`（exit 条件是否满足）
- 硬重试 + 软失败在主循环统一处理，阶段实现不关心重试逻辑
- 即使失败也会走到 Report 阶段，**保证用户永远能收到汇报**（G4）
- TaskLoop 是短寿命对象，每次 `agent.submit_task()` 创建新的实例

---

### 6.3 TaskExecutor（Execute 阶段的 LLM + tool 循环）

**职责**：封装"推进 plan.steps"的 LLM + 工具调用循环。**这是 V2 里唯一和 LLM 打交道的地方**。

```python
# app/v2/core/task_executor.py

class TaskExecutor:
    def __init__(self, task: Task, agent: AgentV2, bus: TaskEventBus,
                 llm_provider, tool_registry):
        self.task = task
        self.agent = agent
        self.bus = bus
        self.llm = llm_provider
        self.tools = tool_registry
        self.MAX_TOOL_TURNS_PER_STEP = 12

    def run_step(self, step: PlanStep) -> bool:
        """推进单个 step，返回 step.exit_check 是否满足。"""
        self.bus.publish(self.task.id, TaskPhase.EXECUTE,
                        "step_enter", {"step_id": step.id, "goal": step.goal})

        for turn in range(self.MAX_TOOL_TURNS_PER_STEP):
            msg = self._call_llm()

            if self._has_tool_calls(msg):
                for tc in msg["tool_calls"]:
                    self._emit_tool_call(tc)
                    result = self._invoke_tool(tc)
                    self._emit_tool_result(tc, result)
                    self.task.context.messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": result
                    })
                # 检查 exit_check
                if self._step_exit_met(step):
                    step.completed = True
                    self.bus.publish(self.task.id, TaskPhase.EXECUTE,
                                     "step_exit", {"step_id": step.id, "ok": True})
                    return True
                continue

            # 无 tool_call 的纯文本回复 → 当作 "progress narrative"
            self.bus.publish(self.task.id, TaskPhase.EXECUTE,
                            "progress", {"text": msg.get("content", "")[:500]})

            # 如果 LLM 停了但 step 未完成 → 注入 nudge 再试
            if not self._step_exit_met(step):
                self._inject_nudge(step)
                continue

            step.completed = True
            return True

        # 超 tool turn 上限
        self.bus.publish(self.task.id, TaskPhase.EXECUTE,
                        "step_exit", {"step_id": step.id, "ok": False,
                                      "reason": "max_tool_turns"})
        return False

    def on_context_pressure(self) -> None:
        """
        当 task.context.messages 过长时的压缩策略。
        内聚在 Executor 内部，不暴露为"agent.compress_context()"。
        """
        # 复用 V1 压缩逻辑的精华（保留 tool_use/tool_result 肌肉记忆，
        # 压缩叙述性文本），但数据源是 task.context.messages 而不是 agent.messages
        ...

    def _call_llm(self) -> dict: ...
    def _has_tool_calls(self, msg: dict) -> bool: ...
    def _invoke_tool(self, tool_call: dict) -> str: ...
    def _step_exit_met(self, step: PlanStep) -> bool: ...
    def _inject_nudge(self, step: PlanStep) -> None:
        """向 messages 注入一条 system message：
           '你已声明意图但未调用工具推进 step X，请立即调用所需工具。'"""
        ...
    def _emit_tool_call(self, tc: dict) -> None: ...
    def _emit_tool_result(self, tc: dict, result: str) -> None: ...
```

**设计要点**：

- 每个 step 独立推进，step 之间 TaskLoop 有机会决策（比如 pause、重规划）
- LLM 停笔但 step 未完成 → Executor **主动注入 nudge**，这就是之前讨论的"外层驱动"
- 压缩是 Executor 内部方法，不再是 agent 顶层 API —— **解决 V1 的根因 3**

---

### 6.4 TaskEventBus（事件流）

**职责**：单向事件发布，订阅者通过 SSE 接收。

```python
# app/v2/core/task_events.py

from dataclasses import dataclass
from typing import Literal

# 事件类型是闭集，不允许自由扩展
EventType = Literal[
    "task_submitted",
    "phase_enter", "phase_exit", "phase_retry", "phase_error",
    "intake_slots_filled", "intake_clarification",
    "plan_draft", "plan_approved",
    "step_enter", "step_exit",
    "tool_call", "tool_result",
    "progress",
    "artifact_created",
    "verify_check", "verify_retry",
    "lesson_recorded",
    "task_completed", "task_failed", "task_paused", "task_resumed",
]

@dataclass
class TaskEvent:
    task_id: str
    ts: float
    phase: str          # TaskPhase.value
    type: EventType
    payload: dict

class TaskEventBus:
    # 批量写入策略：减轻 SQLite 压力，同时保持 at-least-once
    BATCH_SIZE          = 50          # 累积 50 条立即 flush
    BATCH_FLUSH_MS      = 200         # 或最多攒 200ms 立即 flush
    CRITICAL_EVENT_TYPES = {          # 关键事件绕过批量，立即 flush
        "task_submitted", "task_completed", "task_failed",
        "task_paused", "task_resumed", "phase_error",
        "intake_clarification",       # 用户在等这个
    }

    def __init__(self, store: "TaskStore"):
        self.store = store
        self._subscribers: dict[str, list[callable]] = {}
        self._buf: list[TaskEvent] = []
        self._buf_lock = threading.Lock()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="TaskEventBus-Flusher")
        self._flush_thread.start()

    def publish(self, task_id: str, phase: TaskPhase,
                event_type: EventType, payload: dict) -> None:
        evt = TaskEvent(
            task_id=task_id,
            ts=time.time(),
            phase=phase.value if isinstance(phase, TaskPhase) else phase,
            type=event_type,
            payload=payload,
        )
        # 关键事件：同步落库 + 立即分发（保证用户看到 / 保证任务终态不丢）
        if event_type in self.CRITICAL_EVENT_TYPES:
            self.store.append_event(evt)
            self._dispatch(evt)
            return

        # 普通事件：入 buffer + 异步批量 flush + 立即分发给订阅者
        # （订阅者分发不等落库，SSE 时间线不卡顿；落库由后台线程保证）
        with self._buf_lock:
            self._buf.append(evt)
            should_flush = len(self._buf) >= self.BATCH_SIZE
        self._dispatch(evt)
        if should_flush:
            self._flush_now()

    def _flush_loop(self) -> None:
        while True:
            time.sleep(self.BATCH_FLUSH_MS / 1000.0)
            self._flush_now()

    def _flush_now(self) -> None:
        with self._buf_lock:
            if not self._buf:
                return
            batch, self._buf = self._buf, []
        try:
            self.store.append_events_batch(batch)
        except Exception as e:
            # 批量失败：重入 buffer 头部，下次重试（保证 at-least-once）
            with self._buf_lock:
                self._buf[:0] = batch

    def _dispatch(self, evt: TaskEvent) -> None:
        for handler in self._subscribers.get(evt.task_id, []):
            try:
                handler(evt)
            except Exception:
                pass

    def subscribe(self, task_id: str, handler: callable) -> callable: ...
    def replay(self, task_id: str, since_ts: float = 0.0) -> list[TaskEvent]: ...

    def flush_and_close(self, task_id: str) -> None:
        """任务终态时调用：确保该 task 的所有 buffered 事件落库。"""
        self._flush_now()
```

**设计要点**：

- 事件先持久化再分发 —— 保证即使订阅者崩了事件也不丢
- `replay()` 让前端可以打开任意历史任务还原完整过程（V1 做不到）
- 事件类型是闭集，禁止随便加新类型（保持前端渲染器的有限复杂度）

---

### 6.5 AgentV2（瘦壳）

**职责**：只持有身份和能力声明，不持有会话状态。是任务的发起者和归属。

```python
# app/v2/agent/agent_v2.py

@dataclass
class Capabilities:
    skills: list[str] = field(default_factory=list)        # skill_id 列表
    mcps: list[str] = field(default_factory=list)          # mcp_binding_id 列表
    tools: list[str] = field(default_factory=list)         # built-in tool 名
    llm_tier: str = "default"                               # 档位，实际 provider/model 由 router 解析
    denied_tools: list[str] = field(default_factory=list)

@dataclass
class AgentV2:
    id: str
    name: str
    role: str                                               # meeting_assistant, pm, researcher 等
    v1_agent_id: str = ""                                   # 从 V1 克隆时指回
    capabilities: Capabilities = field(default_factory=Capabilities)
    task_template_ids: list[str] = field(default_factory=list)
    working_directory: str = ""
    created_at: float = 0.0

    # 唯一的顶层入口
    def submit_task(self, intent: str, template_id: str = "",
                    parent_task_id: str = "") -> Task:
        """创建一个 Task，交给 TaskLoop 推进。"""
        task = Task(
            id=_new_task_id(),
            agent_id=self.id,
            parent_task_id=parent_task_id,
            template_id=template_id or self._default_template_for(intent),
            intent=intent,
            created_at=time.time(),
            updated_at=time.time(),
        )
        # 持久化、注入初始 Lessons、启动 TaskLoop
        ...
        return task

    def _default_template_for(self, intent: str) -> str:
        """简单关键词匹配，或默认走 generic_template。"""
        ...
```

**注意**：

- 没有 `chat()` 方法 —— 对话型交互也是一种 task（`template_id="conversation"`）
- `messages` 不在 agent 上 —— 每次 submit_task 创建独立上下文
- Capabilities 是"声明"，实际绑定在 TaskLoop 启动时按快照拿

---

## 7. 数据源设计

### 7.0 设计原则（对 V1 数据乱象的直接回应）

V1 的数据问题：
- `agent.messages` vs `agent.profile.messages` 双份（不一致）
- `agent.mcp_servers` 顶层 + `agent.profile.mcp_servers` 也存（哪个权威？）
- `granted_skills` 在 agent、在 registry、在 workspace 各有一份（撤销不生效）
- 事件记在 agent 级别，无法按任务聚合

V2 的四条铁律：

| # | 铁律 | 落地 |
|---|---|---|
| **D1** | **每个概念有且只有一个权威来源（SoT, Source of Truth）** | 其他地方要么是缓存（带 TTL）要么是快照（不可变） |
| **D2** | **运行时状态属于 Task，不属于 Agent** | messages / artifacts / lessons 全在 tasks_v2 |
| **D3** | **Agent 只存声明，不存会话** | capabilities 是声明；具体绑定在 Task 启动时快照 |
| **D4** | **事件按 task_id 聚合，不按 agent_id** | 任意任务可独立回放 |

### 7.1 权威数据源（Source of Truth）表

| 概念 | 权威来源 | 读取路径 | 写入者 | V1 对比 |
|---|---|---|---|---|
| Agent 身份 | `agents_v2` 表 | `TaskStore.get_agent(id)` | API / 管理面板 | 改 |
| Agent 能力声明 | `agents_v2.capabilities_json` | 同上 | API | 统一 |
| Skill 安装列表 | `skill_registry._installs`（Layer 1 共享） | `registry.list_all()` | Skill Store | 共享 |
| Skill 授权关系 | `skill_registry._installs[sid].granted_to`（共享） | `registry.grants_for(agent_id)` | `hub.apply_skill_grant/revoke` | 共享 |
| MCP 绑定 | `mcp_manager` binding 表（共享） | `mcp_manager.bindings_for(agent_id)` | API | 共享 |
| Task 本体 | `tasks_v2` 表 | `TaskStore.get_task(id)` | TaskLoop | 新 |
| Task 上下文 (messages) | `tasks_v2.context_json` | `task.context.messages` | TaskExecutor | **改归属** |
| Task 规划 | `tasks_v2.plan_json` | `task.plan` | TaskLoop (Plan phase) | 新 |
| Task 产出 | `tasks_v2.artifacts_json` | `task.artifacts` | TaskLoop + Executor | 新 |
| Task 事件流 | `task_events_v2` 表 | `TaskStore.load_events(task_id)` | TaskEventBus | 重构 |
| Task 复盘 | `tasks_v2.lessons_json` | `task.lessons` | Verify / SoftFail | 替代 ExperienceLibrary |
| 能力快照 | `tasks_v2.context_json.capabilities_snapshot` | Task 启动时固化 | TaskLoop (Intake 前) | 新 |

**铁律体现**：
- Task 的 messages 只有一个副本 —— `tasks_v2.context_json` —— 永远不会出现"顶层 vs profile"的分叉
- Skill 授权永远读 registry —— 即使 agent 内部有缓存也要过期重拉
- 事件永远按 task_id 索引 —— 永远不会出现"我想看这次任务做了什么"要全库扫描

### 7.2 SQLite Schema

存储位置：`~/.tudou_claw/tudou.db`（与 V1 同文件，不同表）。

```sql
-- V2 Agent（瘦壳）
CREATE TABLE IF NOT EXISTS agents_v2 (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  role            TEXT NOT NULL,
  v1_agent_id     TEXT DEFAULT '',
  capabilities_json   TEXT NOT NULL,      -- Capabilities 序列化
  task_template_ids_json TEXT DEFAULT '[]',
  working_directory TEXT DEFAULT '',
  created_at      REAL NOT NULL
);
CREATE INDEX idx_agents_v2_role ON agents_v2(role);

-- V2 Task（一等公民）
CREATE TABLE IF NOT EXISTS tasks_v2 (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL,
  parent_task_id  TEXT DEFAULT '',
  template_id     TEXT DEFAULT '',

  intent          TEXT NOT NULL,
  phase           TEXT NOT NULL,           -- TaskPhase.value
  status          TEXT NOT NULL,           -- TaskStatus.value

  plan_json       TEXT DEFAULT '{}',
  context_json    TEXT DEFAULT '{}',       -- TaskContext (messages + slots + scratch)
  artifacts_json  TEXT DEFAULT '[]',
  lessons_json    TEXT DEFAULT '[]',
  retries_json    TEXT DEFAULT '{}',

  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  completed_at    REAL DEFAULT NULL,

  FOREIGN KEY (agent_id) REFERENCES agents_v2(id)
);
CREATE INDEX idx_tasks_v2_agent ON tasks_v2(agent_id);
CREATE INDEX idx_tasks_v2_status ON tasks_v2(status);
CREATE INDEX idx_tasks_v2_created ON tasks_v2(created_at DESC);

-- V2 Task Events（事件流 / KPI 源数据）
CREATE TABLE IF NOT EXISTS task_events_v2 (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         TEXT NOT NULL,
  ts              REAL NOT NULL,
  phase           TEXT NOT NULL,
  type            TEXT NOT NULL,
  payload_json    TEXT DEFAULT '{}',

  FOREIGN KEY (task_id) REFERENCES tasks_v2(id)
);
CREATE INDEX idx_events_v2_task ON task_events_v2(task_id, ts);
CREATE INDEX idx_events_v2_type ON task_events_v2(type);
```

### 7.3 文件系统布局

```
~/.tudou_claw/
  tudou.db                              ← SQLite 主文件（V1/V2 共享，表区分）
  v2/
    templates/                          ← YAML 任务模板（管理员可编辑）
      conversation.yaml
      research_report.yaml
      meeting_summary.yaml
    agents/{agent_v2_id}/
      workspace/                        ← Agent 工作目录（共 artifact 落盘用）
        tasks/
          {task_id}/
            artifacts/                  ← 该任务的产出文件
            scratch/                    ← 临时中间文件（可 GC）
      capabilities_snapshot/
        {task_id}.json                  ← 任务启动时的能力快照（不可变）
```

**快照机制**：Task 启动时把当前 agent.capabilities + 每个 skill/mcp 的版本号冻结到 `{task_id}.json`。即使任务执行中管理员撤销了 agent 的某个 skill，**本任务仍按快照继续跑完**，保证任务确定性。

### 7.4 数据生命周期（读写路径）

每类数据的完整生命周期：

#### Agent 生命周期
```
创建 (POST /api/v2/agents)
   → INSERT agents_v2
   → 写 ~/.tudou_claw/v2/agents/{id}/ 目录
修改能力 (PATCH)
   → UPDATE agents_v2.capabilities_json
授权 Skill / 绑定 MCP
   → 调 Layer 1 的 registry.grant / mcp_manager.bind（不改 agents_v2）
   → 读时从 registry / mcp_manager 取（权威），不从 agent 内读
删除 (DELETE)
   → 软删：UPDATE agents_v2 SET archived=1
   → 关联的 running tasks 置为 abandoned
```

#### Task 生命周期
```
提交 (POST /agents/{id}/tasks)
   → INSERT tasks_v2 (phase=intake, status=running)
   → INSERT task_events_v2 (type=task_submitted)
   → 冻结 capabilities_snapshot 到文件
   → 启动 TaskLoop（后台线程）
推进
   → UPDATE tasks_v2 (每个 phase 变动)
   → INSERT task_events_v2 (每个事件)
   → artifacts 文件写入 workspace/tasks/{task_id}/artifacts/
完成 / 失败 / 取消
   → UPDATE tasks_v2 SET status=..., completed_at=...
   → INSERT task_events_v2 (task_completed/failed/abandoned)
归档 (30 天后)
   → scratch/ 目录 GC；artifacts/ 保留
   → task_events_v2 按月分区（后续优化）
```

#### Event 生命周期
```
产生 (TaskLoop / Executor 内部)
   → bus.publish()
   → **先** INSERT task_events_v2 (保证不丢)
   → **后** 分发到 SSE 订阅者
订阅 (SSE 客户端)
   → 新连接：bus.subscribe(task_id) 拿增量
   → 断线重连：GET /events?since=ts → store.load_events(since)
   → 永远从 task_events_v2 回放，不依赖内存状态
```

### 7.5 数据一致性边界

| 场景 | 一致性要求 | 实现 |
|---|---|---|
| Task 状态 + Events | 强一致 | 同一事务：UPDATE tasks_v2 + INSERT task_events_v2 |
| Event 发布 + 订阅者分发 | At-least-once | 先落库后分发；订阅者崩溃不影响库 |
| Task artifacts 文件 + JSON 记录 | 最终一致 | 先写文件，后 UPDATE artifacts_json；重启时扫 workspace 校对 |
| Skill 授权（V1/V2 共享） | 读时强一致 | V2 永不缓存 granted_skills；每次 Task 启动从 registry 取 |
| Capabilities Snapshot | 不可变 | JSON 文件，Task 启动后不再修改 |

### 7.6 数据迁移路径（V1 → V2）

V2 不做自动迁移。提供**按需克隆**：

```
POST /api/v2/agents/{v2_id}/clone_from_v1
body: {"v1_agent_id": "xxx"}

动作：
  - 读 V1 agents 表的 name / role / working_directory
  - 读 V1 profile 的 granted_skills / mcp_servers
  - 写 V2 agents_v2 表（capabilities = 转换后）
  - **不复制** V1 的 messages（V2 按 task 组织，messages 属 task）
  - V1 原 agent 保持不变
```

V1 数据永不迁移到 V2 表。V1 表作为历史数据库一直保留到 V1 废弃（M8）。

### 7.7 JSON 字段内部结构

#### `tasks_v2.plan_json`
```json
{
  "steps": [
    {
      "id": "s1",
      "goal": "调研中东地区 VMware 市场份额",
      "tools_hint": ["web_search", "rag_query"],
      "exit_check": {
        "type": "artifact_created",
        "spec": {"kind": "research_notes", "min_count": 1}
      },
      "completed": false,
      "result_summary": ""
    }
  ],
  "expected_artifact_count": 2,
  "schema_version": 1
}
```

#### `tasks_v2.context_json`
```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user",   "content": "..."},
    {"role": "assistant", "tool_calls": [...]}
  ],
  "filled_slots": {"region": "中东", "report_type": "pptx"},
  "clarification_pending": false,
  "scratch": {}
}
```

#### `task_events_v2.payload_json`
按 event.type 有不同 schema，见附录 B。

---

## 8. 阶段契约（6 个 Phase 各自的合同）

### 8.1 Intake（接收）

| 项 | 内容 |
|---|---|
| **输入** | `task.intent`（用户原始输入）+ agent 的 `task_template` |
| **做什么** | 调 LLM 抽取 slots；判断是否需要向用户反问 |
| **产出** | `task.context.filled_slots` 填充完 或 `clarification_pending=True` |
| **Exit 条件** | `all(slot in filled_slots for slot in template.required_slots)` 或 `clarification_pending=True` |
| **失败处理** | 最多 2 次；仍失败则 clarification_pending=True 让用户补充 |
| **典型事件** | `intake_slots_filled`, `intake_clarification` |

### 8.2 Plan（规划）

| 项 | 内容 |
|---|---|
| **输入** | intent + filled_slots + 同 agent 历史 lessons |
| **做什么** | 调 LLM 输出结构化 Plan JSON |
| **产出** | `task.plan` |
| **Exit 条件** | JSON schema valid 且 `len(plan.steps) >= 1` |
| **失败处理** | 最多 3 次；仍失败则 soft_fail → Report |
| **典型事件** | `plan_draft`, `plan_approved` |

### 8.3 Execute（执行）

| 项 | 内容 |
|---|---|
| **输入** | plan + capabilities snapshot |
| **做什么** | TaskExecutor 对每个 step 跑 LLM + tool 循环 |
| **产出** | 每个 step.completed=True；过程中产生的 artifacts |
| **Exit 条件** | `all(step.completed for step in plan.steps)` |
| **失败处理** | 单 step 超 tool_turns 或 exit_check 失败 → 该 step 重跑 ≤ 3 次；全部耗尽则整个 Execute 判失败 |
| **典型事件** | `step_enter`, `tool_call`, `tool_result`, `progress`, `step_exit`, `artifact_created` |

### 8.4 Verify（验收）

| 项 | 内容 |
|---|---|
| **输入** | plan + artifacts + template.verify_rules |
| **做什么** | 对每条 verify_rule 执行规则检查（regex / section_exists / json_schema / tool_used / llm_judge） |
| **产出** | `verify_report = [{rule_id, passed, note}]` |
| **Exit 条件** | `all(r.passed for r in verify_report)` |
| **失败处理** | 最多 2 次；失败时回退到 Execute 重跑失败的 step，并把 verify feedback 注入 context |
| **典型事件** | `verify_check`, `verify_retry` |

### 8.5 Deliver（交付）

| 项 | 内容 |
|---|---|
| **输入** | artifacts |
| **做什么** | 按 kind 分发：file 落盘、email 发送、rag 入库、message 回复用户 |
| **产出** | 每个 artifact 有 `handle`（可验证句柄） |
| **Exit 条件** | `all(art.handle != "" for art in artifacts)` |
| **失败处理** | 单个 artifact 交付失败重试 ≤ 2；仍失败则 degraded 状态但不阻塞整体 |
| **典型事件** | `artifact_created`（kind=delivery_receipt） |

### 8.6 Report（汇报）

| 项 | 内容 |
|---|---|
| **输入** | 全部 context + artifacts + lessons |
| **做什么** | 调 LLM 生成总结文本；写入 context.messages；emit task_completed/failed |
| **产出** | 用户可见的最终汇报 |
| **Exit 条件** | 永远 True（sink 阶段） |
| **失败处理** | 无（即使 LLM 失败也用模板化文本兜底） |
| **典型事件** | `task_completed` / `task_failed`, `lesson_recorded` |

---

## 9. 事件模型

### 9.1 完整事件类型表

| Type | 触发时机 | 关键 payload |
|---|---|---|
| `task_submitted` | submit_task 返回前 | `{intent, template_id}` |
| `phase_enter` | 每个 phase 开始 | `{phase}` |
| `phase_exit` | phase exit 条件满足 | `{phase, ok: true}` |
| `phase_retry` | exit 未满足，进入重试 | `{phase, attempt}` |
| `phase_error` | phase 执行抛异常 | `{phase, error}` |
| `intake_slots_filled` | Intake 成功 | `{slots: {...}}` |
| `intake_clarification` | 需要用户补充 | `{question}` |
| `plan_draft` | LLM 输出 plan | `{plan}` |
| `plan_approved` | Plan schema valid | `{step_count}` |
| `step_enter` | Execute 阶段某 step 开始 | `{step_id, goal}` |
| `tool_call` | 发起工具调用 | `{step_id, tool, args}` |
| `tool_result` | 工具返回 | `{step_id, tool, summary, full_in_db}` |
| `progress` | LLM 纯文本叙述 | `{step_id, text}` |
| `step_exit` | step 完成或失败 | `{step_id, ok, reason?}` |
| `artifact_created` | 新增 artifact | `{artifact: {...}}` |
| `verify_check` | 单条规则检查 | `{rule_id, passed, note}` |
| `verify_retry` | 验收失败回 Execute | `{attempt, failing_rules}` |
| `lesson_recorded` | 写入 lessons | `{lesson}` |
| `task_completed` | Report 阶段结束，success | `{summary, artifact_count, duration_s}` |
| `task_failed` | Report 阶段结束，fail | `{summary, failed_phase, reason}` |
| `task_paused` | 用户手动暂停 | `{}` |
| `task_resumed` | 用户手动恢复 | `{}` |

### 9.2 前端渲染策略

- **TaskBoard** 只订阅 `task_submitted` / `task_completed` / `task_failed` / `phase_enter`（用于 phase 进度条）
- **TaskTimeline** 订阅当前打开任务的**全部**事件
- **TaskConsole** 订阅 `intake_clarification` 决定是否弹反问输入框

---

## 10. 接口设计

### 10.0 接口分层

V2 的接口共 4 层，每一层契约独立：

```
┌──────────────────────────────────────────────────────┐
│  L4  前端 ↔ 后端契约                                   │
│      REST (动作) + SSE (事件) + URL hash (路由)        │
└──────────────────────────────────────────────────────┘
                        ▲
┌──────────────────────────────────────────────────────┐
│  L3  HTTP API                                         │
│      REST 端点 (§10.1) + SSE 协议 (§10.3)              │
└──────────────────────────────────────────────────────┘
                        ▲
┌──────────────────────────────────────────────────────┐
│  L2  Python 内部 API（模块间）                          │
│      AgentV2 / Task / TaskLoop / TaskExecutor 的方法   │
│      (§10.4)                                          │
└──────────────────────────────────────────────────────┘
                        ▲
┌──────────────────────────────────────────────────────┐
│  L1  共享服务适配接口                                   │
│      llm_bridge / skill_bridge / mcp_bridge (§10.5)    │
│      向 V1 Layer 1 服务的适配薄层                       │
└──────────────────────────────────────────────────────┘
```

### 10.1 REST 端点

| Method | Path | 描述 |
|---|---|---|
| `POST` | `/api/v2/agents` | 创建 V2 agent |
| `GET`  | `/api/v2/agents` | 列出 V2 agent |
| `GET`  | `/api/v2/agents/{id}` | 详情 |
| `PATCH`| `/api/v2/agents/{id}` | 改 capabilities / name |
| `DELETE`| `/api/v2/agents/{id}` | 删除（软删：置 archived=true） |
| `POST` | `/api/v2/agents/{id}/clone_from_v1` | 从 V1 agent 克隆身份到 V2 |
| `POST` | `/api/v2/agents/{id}/tasks` | 提交新任务 |
| `GET`  | `/api/v2/tasks` | 列表（filter: agent_id, status, limit） |
| `GET`  | `/api/v2/tasks/{id}` | 详情（含 plan/artifacts/lessons） |
| `POST` | `/api/v2/tasks/{id}/pause` | 暂停 |
| `POST` | `/api/v2/tasks/{id}/resume` | 恢复 |
| `POST` | `/api/v2/tasks/{id}/cancel` | 取消（→ abandoned） |
| `POST` | `/api/v2/tasks/{id}/clarify` | 回答 Intake 的反问 |
| `GET`  | `/api/v2/tasks/{id}/events` | SSE 事件流（支持 `?since=ts` 断点续传） |
| `GET`  | `/api/v2/templates` | 列出 YAML 任务模板 |
| `GET`  | `/api/v2/templates/{id}` | 模板详情 |

### 10.2 REST 详细接口规范

所有 REST 接口：
- **Content-Type**: `application/json`
- **认证**: 复用 V1 的 `app.auth` 中间件（Session Cookie）
- **错误响应**: `{"ok": false, "error": "...", "error_code": "..."}`
- **HTTP 状态**: 2xx 成功；400 参数错；401 未登录；403 无权限；404 不存在；409 状态冲突；500 内部错

#### 10.2.1 `POST /api/v2/agents`

**Request**
```json
{
  "name": "MeetBot",
  "role": "meeting_assistant",
  "capabilities": {
    "skills": ["pptx-author@1.0.0"],
    "mcps": ["agent_mail"],
    "tools": ["web_search", "rag_query"],
    "llm_tier": "writing_strong",
    "denied_tools": []
  },
  "task_template_ids": ["meeting_summary", "conversation"],
  "working_directory": ""
}
```

**Response 201**
```json
{
  "ok": true,
  "agent": {
    "id": "v2_9f3a",
    "name": "MeetBot",
    "role": "meeting_assistant",
    "capabilities": { ... },
    "task_template_ids": ["meeting_summary", "conversation"],
    "created_at": 1729321234.567
  }
}
```

**错误**
- `400 INVALID_LLM_TIER` — llm_tier 不在允许列表
- `400 UNKNOWN_SKILL` — 引用了 registry 里不存在的 skill

---

#### 10.2.2 `POST /api/v2/agents/{id}/clone_from_v1`

**Request**
```json
{ "v1_agent_id": "adf1371a6e72" }
```

**Response 201**
```json
{
  "ok": true,
  "agent": {
    "id": "v2_7c1d",
    "name": "ceo-菜小二",
    "role": "ceo",
    "v1_agent_id": "adf1371a6e72",
    "capabilities": {
      "skills": ["pptx-author@1.0.0"],
      "mcps": ["agent_mail", "brave_search"],
      "llm_tier": "default"
    },
    "created_at": 1729321240.0
  },
  "clone_report": {
    "copied_skills": 1,
    "copied_mcps": 2,
    "skipped_messages": true
  }
}
```

---

#### 10.2.3 `POST /api/v2/agents/{id}/tasks`

**Request**
```json
{
  "intent": "重新制作一下新的pptx报告发给我邮箱，需要新增调查信息",
  "template_id": "research_report",
  "parent_task_id": ""
}
```

字段说明：
- `intent` (required): 用户原始意图文本
- `template_id` (optional): 若空则调用 `AgentV2._default_template_for(intent)` 做关键字匹配
- `parent_task_id` (optional): 子任务场景使用

**Response 202**（异步启动）
```json
{
  "ok": true,
  "task": {
    "id": "t_7f3c9a",
    "agent_id": "v2_7c1d",
    "template_id": "research_report",
    "intent": "重新制作...",
    "phase": "intake",
    "status": "running",
    "created_at": 1729321234.567,
    "event_stream_url": "/api/v2/tasks/t_7f3c9a/events"
  }
}
```

**错误**
- `404 AGENT_NOT_FOUND`
- `400 UNKNOWN_TEMPLATE`
- `409 AGENT_BUSY` — 如果 Q2 的决策是"单 agent 单任务"

---

#### 10.2.4 `GET /api/v2/tasks/{id}`

**Response 200**
```json
{
  "ok": true,
  "task": {
    "id": "t_7f3c9a",
    "agent_id": "v2_7c1d",
    "template_id": "research_report",
    "intent": "重新制作...",
    "phase": "execute",
    "status": "running",
    "plan": {
      "steps": [
        {"id":"s1","goal":"调研","tools_hint":["web_search"],"completed":true},
        {"id":"s2","goal":"对比","tools_hint":["rag_query"],"completed":false}
      ],
      "expected_artifact_count": 2
    },
    "artifacts": [
      {"id":"a1","kind":"research_notes","handle":"workspace/.../notes.md","summary":"..."}
    ],
    "lessons": [],
    "retries": {"execute": 0},
    "created_at": 1729321234.567,
    "updated_at": 1729321292.1,
    "completed_at": null
  }
}
```

---

#### 10.2.5 `GET /api/v2/tasks`

**Query 参数**
| 参数 | 类型 | 说明 |
|---|---|---|
| `agent_id` | string | 按 agent 过滤 |
| `status` | string | `running`/`succeeded`/`failed`/`paused`/`abandoned` |
| `limit` | int | 默认 50，最大 500 |
| `offset` | int | 分页 |
| `since` | float | 只返回 created_at > since |

**Response 200**
```json
{
  "ok": true,
  "tasks": [ ... ],
  "total": 127,
  "has_more": true
}
```

---

#### 10.2.6 `POST /api/v2/tasks/{id}/clarify`

当 Intake 阶段发 `intake_clarification` 事件后，用户回答通过这个接口提交。

**Request**
```json
{ "answer": "主题是 VMware 中东市场，region 填中东和北非" }
```

**Response 200**
```json
{
  "ok": true,
  "task": { "phase": "intake", "status": "running", ... }
}
```

内部行为：答案写入 `task.context.scratch.clarification_answer`，TaskLoop 重新进入 Intake phase。

---

#### 10.2.7 `POST /api/v2/tasks/{id}/pause` / `resume` / `cancel`

**Request**: `{}`
**Response 200**: `{"ok": true, "task": {...}}`
**错误**: `409 INVALID_STATE_TRANSITION` (例：想 pause 一个已完成的 task)

状态转换合法表：
| 当前 | pause 可 | resume 可 | cancel 可 |
|---|---|---|---|
| running | ✅ → paused | ❌ | ✅ → abandoned |
| paused | ❌ | ✅ → running | ✅ → abandoned |
| succeeded | ❌ | ❌ | ❌ |
| failed | ❌ | ❌ | ❌ |
| abandoned | ❌ | ❌ | ❌ |

---

#### 10.2.8 `GET /api/v2/templates`

**Response 200**
```json
{
  "ok": true,
  "templates": [
    {
      "id": "research_report",
      "display_name": "研究报告任务",
      "version": 1,
      "required_slots": ["topic", "delivery"],
      "allowed_tools": ["web_search", "create_pptx", "send_email"]
    }
  ]
}
```

### 10.3 SSE 事件流协议

#### 10.3.1 端点

```
GET /api/v2/tasks/{task_id}/events
GET /api/v2/tasks/{task_id}/events?since=1729321234.6    # 断点续传
```

**Response Headers**
```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

#### 10.3.2 帧格式

每个 SSE 事件遵循标准格式：

```
id: 1234
event: <event_type>
data: {"task_id":"t_xxx","phase":"execute","payload":{...},"ts":1729321234.6}

```

- `id`: 对应 `task_events_v2.id`（数据库自增主键）
- `event`: 事件类型（见 §9.1）
- `data`: JSON 字符串

客户端收到后存 `lastEventId`，断线重连时通过 `Last-Event-ID` HTTP header（标准）或 query 参数 `?since=<ts>` 续传。

#### 10.3.3 特殊事件

| 事件 | 意义 | 客户端处理 |
|---|---|---|
| `event: heartbeat` | 每 15s 发送，保持连接 | 忽略 payload，更新 "last_seen" |
| `event: stream_end` | 任务已达终态，连接即将关闭 | 客户端应关闭并切到 task 详情接口 |
| `event: error` | 服务端错误 | 展示 error payload，重连 |

#### 10.3.4 示例完整会话

```
← GET /api/v2/tasks/t_7f3c9a/events
→ HTTP/1.1 200 OK
  Content-Type: text/event-stream

  id: 501
  event: task_submitted
  data: {"task_id":"t_7f3c9a","phase":"intake","payload":{"intent":"..."},"ts":1729321234.5}

  id: 502
  event: phase_enter
  data: {"task_id":"t_7f3c9a","phase":"intake","payload":{"phase":"intake"},"ts":1729321234.6}

  id: 503
  event: intake_slots_filled
  data: {"task_id":"t_7f3c9a","phase":"intake","payload":{"slots":{"topic":"VMware"}},"ts":1729321236.1}

  ...（省略中间事件）

  id: 587
  event: heartbeat
  data: {"ts":1729321290.0}

  id: 612
  event: task_completed
  data: {"task_id":"t_7f3c9a","phase":"report","payload":{"duration_s":60},"ts":1729321294.2}

  id: 613
  event: stream_end
  data: {}
```

### 10.4 Python 内部 API（L2 模块间契约）

这些是 V2 模块之间的调用契约，不对外暴露。

#### 10.4.1 `AgentV2`

```python
class AgentV2:
    def submit_task(self, intent: str,
                    template_id: str = "",
                    parent_task_id: str = "") -> Task:
        """
        创建 Task 并启动 TaskLoop 后台推进。立即返回 Task（phase=intake）。
        事件流在另一线程中驱动。
        Raises:
            ValueError: template_id 不存在
            RuntimeError: 能力快照冻结失败
        """

    def capabilities_snapshot(self) -> dict:
        """
        冻结当前能力到不可变字典。Task 启动时调用一次。
        返回结构：
          {"skills": [{"id":"...", "version":"..."}],
           "mcps":   [{"id":"...", "binding_id":"..."}],
           "tools":  [...],
           "llm_tier": "..."}
        """

    @classmethod
    def clone_from_v1(cls, v1_agent_id: str) -> "AgentV2":
        """从 V1 agent 克隆身份。messages 不复制。"""
```

#### 10.4.2 `TaskLoop`

```python
class TaskLoop:
    def __init__(self, task: Task, agent: AgentV2,
                 bus: TaskEventBus, store: TaskStore,
                 template: TaskTemplate): ...

    def run(self) -> None:
        """
        阻塞式运行直到 task.phase == DONE。
        通常在后台线程里调用（submit_task 内部 spawn）。
        线程安全保证：同一 task_id 全局最多一个 TaskLoop 实例。
        """

    # 每个 phase handler 的统一契约：返回 exit 条件是否满足
    def _intake(self) -> bool: ...
    def _plan(self) -> bool: ...
    def _execute(self) -> bool: ...
    def _verify(self) -> bool: ...
    def _deliver(self) -> bool: ...
    def _report(self) -> bool: ...
```

#### 10.4.3 `TaskExecutor`

```python
class TaskExecutor:
    def run_step(self, step: PlanStep) -> bool:
        """推进单 step。返回 step.exit_check 是否满足。"""

    def on_context_pressure(self) -> None:
        """
        内部压缩。触发条件：
          len(task.context.messages) > THRESHOLD 或 token 估算超阈值
        策略：
          保留 tool_use/tool_result 肌肉记忆
          压缩叙述性文本
        """
```

#### 10.4.4 `TaskEventBus`

```python
class TaskEventBus:
    def publish(self, task_id: str, phase: TaskPhase,
                event_type: EventType, payload: dict) -> None:
        """先持久化，再分发给所有 subscribers。"""

    def subscribe(self, task_id: str,
                  handler: Callable[[TaskEvent], None]) -> Callable[[], None]:
        """返回 unsubscribe 闭包。"""

    def replay(self, task_id: str,
               since_ts: float = 0.0) -> list[TaskEvent]:
        """从库回放历史事件，用于 SSE 断线续传。"""
```

#### 10.4.5 `TaskStore`

```python
class TaskStore:
    # Agent
    def save_agent(self, agent: AgentV2) -> None: ...
    def get_agent(self, agent_id: str) -> AgentV2 | None: ...
    def list_agents(self, role: str = "") -> list[AgentV2]: ...

    # Task
    def save(self, task: Task) -> None: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def list_tasks(self, *, agent_id: str = "", status: str = "",
                   limit: int = 50, offset: int = 0) -> list[Task]: ...

    # Event
    def append_event(self, evt: TaskEvent) -> None: ...
    def load_events(self, task_id: str,
                    since_ts: float = 0.0) -> list[TaskEvent]: ...
```

### 10.5 共享服务适配层（L1 桥）

V2 不直接 import V1 的顶层对象。所有向 Layer 1 共享服务的调用走薄薄一层桥：

#### 10.5.1 `v2/bridges/llm_bridge.py`

```python
def call_llm(messages: list[dict],
             tools: list[dict] = None,
             *,
             tier: str = "default",
             max_tokens: int = 4096,
             stream: bool = False) -> dict:
    """
    统一 LLM 调用入口。内部走 app.llm 但不暴露 chat() 语义。
    Returns: {"role":"assistant", "content":..., "tool_calls":[...]}
    """
```

#### 10.5.2 `v2/bridges/skill_bridge.py`

```python
def get_skill_tools_for_agent(agent_v2_id: str) -> list[dict]:
    """
    从 Layer 1 的 skill_registry 查该 agent 被授权的 skill，
    返回 OpenAI-compatible tool schemas。
    """

def invoke_skill(agent_v2_id: str, skill_id: str,
                 args: dict) -> str:
    """执行 skill 工具，返回结果字符串。"""
```

#### 10.5.3 `v2/bridges/mcp_bridge.py`

```python
def get_mcp_tools_for_agent(agent_v2_id: str) -> list[dict]: ...
def invoke_mcp(agent_v2_id: str, tool_name: str, args: dict) -> str: ...
```

**桥的意义**：
- V2 所有业务代码只认 bridge 接口，不认 V1 实现
- V1 废弃时，bridge 内部实现可以替换为 V2 自己的 registry/manager，业务代码零改动

### 10.6 前端 ↔ 后端契约（L4）

#### 10.6.1 路由规则

| URL | 组件 |
|---|---|
| `#/v2` | V2 Tab 默认 TaskBoard |
| `#/v2/tasks/{task_id}` | 打开某个任务详情（Timeline + Console） |
| `#/v2/agents/{agent_id}` | Agent 详情 + 提交新任务 |
| `#/v2/templates` | 模板浏览 |

#### 10.6.2 数据加载契约

- **初次加载**：REST 拉一次初始状态（agents list / tasks list）
- **实时更新**：SSE 订阅推增量事件
- **操作**：REST POST（pause/resume/cancel/clarify）
- **断线重连**：本地保存 `lastEventId`，重连时 `?since=ts`

#### 10.6.3 错误处理契约

- REST 4xx / 5xx：toast 弹出 `error_code` 对应的中文说明
- SSE 断线：5 秒后自动重连（指数退避，最多 1 分钟）
- SSE `event: error`：红色 banner 显示，不清场

---

## 11. 前端设计

### 11.1 Portal 结构改动（最小）

在 `app/templates/portal.html` 最顶部加一个 Tab 切换，**其他一行不动**：

```html
<div id="version-tabs">
  <button class="tab active" data-tab="v1">V1 Classic</button>
  <button class="tab" data-tab="v2">V2 Tasks (Beta)</button>
</div>
<div id="tab-content-v1"> <!-- 原有内容整体包进来 --> </div>
<div id="tab-content-v2" style="display:none;"> <!-- V2 UI --> </div>
```

### 11.2 V2 三栏布局

```
┌───────────────────┬──────────────────────────────┬────────────────────┐
│                   │                              │                    │
│   TaskBoard       │   TaskTimeline               │   TaskConsole      │
│                   │                              │                    │
│  ┌──────────────┐ │  🟦 Task #T-042              │  ┌──────────────┐  │
│  │ Running  (3) │ │  重新制作 PPTX 并发送邮件     │  │ Intent 输入   │  │
│  │ ─────────── │ │  ━━━━━━━━━━━━━━━━━━━━━━━    │  └──────────────┘  │
│  │ T-042 ● 2m   │ │                              │                    │
│  │ T-041 ● 8m   │ │  ✅ Intake         120ms     │  [Submit] [Template│
│  │ T-040 ○ 15m  │ │  ✅ Plan     4 steps  2.1s   │                    │
│  │              │ │  🔄 Execute   step 2/4       │  ─── Artifacts ─── │
│  │ Done (12)    │ │    ├─ ✅ s1 调研            │  📄 research.md    │
│  │ T-039 ✓      │ │    ├─ 🔄 s2 对比竞品 8s     │  📊 report.pptx    │
│  │ T-038 ✗      │ │    │   🔧 web_search        │  ✉️ email.sent     │
│  │ ...          │ │    │   🔧 rag_query         │                    │
│  └──────────────┘ │    │   📊 已收集5家           │  ─── Lessons ──── │
│                   │    ├─ ⬜ s3 生成 PPTX        │  L-1 Intake 漏填 │
│                   │    └─ ⬜ s4 发送邮件          │       region 槽位   │
│                   │  ⬜ Verify                   │                    │
│                   │  ⬜ Deliver                  │                    │
│                   │  ⬜ Report                   │                    │
└───────────────────┴──────────────────────────────┴────────────────────┘
```

### 11.3 组件清单

| 组件 | 文件 | 职责 |
|---|---|---|
| `TaskBoard` | `static/v2/task_board.js` | 任务列表，按 status 分组，点击切换 Timeline |
| `TaskTimeline` | `static/v2/task_timeline.js` | 当前任务的阶段树 + 事件流，SSE 订阅 |
| `TaskConsole` | `static/v2/task_console.js` | 新任务输入、反问回答、artifacts/lessons 面板 |
| `TaskEventRenderer` | `static/v2/event_renderer.js` | 按事件 type 渲染图标/文本 |
| `V2App` | `static/v2/app.js` | 三栏协调、路由（URL = `#/v2/tasks/t_xxx`） |

### 11.4 前端状态同步

- SSE 是**主传输**；REST 只用于初始化和操作动作
- 断线重连：本地保存最后一个事件 `ts`，重连用 `?since=ts`
- 同时打开多个标签：每个标签独立订阅，由 TaskEventBus 多路推送

---

## 12. 任务模板（YAML）

### 12.1 Schema

```yaml
# app/v2/templates/<template_id>.yaml

id: research_report
display_name: 研究报告任务
version: 1
description: 给定主题，产出 PPTX + 邮件发送

# Intake 阶段要抽的槽位
required_slots:
  - name: topic
    description: 报告主题
    examples: ["VMware 中东市场", "自动驾驶芯片竞品"]
  - name: region
    description: 地区
    optional: true
  - name: delivery
    description: 交付方式
    default: "email"

# Plan 阶段的 prompt 提示
plan_prompt: |
  你要做一份 {topic} 的研究报告并 {delivery}。
  请产出 3-6 个 step，每个 step 指定需要的工具和 exit_check。

# Execute 阶段允许用的工具/skill
allowed_tools:
  - web_search
  - rag_query
  - create_pptx
  - send_email

# Verify 阶段规则
verify_rules:
  - id: has_executive_summary
    kind: contains_section
    spec: {section: "## Summary"}
  - id: min_data_points
    kind: json_schema
    spec: {path: "artifacts[*].summary", min_words: 200}
  - id: email_sent
    kind: tool_used
    spec: {tool: "send_email"}

# Deliver 阶段交付契约
expected_artifacts:
  - kind: file
    pattern: "*.pptx"
    min_count: 1
  - kind: email
    min_count: 1

# Report 阶段汇报模板（可选，不写则 LLM 自由发挥）
report_template: |
  ✅ 研究报告已完成
  - 主题：{topic}
  - 产出：{artifact_count} 份文件
  - 已发送至：{email_address}
```

### 12.2 首批模板（V2 上线随代码发布）

| id | 用途 |
|---|---|
| `conversation` | 纯对话（兼容 V1 chat 语义，供"从 V1 克隆" 的 agent 过渡使用） |
| `research_report` | 研究型任务（菜小二场景） |
| `meeting_summary` | 会议纪要 |

其他模板由管理员或 agent 作者后续补充。

---

## 13. V1/V2 共存策略

### 13.1 共享底层清单（允许 V2 import）

| V1 模块 | V2 用途 |
|---|---|
| `app.llm.get_provider()` | LLM 调用 |
| `app.skills.registry.get_registry()` | skill 元数据查询 |
| `app.skills.store.get_store()` | skill 授权/撤销（共享） |
| `app.mcp.manager.get_mcp_manager()` | MCP 工具调用 |
| `app.auth.*` | 认证中间件 |
| `app.runtime_paths.*` | 路径 |

### 13.2 禁用清单（pre-commit 强制检查）

```python
# scripts/check_v2_isolation.py

FORBIDDEN_IMPORTS_IN_V2 = [
    r"^from app\.agent import",
    r"^from app\.agent ",
    r"^import app\.agent\b",
    r"^from app\.agent_llm",
    r"^from app\.agent_execution",
    r"^from app\.hub\._core",
    r"^from app\.workflow",
    r"^from app\.persona",
]
```

违反 → 提交被 block。

### 13.3 资源共享的安全边界

| 资源 | 共享方式 | 风险 |
|---|---|---|
| SQLite 连接 | 同文件，不同表 | 低（表隔离） |
| Skill Registry | 同一 registry 实例 | 中（`granted_to` 字段要支持 agent_v2 id） |
| MCP Manager | 同一 manager 实例 | 中（`bind_mcp_to_agent` 要支持 v2 agent） |
| LLM Provider | 无状态，可直接共享 | 零 |
| 文件系统 | V1 在 `agents/{id}/`，V2 在 `v2/agents/{id}/` | 零 |

### 13.4 V1 agent 克隆到 V2

```
POST /api/v2/agents/{v2_id}/clone_from_v1
body: {"v1_agent_id": "adf1371a6e72"}
```

行为：
- 复制 V1 agent 的 name / role / working_directory
- 复制 capabilities：granted_skills → v2.capabilities.skills；mcp_servers → v2.capabilities.mcps
- **不复制** messages（V2 的 messages 属于 task）
- 记录 `v2.v1_agent_id` 便于追溯
- V1 agent 保持不变，继续跑

---

## 14. 迁移与毕业计划

### 14.1 时间线

| 时间 | 阶段 | 状态 |
|---|---|---|
| W1 | 开发 V2 Core | V1 照常运行 |
| W2–W3 | 3 个 V2 agent 试运行 | 用户同时看两个 Tab |
| W4–W5 | 稳定性观察期 | 收集 KPI / bug |
| **M2** | **毕业评审** | 通过 → 新建 agent 默认 V2 |
| M2–M5 | V1 冻结期 | 只修安全 bug |
| **M8** | **V1 废弃** | 代码 archive 到 `legacy/` 分支 |

### 14.2 毕业标准（必须全部满足）

| # | 标准 | 验证方式 |
|---|---|---|
| A | 3 个不同角色的 V2 agent 连续 2 周无高优 bug | 事件流里 `task_failed` 比例 < 5%，无崩溃 |
| B | V2 能跑通 V1 卡死的菜小二 PPTX 场景 | 端到端测试用例 + 人工验证 |
| C | 所有 V2 代码通过 isolation check | pre-commit 零违规 |
| D | Task 事件流重放功能正常 | 任意历史 task 可 replay 出完整时间线 |

### 14.3 冻结 V1 的含义

- V1 代码只合并"安全 bug + 数据兼容"的 PR
- 不接受新功能
- 新 agent 必须 V2
- 老 V1 agent 提供"一键克隆为 V2"，但不强制

### 14.4 如果 V2 没毕业

- M2 评审不通过 → 给出具体阻塞项
- 最多 2 个 M1-长度的延期
- 再延期仍不通过 → **V2 归档，回到 V1 + 补丁路线**
- 这是防止"永久双轨"的硬约束

---

## 15. 风险与缓解

| 风险 | 影响 | 概率 | 缓解 |
|---|---|---|---|
| 用户不切 V2 Tab，V2 没人用 | V2 毕业失败 | 中 | W2 开始每周观察 V2 使用率；低于阈值主动引导 |
| 共享 Skill Registry 的 granted_to 字段冲突 | 权限串 | 低 | V2 agent id 用独立前缀 `v2_`；registry 侧按前缀区分 |
| 事件流对 SQLite 写入压力大 | 性能降级 | 中 | task_events_v2 加索引；长跑任务定期归档 |
| YAML 模板被乱改导致 exit 条件永远不满足 | 任务死循环 | 中 | TaskLoop 硬重试上限兜底；模板入库前 schema 校验 |
| LLM 在 Plan 阶段输出不合法 JSON | Plan 阶段反复失败 | 中 | 3 次失败后走 soft_fail；prompt 里强约束 JSON 格式 |
| 前端 SSE 断连 | 时间线停更 | 中 | `?since=ts` 断点续传；本地状态缓存 |
| V2 实现和 V1 共享了 LLM 但用法不一 | LLM 层暴露接口不够 | 低 | V2 开发初期就定义 `llm_bridge.py` 适配层 |
| "双轨化" 心态，V2 永远"测试" | 技术债累积 | **高** | **硬毕业标准 + 强制冻结时间线** |

---

## 16. 未决问题（需决策）

| # | 问题 | 选项 | 默认建议 |
|---|---|---|---|
| Q1 | V2 agent 是否支持接收外部 webhook 任务？ | 支持 / 不支持 | 不支持（M1 不做） |
| Q2 | 同一 agent 能并发跑几个 task？ | 1 / N | 1（M1）；M3 再开放并发 |
| Q3 | 子任务（parent_task_id）的事件是否聚合到父任务时间线？ | 是 / 否 | 是（推体验） |
| Q4 | Intake 反问走聊天框还是独立弹窗？ | 聊天 / 弹窗 | 聊天（TaskConsole 内嵌） |
| Q5 | LLM 档位（llm_tier）是否 M1 就落地？ | 是 / 否 | 是（便宜的 tier 给 Intake，昂贵的给 Plan/Report） |
| Q6 | V2 事件流写 SQLite 还是用内存 ring buffer + 落盘？ | SQLite / 混合 | M1 SQLite 足够；性能瓶颈再改 |
| Q7 | V1 的 `hub.py` 协调功能（跨 agent 消息）V2 对应的是什么？ | 平行新建 / 复用 V1 | M1 不处理跨 agent，M3 评估 |

---

## 17. 工期与里程碑

### 17.1 总体工期（7 天）

| Day | 任务 | 交付 |
|---|---|---|
| D1 | 核心抽象 + Store | `task.py` / `task_store.py` / 3 张表创建 / 单测 |
| D2 | TaskLoop + TaskExecutor | 6 phase handler / nudge / 压缩 |
| D3 | TaskEventBus + 一个 YAML 模板（conversation） | 事件持久化 + replay |
| D4 | AgentV2 瘦壳 + REST API | 能创建 agent、提交 task |
| D5 | SSE + 前端 TaskBoard/TaskTimeline/TaskConsole | 基本可用的 UI |
| D6 | 前端完善 + V1 克隆 + research_report 模板 | 跑通菜小二场景 |
| D7 | 测试 + 文档 + pre-commit isolation 脚本 | 可交付 |

### 17.2 里程碑

- **M0（D7）**: V2 Core 可运行；内部试用开始
- **M1（D7+2 weeks）**: 3 个 V2 agent 上线
- **M2（D7+6 weeks）**: 毕业评审
- **M5（D7+6 months）**: V1 冻结 → 废弃

---

## 18. 附录

### A. 完整 YAML 模板示例（`meeting_summary.yaml`）

```yaml
id: meeting_summary
display_name: 会议纪要
version: 1

required_slots:
  - name: transcript
    description: 会议转录文本
  - name: deliverable
    default: "markdown"

plan_prompt: |
  根据 transcript 产出会议纪要：
  - 提取 Decisions / Action Items / Risks
  - 产出 markdown 纪要
  - 可选：同步到 agent 邻居

allowed_tools:
  - rag_query
  - create_markdown
  - send_message_to_agent

verify_rules:
  - id: has_action_items
    kind: contains_section
    spec: {section: "## Action Items"}
  - id: min_sections
    kind: regex
    spec: {pattern: "^## (Summary|Decisions|Action Items|Risks)", min_matches: 3}

expected_artifacts:
  - kind: file
    pattern: "*.md"
    min_count: 1
```

### B. 事件 Payload Schema（按 type 分）

```python
# app/v2/core/event_schemas.py

EVENT_PAYLOAD_SCHEMAS = {
    "task_submitted": {
        "type": "object",
        "required": ["intent", "template_id"],
        "properties": {"intent": {"type": "string"}, "template_id": {"type": "string"}},
    },
    "tool_call": {
        "type": "object",
        "required": ["step_id", "tool", "args"],
        "properties": {
            "step_id": {"type": "string"},
            "tool": {"type": "string"},
            "args": {"type": "object"},
        },
    },
    "tool_result": {
        "type": "object",
        "required": ["step_id", "tool", "summary"],
        "properties": {
            "step_id": {"type": "string"},
            "tool": {"type": "string"},
            "summary": {"type": "string", "maxLength": 500},
            "full_in_db": {"type": "boolean"},   # 完整内容是否落库
        },
    },
    "artifact_created": {
        "type": "object",
        "required": ["artifact"],
        "properties": {
            "artifact": {
                "type": "object",
                "required": ["id", "kind", "handle"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["file", "email", "rag_entry",
                                      "api_call", "message"]},
                    "handle": {"type": "string"},
                    "summary": {"type": "string"},
                },
            }
        },
    },
    # ... 其他事件类似
}
```

### C. 菜小二端到端演练（"重新制作 PPTX 发邮件"）

```
时间 | 事件 | 说明
-----|------|------
T+0   | task_submitted            intent="重新制作..."
T+0.1 | phase_enter phase=intake
T+0.8 | intake_slots_filled       {topic:"VMware 中东市场", delivery:"email",
                                   new_info_required: true}
T+0.9 | phase_exit phase=intake ok=true
T+0.9 | phase_enter phase=plan
T+2.3 | plan_draft                steps=4 ["调研","对比","生成PPTX","发邮件"]
T+2.4 | plan_approved
T+2.4 | phase_exit phase=plan ok=true
T+2.5 | phase_enter phase=execute
T+2.5 | step_enter s1 "调研"
T+3.0 | tool_call web_search      {q:"VMware Middle East market share 2025"}
T+5.1 | tool_result               summary="找到 5 家竞品数据..."
T+5.5 | tool_call rag_query       {q:"中东IT企业合规"}
T+6.8 | tool_result               summary="12 条相关"
T+7.2 | progress                  "收集了市占率+合规材料，开始整理..."
T+9.0 | artifact_created          kind=research_notes handle=".../notes-s1.md"
T+9.1 | step_exit s1 ok=true
T+9.1 | step_enter s2 "对比"
...
T+45  | step_exit s4 ok=true
T+45  | phase_exit phase=execute ok=true
T+45  | phase_enter phase=verify
T+46  | verify_check has_executive_summary passed=true
T+47  | verify_check min_data_points passed=false  ← 数据段字数不足
T+47  | verify_retry attempt=1 failing=[min_data_points]
T+47  | phase_enter phase=execute  ← 回退
T+52  | step_exit s3 ok=true       ← 重跑 s3 补数据
T+52  | phase_enter phase=verify
T+53  | verify_check ... 全部 passed
T+53  | phase_exit phase=verify ok=true
T+53  | phase_enter phase=deliver
T+55  | artifact_created kind=file handle=".../report-v2.pptx"
T+58  | artifact_created kind=email handle="mail_id:ABC123"
T+58  | phase_exit phase=deliver ok=true
T+58  | phase_enter phase=report
T+60  | task_completed summary="..." artifact_count=3 duration_s=60
```

V1 里这个场景会在 T+0.5 卡住（LLM 说"好的我立即..."然后停）。V2 里 TaskLoop 在每个 phase 有 exit 条件，stuck 会触发 nudge 重试，不会沉默。

### D. Pre-commit 隔离检查脚本

```python
#!/usr/bin/env python3
# scripts/check_v2_isolation.py

import re
import sys
from pathlib import Path

FORBIDDEN = [
    re.compile(r"^\s*from\s+app\.agent\s+import"),
    re.compile(r"^\s*from\s+app\.agent\.\w+"),
    re.compile(r"^\s*import\s+app\.agent\b"),
    re.compile(r"^\s*from\s+app\.agent_llm"),
    re.compile(r"^\s*from\s+app\.agent_execution"),
    re.compile(r"^\s*from\s+app\.hub\._core"),
    re.compile(r"^\s*from\s+app\.hub\s+import\s+Hub\b"),
    re.compile(r"^\s*from\s+app\.workflow"),
    re.compile(r"^\s*from\s+app\.persona"),
]

def main() -> int:
    root = Path(__file__).resolve().parent.parent / "app" / "v2"
    if not root.exists():
        return 0
    violations = []
    for py in root.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for pat in FORBIDDEN:
                if pat.search(line):
                    violations.append(f"{py}:{i}: {line.strip()}")
    if violations:
        print("V2 isolation violations:")
        for v in violations:
            print(" ", v)
        print("\nV2 modules must not import V1 top-level modules.")
        print("Use shared low-level services only (see PRD §13.1).")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### E. 对照 V1 概念的迁移表

| V1 概念 | V2 归属 | 变化 |
|---|---|---|
| `agent.chat(msg)` | `agent.submit_task(intent)` + `template=conversation` | 顶层 API 改变 |
| `agent.messages` | `task.context.messages` | 归属改变（属 task） |
| `agent.granted_skills` | `agent.capabilities.skills` + `task.capabilities_snapshot` | 声明/快照分离 |
| `agent.profile.mcp_servers` | `agent.capabilities.mcps` | 归拢 |
| `AgentEvent` | `TaskEvent` | 聚合键改 agent_id → task_id |
| `_compress_context()` | `TaskExecutor.on_context_pressure()` | 降级为内部方法 |
| `ExecutionAnalyzer` | Execute 阶段事件 + Verify 阶段规则 | 拆解 |
| `WorkflowTemplate` + `WorkflowInstance` | `TaskTemplate` + `Task` | 统一到同一模型 |
| `ExperienceLibrary` | `task.lessons` + 启动时查询 | 内聚 |
| `QualityGate`（规划中） | Verify 阶段 + verify_rules | 不再独立 |
| `RolePresetV2`（规划中） | Agent + TaskTemplate 组合 | 不再独立 |

---

## 19. 评审要点

评审时请至少对以下 7 点给出结论：

1. ✅/❌ 5 个核心类是否足够且不冗余？
2. ✅/❌ 3 张表 schema 是否合理？是否有字段应上升为独立表？
3. ✅/❌ 6 阶段划分是否恰当？是否需要合并/拆分？
4. ✅/❌ 事件类型闭集是否够用？
5. ✅/❌ 共享底层清单 + 禁用清单是否合理？
6. ✅/❌ 毕业标准是否可量化、可考核？
7. ✅/❌ 7 天工期是否可信（给出 P50 / P90 估计）？

评审通过 → 进入详细实施方案（`plans/agent-v2-core.md`）编写阶段。

---

*— 文档结束 —*
