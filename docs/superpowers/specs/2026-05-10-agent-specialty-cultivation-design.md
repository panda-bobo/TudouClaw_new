# Agent Specialty Cultivation System — Design Spec

> **Status:** Draft v1.1 (post-review-round-1, score 92/100)
> **Date:** 2026-05-10
> **Skill chain:** superpowers:brainstorming → (this doc) → superpowers:writing-plans → implementation
> **Owner:** TudouClaw maintainer
> **Sub-projects:** SP-0 (UI 整合) → SP-1 (Foundation + RAG) → SP-2 (LoRA pipeline) → SP-3 (Routing + production loop)
> **v1.1 changes:** §17 列出对应评审 7 项优化建议的所有补丁位置

---

## 1. 一句话目标

让任意 Tudou agent 通过一套可复用的「养成」流程,演化成某领域(法律/医疗/财务等)的专家:**对话身份不变,内部能力和知识层逐步累积、可视化进度,且专家数据独立持久化、可备份迁移。**

第一个验证用例:**小法 (Legal Expert)** —— 对常见合同/劳动/民事问题准确率 ≥ ChatGPT,带可点开溯源的法条/判例引用,云 LLM 调用降至 < 20%。

---

## 2. Architecture(分层概览)

```
┌───────────────────────────────────────────────────────────────────┐
│                         USER INTERACTION                            │
│    所有交互在 Agent Workspace (统一 5-tab 工作区) 内完成             │
│    [💬 对话] [🧰 能力] [🎓 养成] [📊 历史] [⚙️ 配置]                 │
└──────────────────────────────┬────────────────────────────────────┘
                               │ (用户跟 agent 自身对话, 不切身份)
                               ↓
┌───────────────────────────────────────────────────────────────────┐
│  AGENT REPLY PIPELINE (现有,加 1 个分支)                             │
│  if agent.expert_specialty:                                         │
│      → expert.pipeline.answer(agent_id, query)                      │
│  else:                                                              │
│      → 现有 LLM 路径 (零变化)                                        │
└──────────────────────────────┬────────────────────────────────────┘
                               │
                               ↓
┌───────────────────────────────────────────────────────────────────┐
│  CULTIVATION SYSTEM   (app/domain_expert/, 全新隔离模块)              │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  SpecialtyTemplate (YAML 配方)                                │  │
│  │      ├── required_prompt_packs                                │  │
│  │      ├── required_skills                                      │  │
│  │      ├── recommended_mcps                                     │  │
│  │      ├── recommended_corpus                                   │  │
│  │      ├── training params                                      │  │
│  │      └── level_rules                                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│  ┌────────────┬────────────┬────────────┬────────────┬─────────┐  │
│  │ 1.Bundle   │ 2.Knowledge│ 3.Transition│4.Inference│ 5.Eval  │  │
│  │  Apply     │  Layer(RAG)│  Pipeline  │  Pipeline │ & Routing │  │
│  │            │            │            │            │         │  │
│  │ - grant    │ - corpus   │ - traces   │ - pipeline │ - eval  │  │
│  │ - bind     │ - vector   │ - synth    │ - safety   │ - level │  │
│  │ - install  │ - embedder │ - trainer  │ - cite     │   推进   │  │
│  │   MCP      │ - reranker │ - DPO      │ - routing  │         │  │
│  └────────────┴────────────┴────────────┴────────────┴─────────┘  │
└──────────┬─────────────────────┬─────────────────────┬───────────┘
           ↓                     ↓                     ↓
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ 向量库 + 语料     │   │ 本地 LLM         │   │ 云 LLM (兜底)     │
│ sqlite-vss       │   │ Qwen2.5-7B       │   │ Claude/GPT/Groq   │
│ ~3 GB            │   │ + LoRA adapter   │   │                   │
│                  │   │ via mlx-lm       │   │                   │
└──────────────────┘   └──────────────────┘   └──────────────────┘

       ─────────── 所有数据落 ~/.tudou_claw/expert/<agent_id>/ ────────────
```

### 2.1 关键架构性质

| 性质 | 含义 | 验收 |
|---|---|---|
| **横向隔离** | 整个 cultivation 在 `app/domain_expert/` 独立模块。`expert_specialty=""` 的 agent 走原路径,零行为差异 | 删 `app/domain_expert/` + agent 一个字段 = 完整还原 |
| **纵向分层** | 5 层(Template → Bundle → Knowledge → Transition → Inference)接口稳定 | 每层可独立替换实现,如 sqlite-vss → chromadb |
| **多 profile 并行** | 每个 specialty agent 自己的 corpus + LoRA + traces,互不干扰 | 同台 Tudou 可同时养小法 + 小医 |
| **降级安全** | LoRA 加载失败 → 走 RAG + 云 LLM;cultivation 整体崩 → 走原 agent 路径 | 任意层级故障不影响 agent 基础对话 |
| **数据可移植** | `tar czf 小土-expert.tgz expert/0c0877.../` 即完整迁移 | 备份/还原跨机器 |

---

## 3. 核心概念定义

### 3.1 SpecialtyTemplate(专家配方)

一个 YAML/JSON 文件,定义"该领域的专家应该具备什么"。**配方文件本身就是声明式定义,不含代码**。

#### 3.1.1 文件位置
```
app/data/specialty_templates/
├── legal.yaml       ← SP-1 实装
├── medical.yaml     ← SP-1 不做,占位
├── finance.yaml     ← SP-1 不做,占位
└── _schema.json     ← JSON Schema, 校验用
```

#### 3.1.2 Schema(关键字段)

```yaml
# legal.yaml
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
icon: ⚖️
description: 中国法系,侧重合同 / 劳动 / 民事

# ── Capability Bundle (复用 Tudou 现有 skill / pack 体系) ──
required_prompt_packs:
  - id: agency_legal_lawyer
  - id: agency_legal_legal_counsel
  - id: agency_legal_contract_lawyer
  - id: akwp_legal_brief
  - id: akwp_legal_review-contract
  - id: akwp_legal_compliance-check
  - id: akwp_legal_triage-nda
  - id: akwp_legal_legal-risk-assessment
  # ... 共 12 项
required_skills:
  - id: legal_doc_search
  - id: citation_validator
  - id: contract_clause_analyzer
recommended_mcps:
  - name: legal_database_mcp
    optional: true

# ── Knowledge Layer (RAG) ──
recommended_corpus:
  - source: flk_npc                # 国家法规库爬虫
    estimated_size: 1.2GB
  - source: hf:disc-law-sft        # HuggingFace 数据集
    estimated_size: 800MB
  - source: hf:cail2018-2019       # 判决文书摘要
    estimated_size: 1.5GB

# ── Training (SP-2) ──
training:
  base_model: Qwen2.5-7B-Instruct
  lora_r: 16
  raft_data_target: 5000
  refresh_cadence_days: 30

# ── 🆕 v1.1: Specialty 专属 Eval Suite ──
# 每个 specialty 自己定义"该领域的硬评测"。系统 schema 通用;
# 内容(数据集名、评分函数、阈值)由各 yaml 自己声明。
# 这一节列出可用的评测项;具体阈值在 level_rules.benchmarks 里引用。
eval_suite:
  - id: legalbench_zh
    description: 中国法律理解综合评测 (300 题 holdout)
    source: hf:legalbench-zh-holdout
    metric: accuracy
    runner: app.domain_expert.training.eval.LegalBenchRunner
  - id: citation_accuracy
    description: 引用真实性 (回答中所有 [Doc N] 必须真实存在于 retrieval 集)
    metric: ratio
    runner: app.domain_expert.training.eval.CitationValidator
  - id: contract_review_accuracy
    description: 合同审查类样本子集 (用户自建 100 题)
    metric: blind_eval_score
    runner: app.domain_expert.training.eval.BlindReviewer

# ── Level 推进规则 ──
level_rules:
  novice:
    description: 配方应用中
    requirements:
      bundle_complete_pct: "<50"
  journeyman:
    description: 知识层就位 + 工具基本齐
    requirements:
      bundle_complete_pct: ">=50"
      corpus_indexed: true
  expert:
    description: LoRA 落地, 有引用能力
    requirements:
      bundle_complete_pct: ">=80"
      lora_active: true
      # 🆕 v1.1: benchmarks 字段是通用 schema, 每个 specialty 在自己
      # 的 yaml 里声明该领域的硬评测集。法律用 LegalBench-zh,
      # 医疗用 CMB / MedQA-zh, 财务用 FinBench-zh, 等等。
      benchmarks:
        legalbench_zh: ">=0.80"        # 法律专属
        citation_accuracy: "==1.00"     # 必须 100% 真实引用
  master:
    description: 持续迭代, 本地处理为主
    requirements:
      bundle_complete_pct: "100"
      lora_refresh_count: ">=3"
      local_handle_rate_clean: ">=0.7"   # 🆕 v1.1 review #7: 排除敏感/跨域/无解后的净本地率
      benchmarks:
        legalbench_zh: ">=0.85"        # 法律专属, 大师档更高
        citation_accuracy: "==1.00"

# ── Safety ──
safety:
  pipl_redact: true
  required_disclaimer: |
    AI 提供的法律分析仅供参考,非正式法律意见;
    重大决策请咨询执业律师。
```

