"""Migrators that lift existing scattered policy into engine rules.

Each migrator function:
  1. Reads its source-of-truth (catalog dict / DEFAULT_TOOL_RISK / etc.)
  2. Generates Rule objects with source="migrator:<name>"
  3. Removes prior rules with the same source (idempotent re-run safe)
  4. Adds the new rules to the store

Migrators should be called at hub boot (after the engine is
initialized but before any agent runs). They're also safe to re-run
on demand (UI button "re-import workflow rules").

Currently implemented:
  - migrate_workflow_catalog — turns catalog step contracts
    (output_files / must_contain / min_lines) into per-step
    before_task_done rules. Replaces the hardcoded check at
    project.py:_check_active_task_deliverables (which stays as a
    fallback during transition; once the engine rules are validated
    in production the project.py check can be deleted).
"""
from __future__ import annotations

import logging
from typing import Any

from .types import Rule, RuleScope

logger = logging.getLogger("tudou.rule_engine.migrators")


def _purge_source(store, source: str) -> int:
    """Delete every rule whose source matches. Returns count removed."""
    removed = 0
    for r in list(store.all()):
        if r.source == source:
            if store.delete(r.id, by="migrator"):
                removed += 1
    return removed


def migrate_workflow_catalog(engine: Any, catalog: list[dict] | None = None) -> dict:
    """Turn each step's deliverable contract in the WORKFLOW_CATALOG
    into a global rule fired on before_task_done.

    Generated rule shape (one per non-empty contract):
      name:    "WF:<workflow_id>:<step_id> output_files present"
      scope:   global (applies to any project that binds this workflow)
      trigger: before_task_done
      condition: task title starts with [WF Step N], output_files
                 declares the contracted file, file is verified
      actions: deny if not all output_files verified

    Returns a summary dict {removed, added} for the API/UI to surface.
    """
    if catalog is None:
        try:
            from ..data.workflow_catalog import WORKFLOW_CATALOG
            catalog = WORKFLOW_CATALOG
        except Exception as e:
            logger.warning("workflow_catalog import failed; skipping migration: %s", e)
            return {"removed": 0, "added": 0, "error": str(e)}

    source = "migrator:workflow_catalog"
    removed = _purge_source(engine.store, source)
    added = 0

    for wf in catalog:
        wf_id = wf.get("id") or ""
        wf_name = wf.get("name") or ""
        for step in (wf.get("steps") or []):
            output_files = step.get("output_files") or []
            if not output_files:
                continue
            step_id = step.get("id") or ""
            step_name = step.get("name") or ""
            min_lines = int(step.get("min_lines") or 0)

            # Match any task title that contains the step name (this is
            # how project.py creates tasks: "[WF Step N] <step_name>").
            # When the title doesn't contain step_name (rare), the rule
            # silently won't match — that's fine, falls back to the
            # generic "no output_files" gate.
            condition = {
                "all": [
                    {"field": "task.title", "contains": step_name},
                    {
                        "any": [
                            # No output_files at all → deny
                            {"field": "task.output_files",
                             "length_lt": len(output_files)},
                            # Below required min_lines → deny
                            {"all": [
                                {"field": "task.min_lines", "gt": 0},
                                {"field": "task.min_lines", "lt": min_lines},
                            ]} if min_lines > 0 else {"field": "_dummy",
                                                       "exists": False},
                        ]
                    },
                ]
            }
            rule = Rule(
                name=f"WF:{wf_id}:{step_id} contract",
                description=(
                    f"[migrated] {wf_name} step '{step_name}' must produce "
                    f"{', '.join(output_files)} ({min_lines}+ lines each)"
                ),
                scope=RuleScope("global"),
                trigger="before_task_done",
                condition=condition,
                actions=[{
                    "type": "deny",
                    "message": (
                        f"step '{step_name}' must produce "
                        f"{output_files} (catalog contract)"
                    ),
                }],
                priority=20,   # higher than user-authored rules by default
                source=source,
                created_by="migrator",
            )
            engine.store.add(rule, by="migrator")
            added += 1

    logger.info("workflow_catalog migration: removed=%d added=%d",
                removed, added)
    return {"removed": removed, "added": added}


