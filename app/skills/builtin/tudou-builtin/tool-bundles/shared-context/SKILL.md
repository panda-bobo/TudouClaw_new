---
name: shared-context
description: Project 级共享上下文数据库 - artifacts/decisions/milestones/handoffs/Q&A. Grants tools sc_query, sc_register_artifact, sc_get_artifact, sc_record_decision, sc_handoff
icon: "🗄️"
metadata:
  tier: core-bundle
  tools:
    - sc_query
    - sc_register_artifact
    - sc_get_artifact
    - sc_record_decision
    - sc_handoff
---

# 🗄️ 共享上下文 / shared context

Project 级的多 agent 协同数据库 - 用查询代替消息推送，token 成本从 O(content) 降到 O(reference)。

## 🔧 包含的工具 (5 个)

| 工具 | 用途 |
|---|---|
| `sc_query` | 查 5 张表 (artifacts/decisions/milestones/handoffs/pending_qs) 或 summary 概览 |
| `sc_register_artifact` | 把 workspace 里的产出物登记成可被引用的 art_* id |
| `sc_get_artifact` | 拿到 art_* id 后查它指向哪个文件 + 摘要 |
| `sc_record_decision` | 记录团队级别决策（其他 agent 都看得到）|
| `sc_handoff` | 给另一个 agent 发任务（pull 模型，写表不推消息）|

## 📌 核心原则

**不要把内容粘到对话里给别的 agent**。流程应该是：
1. 你产出了文件 → `sc_register_artifact(path, summary)` → 拿到 `art_*` id
2. 给下游 agent → `sc_handoff(dst_agent, intent, artifact_refs=[art_*])`
3. 下游 agent 任务启动 → `sc_query(table='handoffs', dst_agent='self', status='pending')` → 看到任务 + 引用
4. 下游需要细节 → `sc_get_artifact(id)` 看摘要，决定是否 read_file 全文

**重要决策务必记到 decisions 表**，否则别的 agent 会重复纠结同样的问题。

## ⚙️ 默认授权

`shared-context` 默认授权给所有新建 agent，跟 memory-ops / file-ops 等并列。

## 🏷️ Bundle 类型

core-bundle —— 多 agent 协同的基础设施。