### 3.2 ExpertSpecialty(agent 上的属性)

Agent dataclass 加 5 个字段(默认全空 = 普通 agent,完全向后兼容):

```python
class Agent:
    # ... 现有字段保持不变 ...

    # ── 专家化属性 ──
    expert_specialty: str = ""           # "" / "legal" / "medical" / ...
    expert_template_version: str = ""    # 应用时锁定的配方版本号
    expert_level: str = "novice"         # novice / journeyman / expert / master
    expert_lora_version: str = ""        # 当前激活的 LoRA, e.g. "v3"
    expert_initialized_at: float = 0.0   # 专家化启动时间
```

**持久化**: 这些字段写入现有 `agents` 表的 `data` JSON blob,不需要 schema migration。

### 3.3 独立持久化的专家数据包

文件系统布局:

```
~/.tudou_claw/expert/<agent_id>/
├── config.json                  # 完整专家化配置(template snapshot + 进度)
├── corpus/                      # 原始语料文件
│   ├── flk_npc_v2026-04/
│   ├── disc_law_sft_v1/
│   └── _manifest.json           # 来源 / 版本 / 入库时间
├── vector_store.db              # sqlite-vss 索引
├── lora/
│   ├── v1/adapter.safetensors
│   ├── v2/adapter.safetensors
│   └── current → v2/            # symlink 标识激活版本
├── traces/                      # 用户问答 trace, 按月分文件夹
│   └── 2026-05/2026-05-10.jsonl
├── datasets/                    # 合成训练集快照
│   └── v2-raft-train.jsonl
└── eval/                        # 评测报告
    ├── v1.json
    └── v2.json
```

**与 agent 的关系**: 按 `agent_id` 索引,但持久化在文件系统,不在 agent JSON 里。删 agent 时 expert 目录可以独立保留(orphan,可以重绑或清理)。

### 3.4 Cultivation Lifecycle(养成生命周期)

7 个 stage,部分自动、部分需用户确认:

| Stage | 名称 | 触发 | 持续时间 | 自动/手动 |
|---|---|---|---|---|
| 0 | Template 选择 | 用户点 [选择 specialty 模板] | 30 秒 | 手动 |
| 1 | Capability Bundle 应用 | Stage 0 确认后 | 几秒 | 自动 |
| 2 | Corpus 下载 | Stage 1 完成后 | 10-30 分钟 | 自动(后台) |
| 3 | Vector Index 构建 | Stage 2 完成后 | 几分钟 | 自动(后台) |
| 4 | Trace 累积 | Stage 3 之后开始,持续累积 | 数周到数月 | 自动 |
| 5 | LoRA 训练触发 | Trace ≥ 1k AND 用户点 [开始训练] | 8-12 小时 | 手动触发,后台跑 |
| 6 | Eval + Routing 启用 | Stage 5 后 eval 通过门槛 | 几分钟 | 自动 |
| 7 | 持续 refine (DPO + 增量) | 长期 | 月度 cron | 自动(可关) |

### 3.5 段位推进(养成可视化)

```
🌱 见习 (novice)
   Stage 0-3 进行中, 无 LoRA
   "我有法律 prompt pack, 但没工具, corpus 还在索引"
       ↓
🌿 熟手 (journeyman)
   Bundle ≥ 50%, corpus 已索引
   "我会查法条 + 引用, 但风格还像通用 LLM"
       ↓
🎯 专家 (expert)
   LoRA v1+ 激活, eval 通过
   "我说话像律师, 本地能答常见问题"
       ↓
🏆 大师 (master)
   多次 refresh, 本地处理率 ≥ 70%
   "我跟你混熟了, 80% 不再求助云 LLM"
```

**晋升/降级规则**: 由 `level_rules`(见 3.1.2)按客观信号自动判定,**不是 LLM 自吹**。Eval 退化(数据漂移、LoRA 训歪)会自动降级 + 暂停 routing 走云端兜底。

### 3.6 Cultivation Pipeline Visualization(顺序图 + 交互枢纽)

**这是养成 tab 的主可视化 + 主交互入口**。横向进度链显示 6 个模块,每个模块**可点击进入,执行该模块允许的操作以推进专家成熟度**。

#### 3.6.1 顺序图(终态例示)

```
养成进度 (Cultivation Pipeline)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │📋 配方    │  │🛠 能力    │  │📚 知识库  │  │📊 训练数据│  │🧠 LoRA   │  │🎯 部署    │
  │TEMPLATE   │  │BUNDLE     │  │KNOWLEDGE  │  │TRACE      │  │TRAINING   │  │ROUTING    │
  ├──────────┤  ├──────────┤  ├──────────┤  ├──────────┤  ├──────────┤  ├──────────┤
  │   ✓      │  │   ✓      │  │   ✓      │  │   ⏳     │  │   ⏸      │  │   ⏸      │
  │ legal    │  │ 12/12 packs│ │ 4.2 万条  │  │ 287/1000 │  │ 等待中    │  │ 等待中    │
  │ v1.0     │  │  3/3 skills│ │ FLK+DISC  │  │ trace    │  │           │  │           │
  │          │  │  1/1 mcp   │ │ +CAIL     │  │          │  │ (need 1k+)│  │           │
  │████ 100% │  │████ 100% │  │████ 100% │  │███░ 28.7%│  │░░░░  0%  │  │░░░░  0%  │
  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘
       │             │             │             │             │             │
       ●─────────────●─────────────●─────────────●─────────────○─────────────○

   ↑ 可点击,进入        ↑ 可点击,进入       ↑ 可点击 ⭐         ↑ 可点击          ↑ 可点击 ⭐         ↑ 可点击
   切换 / 查看配方     管理 packs/skills    上传新语料         浏览/筛选 traces    触发训练 / 切换版本   调路由策略

  当前段位: 🌿 熟手 (62%)              下一段位: 🎯 专家 (需 LoRA v1 + eval ≥ 0.8)
  ████████████░░░░░░░░ 62%
```

#### 3.6.2 6 个模块定义