def migrate_global_denylist(engine: Any) -> dict:
    """Lift auth.ToolPolicy.global_denylist (admin-managed deny set
    of tool names) into engine deny rules. One rule per name.

    Old check at agent.py:6736 still runs in parallel during transition;
    once these rules are validated in production it can be removed.
    """
    source = "migrator:global_denylist"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import get_auth
        auth = get_auth()
        names = sorted(getattr(auth.tool_policy, "global_denylist", set()) or [])
    except Exception as e:
        logger.warning("global_denylist source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for name in names:
        rule = Rule(
            name=f"global_denylist:{name}",
            description=f"[migrated] tool '{name}' on the admin global denylist",
            scope=RuleScope("global"),
            trigger="before_tool_call",
            condition={"field": "tool_name", "eq": name},
            actions=[{
                "type": "deny",
                "message": f"tool '{name}' is on the global denylist",
            }],
            priority=100,           # very high — admin denylist outranks others
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("global_denylist migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def migrate_tool_risk(engine: Any) -> dict:
    """Lift auth.DEFAULT_TOOL_RISK (tool → risk level) into engine
    rules. High-risk tools become require_approval rules; "red" tools
    become deny rules.

    Tools at "moderate" / "low" risk don't need rules — they auto-
    approve in the existing flow. Migrating only the actionable tiers
    (high + red) keeps the rule set focused.
    """
    source = "migrator:tool_risk"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import DEFAULT_TOOL_RISK
        risks = dict(DEFAULT_TOOL_RISK or {})
    except Exception as e:
        logger.warning("tool_risk source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for tool_name, risk in sorted(risks.items()):
        if risk == "red":
            actions = [{"type": "deny",
                        "message": f"tool '{tool_name}' is permanently blocked (red tier)"}]
        elif risk == "high":
            actions = [{"type": "require_approval",
                        "message": f"tool '{tool_name}' requires admin approval (high tier)"}]
        else:
            continue   # moderate / low handled by existing auto-approve path
        rule = Rule(
            name=f"tool_risk:{tool_name}",
            description=f"[migrated] {tool_name} tier={risk}",
            scope=RuleScope("global"),
            trigger="before_tool_call",
            condition={"field": "tool_name", "eq": tool_name},
            actions=actions,
            priority=80,
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("tool_risk migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def migrate_command_patterns(engine: Any) -> dict:
    """Lift auth.ToolPolicy.command_patterns (per-bash-content regex deny
    rules) into engine deny rules.

    Each pattern entry has shape:
      {"pattern": "<regex>", "scope": "global"|"role:X"|"agent:Y",
       "verdict": "deny"|"needs_approval", "reason": "...", "label": "..."}

    Maps scope dialect → engine RuleScope (only "global" + "role:X" are
    in active use; "agent:Y" maps to solo with the agent_id).
    """
    source = "migrator:command_patterns"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import get_auth
        auth = get_auth()
        patterns = list(getattr(auth.tool_policy, "command_patterns", []) or [])
    except Exception as e:
        logger.warning("command_patterns source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("pattern") or "").strip()
        if not pattern:
            continue
        verdict = str(entry.get("verdict") or "deny").lower()
        reason = str(entry.get("reason") or "")
        label = str(entry.get("label") or "")
        scope_raw = str(entry.get("scope") or "global")
        if scope_raw == "global":
            scope = RuleScope("global")
        elif scope_raw.startswith("role:"):
            # No engine scope kind for "role" yet — fall back to global,
            # add condition gating on agent.role.
            scope = RuleScope("global")
        elif scope_raw.startswith("agent:"):
            scope = RuleScope("solo", [scope_raw[6:]])
        else:
            scope = RuleScope("global")

        # Build condition: bash command argument matches the pattern.
        # Also gate on agent.role when scope is "role:X" (since engine
        # scope is global, role gating needs the condition).
        conds: list = [
            {"field": "tool_name", "eq": "bash"},
            {"field": "args.command", "matches": pattern},
        ]
        if scope_raw.startswith("role:"):
            conds.append({"field": "agent.role", "eq": scope_raw[5:]})
        condition = {"all": conds}

        if verdict == "deny":
            actions = [{"type": "deny",
                        "message": reason or f"command matches denied pattern {pattern!r}"}]
        else:
            actions = [{"type": "require_approval",
                        "message": reason or f"command matches: {pattern!r}"}]
        rule = Rule(
            name=f"command_pattern:{label or pattern[:30]}",
            description=f"[migrated] {reason}" if reason else f"[migrated] {pattern}",
            scope=scope,
            trigger="before_tool_call",
            condition=condition,
            actions=actions,
            priority=90,
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("command_patterns migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def migrate_default_project_rules(engine: Any) -> dict:
    """Seed sensible Project-scope defaults for the issues that surfaced
    during multi-agent observability work. Each rule applies to every
    project (scope.targets=["*"]) but only inside project chat scope —
    they don't fire on solo or meeting paths.

    Defaults are mostly WARNS (educational nudge), not denies — so
    existing flows aren't broken. Admin can toggle to deny via the
    Settings → 规则引擎 UI when ready to enforce harder.

    Source tag: "migrator:default_project_rules" — re-running purges
    and regenerates (idempotent). Admin-edited copies (source="admin")
    are untouched. Disabling a default rule via UI also persists across
    re-runs (the migrator deletes by source, then re-adds with
    enabled=True; so admin's "disabled" state is reset on re-run —
    that's by design: re-run = "give me the factory defaults again").
    """
    source = "migrator:default_project_rules"
    removed = _purge_source(engine.store, source)

    rules = [
        # 1. agent 用 glob_files 查项目状态 → 强制走 project_state
        # 2026-05-06: 升级为 deny。warn 不够 — 实测 agent 会无视提示,
        # 死循环 glob_files **/* 4+ 次 (用户反馈截图)。改 deny 后,
        # agent 第一次就拿到 error,只能转去用 project_state。
        Rule(
            name="no glob in project chat — use project_state",
            description=(
                "[default] 项目内禁止 glob_files / search_files 查状态。"
                "用 project_state(scope=my, project_id=...) — "
                "Milestone/Deliverable/Task 才是真值。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_tool_call",
            condition={"field": "tool_name", "in": ["glob_files", "search_files"]},
            actions=[{
                "type": "deny",
                "message": ("项目内禁止 grep — 用 project_state(scope='my', "
                            "project_id='<id>') 查自己的任务/产出/缺什么。"
                            "看具体 step 用 project_state(scope='step', step_id='...')。"),
            }],
            priority=5,
            source=source, created_by="default",
        ),

        # 2. 写文件散落在共享 workspace 根目录 (没在 agent 自己的子目录)
        Rule(
            name="file should be in agent subdir, not shared root",
            description=(
                "[default] 文件请写到自己的 <role>-<name>/ 子目录,"
                "不要散落在 shared/ 根。便于追踪归属 + 验收。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_file_write",
            condition={
                "all": [
                    {"field": "args.path", "contains": "/workspaces/shared/"},
                    {"not": {"field": "args.path",
                              "matches": "/(coder|reviewer|pm|tester|general|researcher|admin|architect|devops|designer)-[^/]+/"}},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("文件写在共享 root 不便于追踪,"
                            "请写到 coder-小新/ 这种子目录里"),
            }],
            priority=8,
            source=source, created_by="default",
        ),

        # 3. workflow 任务标 done 但 0 输出文件
        Rule(
            name="workflow task needs output_files to mark done",
            description=(
                "[default] workflow step 标 done 之前必须至少有 1 个 "
                "output_file (auto-tracked from write_file/edit_file)。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_task_done",
            condition={
                "all": [
                    {"field": "task.created_by", "eq": "workflow"},
                    {"field": "task.output_files", "length_lt": 1},
                ],
            },
            actions=[{
                "type": "deny",
                "message": ("workflow 任务必须留下产物。"
                            "调 write_file 写出至少一个文件,再标 done"),
            }],
            priority=15,
            source=source, created_by="default",
        ),

        # 4. 测试任务标 done 但没有 test-report 命名的文件
        Rule(
            name="reviewer/tester step needs a *report* file",
            description=(
                "[default] 测试/审查类任务的 done 应该附带一份 report 文件"
                "(test-report.md / review-report.md 等)。否则容易出现 "
                "「大卫无报告标完成」的情况。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_task_done",
            condition={
                "all": [
                    {"any": [
                        {"field": "task.title", "contains": "测试"},
                        {"field": "task.title", "contains": "审查"},
                        {"field": "task.title", "contains": "review"},
                        {"field": "task.title", "contains": "test"},
                    ]},
                    {"not": {"field": "task.output_files", "matches": "report"}},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("测试/审查任务建议产出 ...-report.md/json,"
                            "便于其他 agent 验收"),
            }],
            priority=12,
            source=source, created_by="default",
        ),

        # 5. milestone 标 done 但 evidence 字段太短 (没写清楚做了什么)
        Rule(
            name="milestone confirmation needs ≥50 chars evidence",
            description=(
                "[default] PM 确认 milestone 之前,evidence 字段至少 50 字符 — "
                "写清楚交付了什么、谁做的、在哪。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_milestone_done",
            condition={"field": "milestone.evidence_length", "lt": 50},
            actions=[{
                "type": "warn",
                "message": ("milestone evidence 太短,写清楚交付物 + 验收线索"),
            }],
            priority=10,
            source=source, created_by="default",
        ),

        # 6. 单 agent 在一个项目里同时进行的任务 > 3 → 提醒
        Rule(
            name="agent shouldn't carry > 3 in-flight tasks",
            description=(
                "[default] 单 agent 在同一项目里同时承担超过 3 个任务,"
                "通常意味着分配过载,质量会受影响。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_task_assign",
            condition={"field": "assignee.current_task_count", "gt": 3},
            actions=[{
                "type": "warn",
                "message": ("该 agent 当前任务已 4+,建议先消化再派新的"),
            }],
            priority=7,
            source=source, created_by="default",
        ),
    ]

    added = 0
    for r in rules:
        engine.store.add(r, by="migrator")
        added += 1
    logger.info("default_project_rules migration: removed=%d added=%d",
                removed, added)
    return {"removed": removed, "added": added}


def migrate_default_coder_rules(engine: Any) -> dict:
    """Seed coder-role methodology rules using Tier-2 PEP enrichment
    (recent_tool_call_names, recent_write_paths, project.has_design_doc /
    has_plan_md, task.status). These nudge coder agents toward the
    superpowers-engineering flow (brainstorm → plan → TDD → verify).

    Defaults are WARNS (educational); admin can flip to deny via the
    Settings → 规则引擎 UI when ready to harden.

    Source tag: "migrator:default_coder_rules" — re-run purges +
    re-adds. Admin-edited copies (source != this) untouched.
    """
    source = "migrator:default_coder_rules"
    removed = _purge_source(engine.store, source)

    rules = [
        # 1. coder writes impl code without a design doc → warn
        Rule(
            name="coder: write impl without design doc",
            description=(
                "[default] coder 写实现代码(src/ app/ lib/ 下的 .py/.js/"
                ".ts/.go/.rs)前,项目内应先有设计文档(docs/superpowers/"
                "specs/ 或 docs/specs/ 下的 .md)。superpowers 流程要求 "
                "brainstorm → plan → TDD → verify。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_tool_call",
            condition={
                "all": [
                    {"field": "agent.role", "eq": "coder"},
                    {"field": "tool_name", "in": ["write_file", "edit_file"]},
                    {"field": "args.path",
                     "matches": r"^(src|app|lib|server|client)/.*\.(py|js|ts|jsx|tsx|go|rs|java|cpp|cc|c)$"},
                    {"field": "project.has_design_doc", "eq": False},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("Coder methodology: 写实现代码前先 brainstorm 设计 + "
                            "保存到 docs/superpowers/specs/<topic>-design.md。"
                            "见 superpowers-engineering skill。"),
            }],
            priority=10,
            source=source, created_by="default",
        ),

        # 2. coder writes impl code without a plan doc → warn
        Rule(
            name="coder: write impl without plan",
            description=(
                "[default] coder 设计批准后,实现前应先有 plan 文档(docs/"
                "superpowers/plans/ 或 docs/plans/)。plan 包含目标、任务"
                "清单(checkbox)、每步验证标准。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_tool_call",
            condition={
                "all": [
                    {"field": "agent.role", "eq": "coder"},
                    {"field": "tool_name", "in": ["write_file", "edit_file"]},
                    {"field": "args.path",
                     "matches": r"^(src|app|lib|server|client)/.*\.(py|js|ts|jsx|tsx|go|rs|java|cpp|cc|c)$"},
                    {"field": "project.has_plan_md", "eq": False},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("Coder methodology: 设计已批准的话,先把实施计划"
                            "写到 docs/superpowers/plans/YYYY-MM-DD-<feature>.md "
                            "(含 checkbox 任务清单)再开始实现。"),
            }],
            priority=10,
            source=source, created_by="default",
        ),

        # 3. coder calls submit_deliverable without running tests → warn
        Rule(
            name="coder: submit without verification",
            description=(
                "[default] coder 调 submit_deliverable 前,本轮应跑过测试 / "
                "lint / smoke 验证(run_tests 或 bash 跑 pytest/jest/...)。"
                "verification-before-completion 是 superpowers 的硬关卡。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_tool_call",
            condition={
                "all": [
                    {"field": "agent.role", "eq": "coder"},
                    {"field": "tool_name", "eq": "submit_deliverable"},
                    {"not": {"field": "agent.recent_tool_call_names",
                              "contains": "run_tests"}},
                    {"not": {"field": "agent.recent_tool_call_names",
                              "contains": "bash"}},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("Coder methodology: 提交前先跑测试 / lint / smoke "
                            "验证,把结果贴到 deliverable description 里 "
                            "(verification-before-completion)。"),
            }],
            priority=12,
            source=source, created_by="default",
        ),

        # 4. coder writes impl code AHEAD of any test file (TDD violation)
        Rule(
            name="coder: TDD — tests should come first",
            description=(
                "[default] TDD 是 superpowers 硬规则:实现 src/foo.py 之前,"
                "应先有 tests/test_foo.py 或类似测试存在。本轮如果之前没有"
                "写过测试文件就开始写实现属于 anti-pattern。"
            ),
            scope=RuleScope("project", ["*"]),
            trigger="before_tool_call",
            condition={
                "all": [
                    {"field": "agent.role", "eq": "coder"},
                    {"field": "tool_name", "in": ["write_file", "edit_file"]},
                    {"field": "args.path",
                     "matches": r"^(src|app|lib|server|client)/.*\.(py|js|ts|jsx|tsx|go|rs)$"},
                    {"field": "agent.recent_write_paths", "length_lt": 1},
                ],
            },
            actions=[{
                "type": "warn",
                "message": ("Coder methodology: TDD 测试先行 — 实现前确保"
                            "本轮已写过对应的 test_*.py / *.test.ts。"
                            "看不到测试就直接写实现,补回去。"),
            }],
            priority=8,
            source=source, created_by="default",
        ),
    ]

    added = 0
    for r in rules:
        engine.store.add(r, by="migrator")
        added += 1
    logger.info("default_coder_rules migration: removed=%d added=%d",
                removed, added)
    return {"removed": removed, "added": added}


def run_all_migrations(engine: Any) -> dict:
    """Convenience: run every implemented migrator. Returns aggregate
    summary {migrator_name: {removed, added}}."""
    out: dict[str, dict] = {}
    for name, fn in (
        ("workflow_catalog", migrate_workflow_catalog),
        ("global_denylist", migrate_global_denylist),
        ("tool_risk", migrate_tool_risk),
        ("command_patterns", migrate_command_patterns),
        ("default_project_rules", migrate_default_project_rules),
        ("default_coder_rules", migrate_default_coder_rules),
    ):
        try:
            out[name] = fn(engine)
        except Exception as e:
            logger.exception("%s migrator crashed", name)
            out[name] = {"error": str(e)}
    return out
