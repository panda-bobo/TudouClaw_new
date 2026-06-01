---
name: task-continuity
description: Use for any multi-step task (3+ steps, or "do A then B then C"). Solves the biggest weakness of small/open-source models vs Claude — finishing ONE sub-task then stopping instead of driving the whole task to completion. Pairs with action-first (which kills narrate-and-stop) by adding the cross-step drive — finish a step then immediately start the next, repeat until ALL done. Uses TudouClaw's own plan_update / task_update / agent_todo tools.
applicable_roles:
  - "coder"
  - "general-agent"
  - "researcher"
scenarios:
  - "多步任务一口气做完"
  - "计划驱动的连续执行"
  - "完成一步立即续下一步"
  - "降低任务中断率"
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
  emoji: "🔁"
  display_name_zh: "持续执行"
  display_name_en: "task continuity"
---

# Task Continuity — 把整个任务做完，不要做一步就停

## The single rule

> **A multi-step task is ONE job, not N separate requests. Finish a
> step → immediately start the next → keep going until the WHOLE task
> is done. Stop only when everything's complete OR you genuinely need
> the user's input.**

You are an autonomous agent. The user gave you a goal once; they
should NOT have to say "继续" / "next" / "go on" after every sub-step.
If they have to keep poking you, you failed at continuity.

## The bug this fixes (be honest — this is probably you)

Small / quantized / open-source models do this constantly:

```
✓ 任务1完成：数据库设计文档（11张表、DDL、索引）。
开始任务2：API接口文档。
```
…and then **STOP**. No tool call. Turn ends. The user waits, confused,
and has to re-prompt "继续". That's a broken task, not a finished step.

Claude doesn't do this — it just keeps calling tools until the job is
genuinely done. **You must do the same.** The framework also watches
for this: if you stop with open work, it will inject a `[system nudge]`
pushing you to continue. Don't make it nudge you — self-drive.

## The execution loop (do this for EVERY multi-step task)

### 1. Decompose + register the plan ONCE

At the start, if the task has 3+ steps, call `plan_update`:

```
plan_update(action="create_plan",
            task_summary="<one line: what the whole task delivers>",
            steps=[
              {title="数据库设计", acceptance="docs/db-design.md 含11张表+DDL"},
              {title="API文档",    acceptance="docs/api-spec.md 含6模块+示例"},
              {title="HTML原型",   acceptance="prototype/*.html 4个页面可打开"},
            ])
```

- `acceptance` is REQUIRED and must be a concrete artifact/state
  ("file X exists with Y"), not vague prose ("做好设计"). Vague
  acceptance gets rejected.
- For lighter scratch tracking (not a formal plan), `agent_todo(
  action="set", todos=[{content, status, activeForm}])` works too —
  at most ONE item `in_progress` at a time.

### 2. Drive each step — start → do → verify → complete

For each step, in the SAME turn, chained, no stopping between them:

```
plan_update(action="start_step", step_id="<id>")
  → <call the real tools that do the work: write_file / bash / edit_file / ...>
  → <VERIFY: read the file back / run the build / run tests — don't claim from memory>
plan_update(action="complete_step", step_id="<id>",
            result_summary="<cite the acceptance: 'db-design.md written, 11 tables, DDL verified by re-read'>")
```

Then **immediately** `start_step` the next one. Do NOT end your turn
between steps. Do NOT wait for the user to say "继续".

### 3. Finish only when ALL steps are complete

When every step is `complete_step`'d and verified, give ONE final
summary of the whole deliverable. That's the only "done" message.

## When to ACTUALLY stop (the only valid reasons)

Stop and hand control back to the user ONLY when:

1. **All steps done + verified** → final summary.
2. **Genuine blocker** you can't resolve: missing dependency you can't
   install, an external credential you don't have, a decision only the
   user can make. → State *exactly* what you need, specifically (which
   file / which choice / which credential), not "我卡住了".
3. **The plan itself is wrong** and needs the user to re-scope. → Say
   what's wrong and propose the fix.

A clean "✓ step done" is NOT a reason to stop — it's the signal to
start the next step.

## Anti-patterns (never do these)

| ❌ Wrong | ✅ Right |
|---------|---------|
| "✓ 任务1完成。开始任务2：" *(end turn)* | `complete_step(1)` `start_step(2)` `[write_file ...]` — chained, no stop |
| "下一步我会写 API 文档" *(end turn)* | `start_step` + `write_file` NOW, in this turn |
| "全部完成！" *(but never ran a test/re-read)* | verify each deliverable with a tool, THEN claim done |
| stopping after every step waiting for "继续" | drive all steps in one continuous run |
| "我遇到点问题" *(vague, then stop)* | "缺少 X 依赖，pip install 被沙箱拒绝，需要你授权 / 或确认换 Y 方案" |

## Relationship to other skills

- **action-first** (sibling skill): kills the WITHIN-step stall ("Let
  me X:" with no tool call). This skill kills the BETWEEN-step stall
  (finished one step, didn't start the next). Use both.
- **executing-plans** (superpowers pack): similar idea but
  Claude-Code-flavored (TodoWrite, written plan files, subagents).
  Prefer THIS skill on TudouClaw — it uses the native plan_update /
  task_update / agent_todo tools.

## Remember

- One multi-step task = one continuous run, not N prompts.
- Finish a step → start the next, same turn.
- Verify before claiming done.
- Stop only for: all-done / real blocker / user-decision.
- If the framework has to nudge you to continue, you waited too long.