| # | 模块 | 显示指标 | 完成判定 | 数据源 |
|---|---|---|---|---|
| 1 | 📋 **Template** | 配方名 + 版本号 | 已选 specialty ≠ "" | `agent.expert_specialty` + `expert_template_version` |
| 2 | 🛠 **Bundle** | `bound_packs/total` `granted_skills/total` `mcps/total` | 三项 all ≥ 80% | `agent.bound_prompt_packs` ∩ template + `granted_skills` + `mcp_servers` |
| 3 | 📚 **Knowledge** | corpus 条目数 + 索引状态 + 来源数 | `corpus_indexed=true` | `~/.tudou_claw/expert/<id>/vector_store.db` 行数 + `corpus/_manifest.json` |
| 4 | 📊 **Trace** | 累积 trace 数 / `raft_data_target` | trace ≥ `raft_data_target` | `traces/*.jsonl` 文件总行数 |
| 5 | 🧠 **LoRA Training** | active LoRA 版本 + 最新 eval score | `active_lora_version != ""` AND `eval >= 0.8` | `lora/current` symlink + `eval/<v>.json` |
| 6 | 🎯 **Production Routing** | 本地处理率 % | `local_handle_rate >= 0.6` | `stats.json` 运行时累积 |

#### 3.6.3 状态枚举(每个模块)

| 状态 | 视觉 | 含义 |
|---|---|---|
| **✓ 完成** | 绿色 + check + 100% 进度条 | 该模块达标,贡献给段位推进 |
| **⏳ 进行中** | 黄色 + 当前数字 + 部分进度条 | 正在累积/处理 |
| **⏸ 等待** | 灰色 + 前置条件提示 | 上游模块未完成,无法启动 |
| **✗ 失败** | 红色 + 错误描述 + [重试] 按钮 | 出错(下载失败/训练崩等),用户可重试 |

#### 3.6.4 点击模块的钻取交互

**全部 inline 在养成 tab 内部展开,不弹独立 modal**(SP-0 整合原则)。点击任一模块:
1. 顶部 pipeline 高亮该模块
2. 下方 detail panel 切换为该模块的操作面
3. 顶部出现 [← 返回总览] 按钮

各模块 detail panel 允许的操作:

| 模块 | 钻取后可做 |
|---|---|
| 📋 Template | 查看当前配方 YAML / 切换配方(危险操作,二次确认) / 检查上游版本更新 |
| 🛠 Bundle | 查看所有 bound packs / granted skills / mcps 清单 / 单独 enable/disable / 跳转到现有"能力 tab"做更细粒度管理 |
| 📚 **Knowledge ⭐** | **上传新语料文件 / 添加新来源 / 触发增量索引 / 删除某来源 / 查看 chunks 详情** |
| 📊 Trace | 浏览 trace 历史 / 按 feedback 筛选 / 标记低质 / 导出 RAFT 训练集预览 / 数据集合成历史 |
| 🧠 **LoRA Training ⭐** | **触发新一轮训练(需 trace ≥ 1k) / 训练历史 / loss 曲线 / 切换激活 LoRA 版本 / 一键回滚** |
| 🎯 Routing | 调 confidence_threshold / 查看本地 vs 云分布 / 强制全云端模式(临时禁本地) / 强制全本地模式(测试用) |

⭐ 标记的是**用户主动推进专家成熟度的关键操作**(用户原话:"导入新的数据,再做 LoRA 训练,向前推进专家成熟度")。这两个模块的 detail panel 是养成系统的真正发动机。

#### 3.6.5 实时刷新

- **进入养成 tab 时拉一次** 全部 6 个模块状态
- **每 10 秒轮询** 一次(只刷在进行中的模块)
- 训练 / 索引这种长任务通过 SSE 推送进度,不依赖轮询
- 模块切换钻取时刷新该模块的 detail data

#### 3.6.6 段位与 Pipeline 的对应关系

下半部分的段位条不是独立指标,**直接由 pipeline 6 个模块的完成状态算出**:

```
🌱 见习      ─ 模块 1, 2 进行中
🌿 熟手      ─ 模块 1-3 全完成 + 模块 4 累积中
🎯 专家      ─ 模块 1-5 全完成 + eval ≥ 0.8
🏆 大师      ─ 模块 1-6 全完成 + 已 refresh ≥ 3 次
```

可视化上,pipeline 进度推进 = 段位条推进。**用户每次给某个模块"喂数据 / 下指令",立刻看到段位向前一格**。

#### 3.6.7 Pipeline 上的操作触发段位推进的典型流程

```
用户进养成 tab,看到 📚 Knowledge 已完成 / 📊 Trace 287/1000
   ↓
两周后日常使用 agent 一段时间, Trace 自动累积到 1000+
   ↓
用户进养成 tab,发现 🧠 LoRA Training 现在不再是 ⏸ 等待,变 ⏳ 可触发
   ↓
点击 🧠 模块 → detail panel 显示 [开始训练 (1247 traces 就绪)]
   ↓
点 [开始训练] → 后台 8-12 小时
   ↓
训练完成 + eval 自动跑 + score=0.85
   ↓
🧠 模块从 ⏳ 变 ✓ + 段位条从 62% 跳到 78% + 触发 🎯 段位达成动画
```

这就是用户原话"向前推进专家成熟度"的完整闭环。

### 3.7 Trace 数据双源 Feed(组织内外两条流入)

📊 Trace 模块的数据有 **两个独立来源**,都汇入同一个 trace pool 喂给 RAFT 合成 + LoRA 训练:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Trace Pool (统一存储)                            │
│         ~/.tudou_claw/expert/<agent_id>/traces/                          │
│                                                                          │
│         记录: { Q, retrieved_docs, A, feedback, origin, ... }            │
└─────────────────────┬─────────────────────────┬─────────────────────────┘
                      ↑                         ↑
                      │                         │
        ┌─────────────┴────────────┐  ┌─────────┴────────────┐
        │   🟢 内生源 (Organic)     │  │   🟦 外部源 (Imported) │
        │   日常 agent 工作总结     │  │   用户主动导入         │
        ├──────────────────────────┤  ├──────────────────────┤
        │ 来源:                    │  │ 来源:                  │
        │ • 真实用户-agent 对话     │  │ • 公开数据集 (HF/CAIL) │
        │ • 多轮对话被 LLM 蒸馏     │  │ • 用户自有 Q/A 库      │
        │   成单 (Q, A) 对          │  │ • 律师亲手标注          │
        │ • 用户 👍/👎 反馈        │  │ • 其它 Tudou 实例 export│
        │                          │  │                       │
        │ 标签 origin: "organic"   │  │ 标签 origin: "import" │
        │                          │  │ 标签 source: <tag>    │
        │ 速率: 慢 (~50/天)        │  │ 速率: 一次性大量      │
        │ 质量: 不稳定 (依赖用户)   │  │ 质量: 高 (可预筛)      │
        │ 成本: 0                  │  │ 成本: 0 / 数据费       │
        │ 个性化: 强 (你的常用问题) │  │ 个性化: 弱 (通用语料)  │
        └──────────────────────────┘  └──────────────────────┘
```

#### 3.7.1 双源的互补价值

| 角色 | 内生源 (organic) | 外部源 (imported) |
|---|---|---|
| **冷启动加速** | 慢 (要 1-3 个月攒到 1k+) | 快 (一晚上导入 10k 高质量样本) |
| **领域广度** | 用户日常问什么就有什么 | 覆盖标准化训练集所有题型 |
| **个性化深度** | 强 (你的合同模板, 你的话术习惯) | 弱 (通用律师风格) |
| **质量可控** | 受用户提问质量影响 | 用户可预筛, 质量稳定 |

**最佳实践**: SP-1 上线第一周, 用户**导入一个 5k+ 的专业 Q/A 数据集做底子**, 之后日常使用通过 organic trace 累积做个性化微调。 训练时这两类都进 RAFT pipeline,但**imported 优先 / organic 二次精调** (DPO 阶段)。

#### 3.7.2 Organic Trace 的"工作总结"机制

agent 跟用户的真实对话往往是 **多轮、零散、隐含问题**, 不能直接喂给 LoRA。需要中间一层 distillation:

```
原始多轮对话:
  user: 我有个合同想咨询一下
  agent: 好的, 是什么类型?
  user: 上周签的房屋租赁
  agent: 主要担心哪条?
  user: 第 X 条违约金有点高
  agent: ...(详细分析)...
       ↓
  Trace Distiller (LLM-based, 周期性后台跑)
       ↓
精炼成单条 (Q, A) trace:
  Q: "房屋租赁合同第 X 条违约金过高,如何主张减少?"
  A: "依《民法典》第 585 条 [Doc 1], 当事人可请求适当减少..."
  retrieved_docs: [民法典 585 条原文, ...]
  origin: organic
  source_session: <session_id>
  feedback: <用户事后给的👍>
```

**Distiller 的实现**: 后台 cron 每天跑一次, 用云 LLM(老师) 把当天的 chat session 抽炼。**用户可以在 📊 Trace 模块 detail 面板里手动审阅 / 修正 distill 结果**, 标"高质量"的样本会被加权进 RAFT。

#### 3.7.3 外部源导入接口

📊 Trace 模块 detail 面板的 [+ 导入外部数据] 操作:

支持的格式:

| 格式 | Schema | 适用场景 |
|---|---|---|
| **JSONL** | `{"Q": str, "A": str, "retrieved_docs"?: [str], "citations"?: [str], "source"?: str}` | 标准格式, 推荐 |
| **HuggingFace dataset** | 通过 `datasets` 库直接拉, 配字段映射 | DISC-Law-SFT, CAIL, Lawyer-Instruct-CN |
| **CSV** | `Q, A` 两列 (简化) | Excel 导出 |
| **Tudou trace export** | `~/.tudou_claw/expert/<id>/traces/*.jsonl` | 跨实例迁移 |

导入流程:
1. 用户上传文件或选 HF dataset 名
2. UI 显示 schema 校验 + 预览前 5 条
3. 用户确认 → 写入 traces 目录,打 `origin: "import"` + `source: <tag>` 标签
4. Pipeline 视图 📊 Trace 模块的 count 立刻更新 (organic + import 分开显示)
5. 后续训练自动消费两源

UI 上的视觉:

```
📊 Trace Pool: 5287 条
├── 🟦 imported: 5000 条 (DISC-Law-SFT)
└── 🟢 organic: 287 条 (你的日常对话)

[+ 导入外部数据]   [审阅 distilled traces]   [导出当前 trace 集]
```

#### 3.7.4 Trace 清洗规则(v1.1 review #2)

新加进 trace pool 的样本(无论 organic/import)都要过清洗流水线,否则 RAFT 训练数据被污染:

| 规则 | 触发 | 处理 |
|---|---|---|
| **去重** | Q 文本 cosine 相似度 > 0.95 | 保留质量更高的(优先 imported curated > 用户 👍 > 时间最新) |
| **过短过滤** | Q < 5 字符 OR A < 20 字符 | 直接丢 |
| **垃圾对话过滤** | 含明显测试语("test"/"123"/"asdf") OR 句子单字符占比 > 60% | 丢 |
| **低质标记** | 用户 👎 OR distiller 自评分 < 0.5 | 不进训练集,但保留供分析 |
| **每月自动清洗** | cron 任务 | 重跑全部规则,持久化清理后版本 |

实现位置: `app/domain_expert/training/trace_cleaner.py`,SP-2 阶段交付。

#### 3.7.5 段位规则同时考虑两源

**Trace 模块完成判定**: `total_traces >= raft_data_target`,**不区分来源**。

但段位规则可以加 metadata: 例如 🎯 专家段位有附加条件 "organic 占比 ≥ 10%",防止纯 import 永远没个性化。配方文件 `level_rules` 里可定:

```yaml
level_rules:
  expert:
    requirements:
      bundle_complete_pct: ">=80"
      lora_active: true
      eval_score: ">=0.8"
      organic_trace_ratio: ">=0.1"   # 🆕 至少 10% 来自真实使用
```

### 3.8 RAG → LoRA 转化管道(核心机制)

养成系统不是"把 RAG 编译进 weights"——RAG 永远不消失。实际转化:

```
Corpus → RAG Index (上游)
   ↓
RAG 答用户问题 → 留 trace { Q, retrieved_docs, A, feedback }
   ↓
trace 累积到 1k+ → 云 LLM 老师改写为 RAFT 训练样本
   (含真相关 + 干扰文档, 答案带 [Doc N] 引用, 部分样本教"拒绝")
   ↓
QLoRA 训练
   ↓
LoRA adapter 落盘
   ↓
推理时 LoRA + RAG 协作 (LoRA 学的是"怎么用 RAG 用得好")
   ↓
持续 trace 累积 → 月度 DPO refine
```

**进 LoRA 的内容**: 领域语言风格、推理结构、cite 规范、何时拒答、常见模式套路。
**永远在 RAG 的内容**: 具体法条原文、判例编号、新出台内容、所有"必须可溯源"的事实。

### 3.9 Chunker 按 Specialty 自适配(v1.1 review #3 + 自适配修正)

不同领域的文本结构差异巨大,**通用按字数切就把语义切碎了**。Chunker 设计成**插件式策略**:每个 specialty 在 YAML 里声明自己用哪种 chunker + 配置参数。

#### 3.9.1 通用 schema(在 specialty YAML 里声明)

```yaml
# 在任意 specialty 的 yaml 里
chunker:
  strategy: <strategy_id>          # 引用 chunker registry 里的实现
  config:
    <strategy 自定义参数>
```

#### 3.9.2 内置 chunker 策略(registry)

| strategy_id | 适用领域 | 切分单元 | 实现路径 |
|---|---|---|---|
| **`hierarchical_legal`** | 法律 / 法规 / 司法解释 | 编→章→节→条→款→项,以"条"为最小独立 chunk | `app/domain_expert/corpus/chunker_legal.py` |
| **`legal_judgment`** | 判决文书 | 按段落 + 元数据,**判决书原文不切散** (2-4k tokens 一个 chunk) | 同上文件 |
| **`medical_section`** ⏸ | 医疗指南 / 病历 | 按 SOAP / 诊断 / 治疗 / 药理 等结构化 section | `chunker_medical.py` (SP-1 不实装,医疗 specialty 上线时加) |
| **`finance_report`** ⏸ | 财报 / 公司公告 | 按"资产负债表/利润表/现金流量表/附注"分块 | `chunker_finance.py` (财务 specialty 上线时加) |
| **`paragraph`** | 通用兜底 | 按段落 + 标题层级 | `chunker_paragraph.py` |
| **`fixed_window`** | 极简兜底 | 按字符 sliding window | `chunker_window.py` |

#### 3.9.3 法律 specialty(SP-1 实装)

`legal.yaml` 配置:

```yaml
chunker:
  strategy: hierarchical_legal
  config:
    levels: [book, chapter, section, article, paragraph, item]
    primary_unit: article          # 以"条"为最小独立 chunk
    keep_metadata:                 # 每个 chunk 携带的元数据
      - law_name                   # 民法典 / 合同法
      - book                       # 第三编 合同
      - chapter                    # 第N章
      - section
      - article_number             # 585
    min_chunk_chars: 80
    max_chunk_chars: 800
```

切分层级例:

```
法律文本结构:
   编 (Book)          ← 民法典分 7 编: 总则 / 物权 / 合同 / ...
    └ 章 (Chapter)    ← 合同编下"合同的订立"章
      └ 节 (Section)   ← 节下面有具体条
        └ 条 (Article) ← 最小独立语义单元 (585 条) ← chunk 边界
          └ 款 (Para)  ← 一条下面分几款
            └ 项 (Item) ← 一款下面分几项 (一、二、三...)

输出 chunk 例:
   text: "第585条 当事人可以约定一方违约时应当根据违约情况向..."
   metadata:
     law_name: 民法典
     book: 第三编 合同
     chapter: 第八章 违约责任
     section: ""
     article_number: 585
     full_path: "民法典.第三编.第八章.第585条"
```

**判决文书走另一个 chunker**(legal.yaml 里可同时声明多个):

```yaml
chunker_secondary:
  - strategy: legal_judgment
    applies_to:
      file_pattern: "*.judgment.txt"
      source: cail*
    config:
      min_chunk_chars: 800
      max_chunk_chars: 4000
      keep_metadata: [court_level, case_number, case_type, decision_year, parties]
```

#### 3.9.4 Retrieval 阶段的影响

不同 chunker 输出的 metadata 决定 retrieval 排序。法律的 `full_path` 让"同章 > 同编 > 跨编"加权;判决文书的 `case_type` 让相似案由优先。

**通用接口**: chunker registry 输出 `{text, metadata}` 标准格式,retrieval / reranker 不感知 specialty 细节。

#### 3.9.5 元原则:Specialty-Pluggable Components

到目前为止 spec 里**所有"按 specialty 不同"的组件**都遵循同一个模式 —— 插件式策略 + 配置在 YAML 里:

| 组件 | 通用 schema | 法律实装 | 未来扩展 |
|---|---|---|---|
| 评测套件 (§3.1.2 `eval_suite`) | 列名+runner | LegalBench-zh + 引用准确率 | CMB(医疗) / FinBench(财务) |
| Chunker (§3.9 `chunker`) | strategy_id + config | hierarchical_legal | medical_section / finance_report |
| 配方 packs / skills 清单 (§3.1.2) | 列 ID | 12 个法律 packs + 3 skills | 各领域自己列 |
| 安全闸门 (§11) | disclaimer + redact rules | 律师法 + PIPL | 医疗法 + HIPAA(海外) |

**好处**: 加新 specialty 只是写一个 YAML + 实装少量自定义 chunker / runner,**核心代码零改动**。SP-1 完成后,加"小医"理论上一周内出第一版。

### 3.10 Mac 资源分级策略(v1.1 review #4)

单机部署在 Mac M3/M4 上,LoRA 训练 + 推理 + 主 Tudou 服务 + voice mode 同时跑,需要**显式资源调度**避免 OOM 或主服务卡顿:

| 模式 | 触发 | 资源占用 | 行为 |
|---|---|---|---|
| **省电模式** | 用户手动 / 笔记本电池电量 < 30% | embed/rerank lazy 加载,LoRA 卸载 | 推理走云端;trace 仍累积;不能训练 |
| **平衡模式** (默认) | 充电中 / 桌面工作 | LoRA inference 常驻 (~6GB),embed/rerank 按需 | 推理本地化,训练受阻塞需手动触发 |
| **训练模式** | 用户手动触发训练 + 确认 | 主服务 throttle 到最低,主聊天延迟 +1s | LoRA 训练 8-12h,期间 voice mode 不推荐 |
| **性能模式** | 用户手动 (临时) | 全开,所有模型常驻 | 推理 / 训练 / 索引并发,内存峰值 ~30GB |

**实现**:
- 设置 tab 加 [资源模式] 切换器(SP-0 不做,SP-3 落地)
- 模型按需加载/卸载: `app/domain_expert/inference/resource_manager.py`
- 训练时主服务进入"shadow mode": LLM 调用全走云,本地 LoRA 让出 GPU/MPS

### 3.11 多轮对话 RAG 优化(v1.1 review #5)

现有 RAG 默认用最后一句 query 检索,多轮对话场景会丢上下文。例:

```
user: 帮我看个合同
agent: 好,什么类型?
user: 房屋租赁
agent: 主要担心哪条?
user: 第 X 条违约金              ← 单凭这句去检索 = 检不到任何法条
```

**v1.1 加: Query Rewrite 层**

```
推理流程多轮对话时:
   ↓
[最近 3-5 轮 history] + [当前 query]
   ↓
LLM (本地 LoRA / 云) 重写为完整意图
   "房屋租赁合同中违约金条款过高 (合同法/民法典)"
   ↓
重写后 query 喂给 retrieval
   ↓
检索结果 + 历史引用上下文一起进下游 prompt
```

**实现**: `app/domain_expert/inference/query_rewriter.py`。SP-1 用云 LLM 实现一版,SP-2 后让本地 LoRA 接管(降低 latency)。

### 3.12 LoRA 版本管理 + Shadow 部署(v1.1 review #6)

每次训练**绝不直接覆盖**当前激活的 LoRA。版本管理流程:

```
用户触发训练
   ↓
LoRA v(N+1) 训出 → 落 lora/v(N+1)/
   ↓
Shadow run 阶段 (1 周, 不切换 active 链接)
   ├── 真实流量同时进 v(N) 和 v(N+1)
   ├── 用户看到的回复来自 v(N) (旧版,稳)
   ├── v(N+1) 的输出留 trace 但不 ship
   └── 自动对比: response time / cite 准确率 / blind eval 评分
   ↓
Eval 通过门槛?
   ├── ✓ 自动 promote: lora/current → v(N+1)/
   └── ✗ 保留 v(N+1)/ 但 active 不切换 + 通知用户
   ↓
任何时刻用户可以一键回滚: 切 lora/current → v(K)/ (任意旧版本)
```

**版本保留策略**: 保留所有 LoRA 版本,直到磁盘紧张才提示清理(每个 ~50MB,够留几十版)。

**Eval 门槛**(自动 promote 条件):
- LegalBench-zh 分数 ≥ 旧版 - 1% (允许微回退,因为引入新风格)
- Citation 准确率 == 100%
- 用户 👍 比例 ≥ 旧版 - 5%

**实现**: `app/domain_expert/training/version_manager.py`。SP-2 阶段交付。

### 3.13 精确本地处理率(v1.1 review #7)

**朴素本地率**: `local_handled_count / total_count` —— 易虚高,因为模型遇到无解问题、敏感问题、跨域问题都会"假装答了"也算本地。

**精确本地率(净本地率)**:

```python
local_handle_rate_clean = local_handled_legitimate / eligible_count

eligible_count = total_count - excluded_count

excluded_count =
    sensitive_pipl_redacted_count       # PIPL 触发自动转云
  + cross_domain_count                  # 跨领域(LoRA confidence 极低 + retrieval 全打分 < 0.3)
  + unanswerable_count                  # agent 主动说"我无法回答"或"建议咨询律师"
  + safety_violation_count              # 涉及刑事/宪法这种风险高领域
```

**记录方式**: 每条推理的 trace 多个 boolean 字段:
- `is_pipl_sensitive: bool`
- `is_cross_domain: bool`
- `is_unanswerable: bool`
- `is_safety_excluded: bool`

后台统计任务每天计算 `local_handle_rate_clean`,这个才是 master 段位的判定指标。

**实现**: `app/domain_expert/inference/routing.py` 落 trace 的同时打这些 flag。SP-3 阶段交付。

---

## 4. UI Architecture(SP-0 整合)

### 4.1 Unified Agent Workspace

每个 agent 不再是"chat 主页 + 一堆浮窗",而是**完整工作区,5 tab 切换**。

```
┌─ 左侧 nav rail (现有,不变) ─┐
│ 🤖 Agents                  │
│ 🛠 Skills (全局浏览)        │
│ 🔌 MCP                     │
│ ⚙️ Settings                │
└────────────────────────────┘

主区域 (点击 agent 后):
┌──────────────────────────────────────────────────────────────────┐
│ ◀ Back   ⬢ 小土 · 通用聊天助手                              [⋯]   │
│ ──────────────────────────────────────────────────────────────── │
│ [💬 对话] [🧰 能力] [🎓 养成] [📊 历史] [⚙️ 配置]                  │
│ ──────────────────────────────────────────────────────────────── │
│                                                                   │
│ [当前 tab 内容,占满主区]                                            │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 5 Tab 内容定义

#### 💬 对话
- 现有 chat 界面(滚动消息流, thinking process, execution steps)
- 顶部按钮: [Voice Mode] (启动现有全屏 voice overlay)
- 滚动状态独立缓存,切回保留位置

#### 🧰 能力
统一 4 类能力 + 内嵌的添加流程:

```
─ 能力概览 (4 stat tiles) ─

▼ MCP SERVICES (3 connected)        [+ 添加 MCP ▼]
▼ GRANTED SKILLS (5 ready)          [+ 从 Store 授权 ▼]
▼ PROMPT PACKS (12 bound)           [+ 添加 ▼]
▼ EXPERT SPECIALTY (read-only card) [→ 跳转养成 tab]
```

**关键**: 点 `[+ 添加 ▼]` 不弹独立 modal,而是在当前 tab 内向下展开 inline picker(catalog 浏览器、Skill Store 浏览器等)。原 90vw 的 Prompt Pack 市场 popup 直接消失,改为 tab 内的展开区。

#### 🎓 养成 (SP-1 主入口)
- 普通 agent: 显示 specialty 模板选择器(法律 / 医疗 / 财务 等卡片)
- 专家 agent: 显示
  - 当前段位卡 (🌱→🏆 进度条)
  - 配方应用进度 (Stage 0-7)
  - 配方清单详情 (可折叠展开,显示每个绑定项)
  - 训练历史 + Eval 报告 (SP-2/3 后)
  - [禁用专家化] / [恢复专家化] / [完全卸载] 操作

#### 📊 历史
- 对话历史 list
- Tool 调用 timeline
- Plan 执行轨迹
- Voice mode session 记录

#### ⚙️ 配置 (取代旧 Edit Agent modal)
- 身份 (name / avatar / role / persona)
- 模型 (LLM provider / 参数)
- TTS / STT 配置
- 高级 (memory / RAG / ...)

### 4.3 「能力」tab 与「养成」tab 的边界

| 维度 | 能力 tab | 养成 tab |
|---|---|---|
| **作用范围** | 任意 agent | 仅专家 agent 有内容,普通 agent 看入口 |
| **MCP** | ✓ 单独管理 | ✗ |
| **Skills** | ✓ 单独 grant/revoke | (养成时批量授权,但管理在能力 tab) |
| **Prompt Packs** | ✓ 单独 bind/unbind | (养成时批量绑定,但管理在能力 tab) |
| **Corpus / RAG / 向量库** | ✗ 完全不存在 | ✓ 唯一管理处 |
| **LoRA / 训练** | ✗ | ✓ (SP-2) |
| **Routing** | ✗ | ✓ (SP-3) |
| **段位** | 状态卡只读显示 | ✓ 完整管理 |

### 4.4 现有 popup → 整合后的归宿

| 现状 | 整合后 |
|---|---|
| 能力扩展 popup | → 能力 tab |
| Prompt Pack 市场 popup | → 能力 tab 内嵌区 |
| 已发现 popup | → 能力 tab 现有列表 |
| Edit Agent popup | → 配置 tab |
| Skill Store(全局) | ✓ 保留(nav rail,跟 agent 无关) |
| Voice mode 全屏 | ✓ 保留(对话 tab 顶部按钮启动) |
| 专家化 sub-modal(规划) | ❌ 不再创造,直接进养成 tab |

---

## 5. API 设计

### 5.1 现有 namespace,加 expert 子路径(不另开顶级)

```
现有(不变):
GET    /api/portal/agent/{id}/profile
GET    /api/portal/agent/{id}/skill-pkgs
GET    /api/portal/agent/{id}/prompt-packs

新增:
GET    /api/portal/specialty-templates                    列出所有可用配方
GET    /api/portal/specialty-templates/{id}               配方详情

GET    /api/portal/agent/{id}/expert                      专家化状态
POST   /api/portal/agent/{id}/expert/initialize           启动专家化(指定 template_id)
DELETE /api/portal/agent/{id}/expert                      卸载(可选保留数据)

POST   /api/portal/agent/{id}/expert/corpus/ingest        触发语料下载 + 索引
GET    /api/portal/agent/{id}/expert/corpus               列语料状态
POST   /api/portal/agent/{id}/expert/corpus/reindex       重建向量索引

POST   /api/portal/agent/{id}/expert/query                专家推理入口(替代默认 LLM 路径)
POST   /api/portal/agent/{id}/expert/feedback             用户 👍/👎 反馈

POST   /api/portal/agent/{id}/expert/training/start       触发 LoRA 训练 (SP-2)
GET    /api/portal/agent/{id}/expert/training/status      训练状态
POST   /api/portal/agent/{id}/expert/training/cancel      取消正在训练的 job

POST   /api/portal/agent/{id}/expert/eval/run             跑 holdout eval
GET    /api/portal/agent/{id}/expert/eval/latest          最新 eval 报告

GET    /api/portal/agent/{id}/expert/traces               历史
GET    /api/portal/agent/{id}/expert/stats                指标看板数据
```

### 5.2 Reply pipeline 接入点

agent.py reply pipeline 增加单一分支(其余流程不变):

```python
async def agent_reply(agent: Agent, query: str, ...):
    if agent.expert_specialty:
        try:
            from app.domain_expert.inference.pipeline import answer
            return await answer(agent, query, ...)
        except Exception as e:
            logger.warning("expert pipeline failed, falling back: %s", e)
            # fall through to default path
    return await _default_llm_reply(agent, query, ...)
```

**降级语义**: 任何 expert pipeline 异常 → catch + log + 走原路径,**绝不让 expert 系统的 bug 影响 agent 基础对话**。

---

## 6. Sub-project 分解 + 时间线

| Sub-project | 内容 | 估算 | Gate(进入下一阶段的条件) |
|---|---|---|---|
| **SP-0** UI 整合 | Workspace shell, tab 导航, 现有 popup 内化 | 3-5 天 | 所有现有功能在新 workspace 跑通,无 regression |
| **SP-1** Foundation + RAG | 模块骨架, SpecialtyTemplate loader, bundle apply, 法律 corpus, RAG pipeline, 养成 tab UI | ~2 周 | 用户能选法律模板,装好 packs/skills,corpus 索引完,问问题得到带引用的答(云 LLM 答, RAG 给 context) |
| **SP-2** LoRA pipeline | Trace 增强采集, RAFT 合成数据生成, QLoRA 训练驱动, eval 框架 | ~3 周 | LoRA v1 训出, holdout eval 比 baseline +10%, 可一键回滚 |
| **SP-3** Routing + Production loop | Local LLM 推理(mlx-lm), 置信度 routing, DPO 月度 refine, 反馈 UI | ~1-2 周 | 本地处理率 ≥ 60%, 平均延迟 ≤ 2s, 1 周稳定运行 |

**关键**: 每个 SP 独立可交付。SP-1 跑稳一个月再决定是否继续 SP-2;SP-2 训出来效果不达标可以只用 SP-1 的 RAG;SP-3 是锦上添花。

---

## 7. 能力演进时间线

```
Day 0 (SP-1 done)        Month 1 (SP-2 v1)       Month 3 (SP-3 mature)    Month 6+
─────────────────        ──────────────────       ──────────────────       ─────────
RAG + 云 LLM             + LoRA v1 落地           + Routing + DPO          + 持续刷新
强制引用                  + Trace 累积 1k+         + 本地处理率 60%+         + 用户反馈
                         + Eval 体系              + 二审机制                  + 知识更新
段位:🌱 见习              段位:🌿 熟手             段位:🎯 专家               段位:🏆 大师
```

| 里程碑 | 用户体感 |
|---|---|
| Day 0 | 「Tudou 多了个法律咨询能力,每个回答都带可点开的法条引用」。已超越直接问 ChatGPT |
| Month 1 | 表面上没大变化,但 trace 累计 1k+, 第一版 LoRA 训完, eval 显示常见合同条款类问题 75% → 88% |
| Month 3 | 体感明显:常见问题响应 5s → 2s; 80% 查询不出网; 风格越来越像「跟你混熟了的小法」 |
| Month 6+ | 真正专家形态:常用领域比 ChatGPT 准、带引用、私密、便宜。冷门领域走云 LLM 兜底 |

---

## 8. 量化效果矩阵

| 指标 | 现状(直接问云 LLM) | Day 0 (SP-1) | Month 3 | Month 6+ |
|---|---|---|---|---|
| **平均响应延迟** | 3-8 s | 4-7 s | 2-3 s | 1.5-2.5 s |
| **回答带可信引用** | ✗ | ✓ | ✓ | ✓ |
| **法条引用准确率** | 编造率 ~25% | <3%(RAG 保证) | <1% | <1% |
| **常见问题准确率** | ~75% | ~80% | ~88% | ~92% |
| **隐私(query 上云)** | 100% | 100% | ~30% | ~15% |
| **每月 LLM 成本** | $50-200 | $50-200 | $20-60 | $10-30 |
| **个性化** | 0 | 0 | 中 | 高 |
| **新法吸收速度** | 即时 | 1-7 天 | 1-7 天 | 1-7 天 |

---

## 9. 能力边界(明示**不会**做的)

| ❌ 不做 | 原因 |
|---|---|
| 替代律师出庭/出具正式法律意见书 | 未持执业证, UI 强制底栏免责 |
| 跨域/跨法系问题(美国法、国际仲裁) | corpus 中国法系, 跨域走云 LLM 不强训 |
| 自动跟进新出台法律 | 无 daemon 监控立法网站, 月度手动 refresh |
| 复杂多方博弈(M&A、反垄断博弈) | 单 agent 不能解决, 主动转云 LLM |
| 一个 agent 同时多 specialty | 一个 agent 只能 specialize 一次, 多领域专家请建多个 agent |
| 跨 agent 共享 corpus | SP-1 阶段每 agent 自己一份;未来 v2 考虑 corpus pool |
| Self-play 突破人类专家 | 法律无干净 reward 信号,删除该路径 |
| 替代 PPO 用复杂 RL | 用 DPO from feedback,简单 10 倍且稳 |
| 在第 1 周显著好用 | 冷启动需 1k+ trace, 头一个月就是「Tudou + RAG + 强制引用」 |

---

## 10. 单次查询数据流(端到端示例)

```
用户在小土 chat tab 输入 "帮我看看这条违约金条款是否过高"

Step 1  Agent reply pipeline 检测 expert_specialty="legal"
        → 走 expert.pipeline.answer()                          0 ms
        ↓
Step 2  Safety gate                                            1 ms
        - 扫描 PIPL 敏感词(姓名/身份证/手机) → 自动脱敏
        - 注入免责声明上下文
        ↓
Step 3  Retrieval                                              80-150 ms
        - bge-m3 编码 query → 向量
        - sqlite-vss 检索 top-30
        - bge-reranker-v2-m3 精排 → top-8
        - 返回: [民法典 585 条, 合同法解释二 29 条, ...]
        ↓
Step 4  Routing(SP-3+ 启用)                                    5 ms
        - 看 query 类型 + retrieval 置信度
        - 决定: 本地 LoRA / 云 LLM / 二审
        - SP-1 阶段:全部走云 LLM
        ↓
Step 5  Generate                                               1500-7000 ms
        - prompt = 免责 + [retrieved 片段] + query
        - 模型经 RAFT 训练 (SP-2 后), 知道引用 [Doc N]
        - 输出: "依《民法典》第 585 条 [Doc 1], 违约金过分高于
                造成损失的, 当事人可请求适当减少..."
        ↓
Step 6  Cite 校验                                               30 ms
        - 抓回复中所有 [Doc N] → 反查 retrieval 集
        - 不存在的引用 → 标红/重生成
        ↓
Step 7  返回 chat tab                                          10 ms
        - 引用片段 hover 显示原文 / 点击开新窗
        - 底部 👍/👎 按钮
        ↓
Step 8  Trace 落盘 (异步)
        - { Q, retrieved_top8, model_choice, A, latency, [feedback] }
        - 用户后续 👍/👎 → 追加进 trace

总延迟:
  SP-1 (云 LLM 路径): 4 - 7 秒
  SP-3+ (本地 LoRA 路径): 1.6 - 2.7 秒
```

---

## 11. 合规与安全

### 11.1 PIPL(个人信息保护法)

- 用户上传含他人隐私的合同/案件材料,**入 RAG 前自动检测 + 脱敏**
- 检测目标: 姓名、身份证号、手机号、银行卡号、地址
- 实现: 正则 + small NER 模型(SP-1 用正则即可,SP-2 替换为更稳的 NER)
- 脱敏后保留原文供用户校对,但 RAG 索引和 trace 只存脱敏版

### 11.2 律师法 / 业务范围

- UI 顶部 + 每个回复底栏强制注入: "AI 提供的法律分析仅供参考,非正式法律意见"
- 模型经 RAFT 训练拒绝输出"我作为律师建议..."句式
- 只输出"以下分析供参考"或"按民法典条款看, 一般情况下..."

### 11.3 数据出境

- 用户 corpus 默认不出云
- 云 LLM 调用时只发送 (query + retrieval top-k 法条片段),不发送整个用户语料
- 用户可在配置 tab 关闭"允许走云 LLM 兜底",改纯本地(SP-3 后可用)

---

## 12. 几个明确的设计决策(及理由)

| 决策点 | 选择 | 否决项 | 理由 |
|---|---|---|---|
| Specialty 是 agent 属性还是独立实体? | **agent 属性 + 独立持久化数据** | 独立 ExpertProfile 实体 | 用户跟 agent 对话身份不变, 但数据可备份迁移 |
| RAG 是横向能力还是养成内一层? | **养成内一层** | 横向能力(任 agent 可加 RAG) | 普通 agent 不需要 RAG, 加了反而增加复杂度;轻量知识用 prompt pack |
| Bundle 用现有 skill / pack 体系还是新建? | **现有体系** | 全新 expert-only skill 系统 | 复用最大化, 无需平行系统;skill / pack 已经是 first-class |
| RL 用 PPO 还是 DPO? | **DPO from user feedback** | PPO from scratch | 法律无干净 reward, PPO 容易崩;DPO 直接吃偏好对 |
| 是否做 self-play? | **不做** | AlphaGo 风格 | 法律开放场景无 ground truth, self-play 会偏 |
| 一个 agent 多 specialty? | **一次只一个** | 同时 legal+medical | 概念清晰、训练数据互不污染;多领域请多 agent |
| Specialty 模板版本管理? | **手动确认更新** | 自动 silent update | 用户可控, 避免 LoRA 因配方变化莫名重训 |
| 删除专家化时数据保留? | **保留 + [恢复] 按钮** | 直接清空 | LoRA 训练成本高, 用户可能反悔;明确 [完全卸载] 才清 |
| Capabilities tab 是否显示 specialty? | **只读状态卡, 跳转养成 tab 编辑** | 完全不显示 / 在能力 tab 编辑 | 提供整体视图但不重复编辑入口 |

---

## 13. 风险登记

| 风险 | 概率 | 影响 | 兜底 |
|---|---|---|---|
| RAFT 数据合成质量差 | 中 | LoRA 训歪 | Eval 门槛 + holdout test, 跌过阈值不发布,保留所有版本 1-click rollback |
| mlx-lm 在某些 Mac 配置不稳 | 低 | 训练失败 | 备选 transformers + peft + bitsandbytes 路径 |
| Mac 内存不够同时跑 LoRA + 主服务 | 中 | 训练影响日常使用 | 训练只在用户主动触发, 不 24h 后台 |
| 法律语料合规风险(裁判文书网 ToS) | 中 | 数据来源不能用 | 仅用 flk.npc.gov.cn(公开)+ HF 开源数据集(MIT/Apache) |
| 用户反馈数据稀疏 | 高 | DPO 无足够偏好对 | UI 强制反馈引导 + 隐式信号(对话延续度等) |
| LoRA 推理质量低于云 LLM 太多 | 中 | 用户失望 | 头 3 个月主路径仍是云 LLM, LoRA 是 shadow + 渐进切换 |

---

## 14. Out of scope(明确不在本设计范围)

- 多用户协作 / 权限模型(Tudou 单租户定位)
- 多机器分布式训练
- 商用化(SLA、计费、审计追踪)
- 跨 agent 路由("该问小法还是小医?")—— v2 议题
- Specialty marketplace UI(社区分享配方)—— v2
- 模板编辑器 UI(SP-1 阶段配方手写 YAML 即可)
- 中文之外的语言支持
- Specialty 之间的能力组合(legal + finance hybrid)

---

## 15. 验收标准(SP-1 完成的判定)

最小验收集:

- [ ] Agent dataclass 加 5 个 expert_* 字段, 持久化通过, 旧 agents.json 加载不破坏
- [ ] `app/domain_expert/` 模块独立, 主项目 `requirements.txt` 不变(新依赖 optional)
- [ ] `app/data/specialty_templates/legal.yaml` 配方文件 + Schema 校验
- [ ] SpecialtyTemplate loader + bundle apply 引擎(批量 grant skills + bind packs)
- [ ] 法律语料采集脚本(flk.npc.gov.cn 爬虫 + HuggingFace 数据集导入器)
- [ ] 法律特化 chunker(按 第X条/款/项 切)
- [ ] bge-m3 embedder + sqlite-vss 向量库
- [ ] `/api/portal/agent/{id}/expert/*` 全套 REST 端点
- [ ] Reply pipeline 加 expert 分支, 降级测试通过
- [ ] UI workspace shell 落地, 5 tab 切换正常
- [ ] 能力 tab 内嵌 packs / skills / mcp 添加流程, 旧 modal 全部废止
- [ ] 养成 tab 落地, specialty 选择器 + 配方应用进度 + 段位卡
- [ ] 法律 specialty 端到端跑通: 选模板 → 装 packs/skills → 索引 corpus → 问问题得到带引用的回复
- [ ] LegalBench-zh baseline 跑分入库(后续比较基准)
- [ ] feature flag `tudou_workspace_v2` 可关回旧 UI(过渡期)
- [ ] PIPL 脱敏 + 免责声明强制注入, 含手机号的输入测试用例验证
- [ ] 删 agent 时 expert 目录处理符合预期(默认保留 orphan, 可选清除)
- [ ] 关 expert_specialty 时 RAG 数据保留, [恢复] 按钮可一键回到原状
- [ ] 现有功能 100% 无 regression(voice mode + chat + 现有 skill / mcp 全部正常)

---

## 16. 后续 SP 简述

### SP-2 关键交付
- Trace 采集格式标准化
- Cloud LLM 合成 RAFT 训练集
- mlx-lm QLoRA 训练驱动
- Holdout eval 框架(LegalBench-zh + 自建样本)
- 训练 UI: 进度条 + loss 曲线 + 一键回滚

### SP-3 关键交付
- mlx-lm local inference 服务
- Routing policy(置信度阈值, query 类型分类)
- DPO from user feedback 月度增量训练
- 反馈 UI 闭环
- 监控/eval dashboard

---

## 17. Review Round 1 Changelog (v1.0 → v1.1)

外部评审(2026-05-10, 92/100)给了 7 条优化建议,全部纳入。下表是补丁位置:

| # | 评审建议 | 落到 spec 哪里 | 状态 |
|---|---|---|---|
| 1 | LegalBench-zh / 引用准确率作为段位**硬门槛** | §3.1.2 `eval_suite` 通用 schema + legal.yaml `level_rules.benchmarks` 专属阈值;明确**每个 specialty 自定义自己的硬评测**(legal=LegalBench-zh, medical=CMB, finance=FinBench, ...) | ✓ |
| 2 | Trace 清洗规则(去重/过滤/低质标记) | §3.7.4 新增 5 条规则 + `trace_cleaner.py` 实现位置 | ✓ |
| 3 | 文本切分器**按 specialty 自适配** | §3.9 重写为插件式 chunker registry + YAML 声明策略;法律实装 hierarchical_legal + legal_judgment;泛化为 §3.9.5 元原则 "Specialty-Pluggable Components"(eval / chunker / safety / bundle 都遵循同模式) | ✓ |
| 4 | Mac 内存分级策略(省电/平衡/训练/性能) | §3.10 新增, 4 模式定义 + `resource_manager.py` | ✓ |
| 5 | 多轮对话 Query Rewrite | §3.11 新增, `query_rewriter.py` | ✓ |
| 6 | LoRA 版本管理 + Shadow 部署 + 一键回滚 | §3.12 新增, 完整 promote/rollback 流程 | ✓ |
| 7 | 精确本地处理率(排除敏感/跨域/无解) | §3.13 新增, `local_handle_rate_clean` 公式 + master 段位规则更新 | ✓ |

**未改的部分** (评审标"务必保持"的 8 条精华):

✓ 一个 Agent 只专精一个领域 · ✓ RAG 永远在线, LoRA 不学事实 · ✓ DPO 不用 PPO ·
✓ 不做 self-play · ✓ 独立模块 / 强降级 / 数据隔离 · ✓ UI 统一工作台消灭弹窗 ·
✓ 法律语料只用公开合规来源 · ✓ 强制引用校验 · ✓ SP 分阶段迭代

**落到具体 sub-project 的工作量影响:**

| SP | v1.0 估算 | v1.1 增加 | v1.1 估算 | 增加内容 |
|---|---|---|---|---|
| SP-0 | 3-5 天 | 0 | 3-5 天 | (UI 整合不涉及评审项) |
| SP-1 | ~2 周 | +2 天 | ~2.5 周 | 法律层级 chunker (#3) + 多轮 query rewrite v0 (#5) |
| SP-2 | ~3 周 | +4 天 | ~3.5 周 | Trace cleaner (#2) + Eval suite framework (#1) + Version manager + shadow deploy (#6) |
| SP-3 | ~1-2 周 | +3 天 | ~2 周 | Resource manager (#4) + Routing flag fields (#7) |

整体多了 ~1.5 周, 但**质量/可信度边界清晰得多**, 这一周值得花。

---

**End of design.**
