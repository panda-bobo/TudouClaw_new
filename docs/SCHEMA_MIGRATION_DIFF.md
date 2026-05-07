# Schema Migration — Before vs After Diff

Purpose: catalog **what actually changed** in what reaches the LLM
(both `messages[0]` system prompt content and `tools[]` payload)
between the inline-string era and the schema-driven era. Each row
flags whether the change is **passthrough** (byte-equivalent),
**format-changed** (same info, different rendering), or
**content-added/removed** (LLM sees more or less).

Anything not "passthrough" gets a focused regression test in
`tests/test_prompt_schemas_e2e.py`.

| # | Block | Schema | Status | Test |
|---|---|---|---|---|
| 1 | Plan state | `PlanStateSchema` | format-changed when plan has steps; passthrough via `markdown_fallback` otherwise | ✅ `test_plan_state_*` |
| 2 | Intent hint | `IntentHintSchema` | passthrough(`<intent_hint>` wrapper byte-equivalent) | smoke OK |
| 3 | Playbook | `PlaybookSchema` | passthrough(`<playbook scope=…>` wrapper byte-equivalent) | smoke OK |
| 4 | **Rule Engine rules** | `RuleSchema` | **content-added** — block didn't exist in active path before; LLM now sees up to 12 applicable rules upfront | ✅ `test_rule_engine_block_appears_with_rules` |
| 5 | **Workspace state** | `WorkspaceFilesSchema` | **content-added** — new block listing workspace_root + design/plan flags + top-level files | ✅ `test_workspace_files_*` |
| 6 | Knowledge wiki | `KnowledgeWikiSchema` | passthrough — wraps `_kb.get_prompt_summary()` unchanged | smoke OK |
| 7 | Scheduled context | `ScheduledContextSchema` | passthrough | smoke OK |
| 8 | Git context | `GitContextSchema` | passthrough — wraps `_get_git_context()` unchanged | smoke OK |
| 9 | Memory recall | `MemoryRecallSchema` | passthrough — wraps `mm.retrieve_for_prompt()` unchanged | smoke OK |
| 10 | **Skill roster** | `SkillSchema` | **format-changed** — was 1-line `- \`name\`: desc`; now 6+ lines per skill with id / path / 何时调用 / 适用角色 / 场景 / rules | ✅ `test_skill_roster_format_richer_than_legacy` |
| 11 | Admin instruction | `AdminInstructionSchema` | passthrough — wraps the project.py admin block unchanged | ✅ `test_admin_schema_passthrough` |
| 12 | **Tool validation error** | `render_tool_signature` | **format-changed** — was 1-line `tool(a:str, b:int=0)`; now multi-line with `# description` per param | ✅ `test_tool_error_signature_includes_descriptions` |
| 13 | **`tools[]` payload** | `ToolSchema.to_openai_payload()` | **structurally-equivalent** but normalized — strips `_*` server-side params, preserves `oneOf/anyOf/allOf/$ref/not` via `raw_schema` | ✅ `test_tool_schema_round_trip_all_definitions` + `test_tool_schema_preserves_oneof_anyof_complex_schemas` |

## High-risk diffs explained

### #4 Rule Engine — content-added

The active prompt-assembly path (`agent.py:_build_dynamic_context`)
NEVER had this block before. Now it surfaces up to 12 applicable
rules from `rule_engine.store.for_trigger(...)` for action-time
hooks (`before_tool_call` / `before_file_write` /
`before_dispatch_task` / `before_task_done` / `before_message_send`),
filtered to the agent's scope (project / meeting / solo).

Net effect: **system_prompt grows by ~500–2000 chars** depending on
rule count. LLM stops the "try → deny → retry" loop because it
sees the rule before the action.

### #5 Workspace state — content-added

Same `_build_pep_workflow_enrichment` data Rule Engine evaluates
against — now also surfaced to the LLM so it sees `has_design_doc`
/ `has_plan_md` and the top-level workspace listing without having
to glob-discover.

Net effect: ~200–500 chars added when in a project context.
Skipped silently for solo agents.

### #10 Skill roster — format-changed

**Before** (per skill):

```
- `superpowers-engineering`: Engineering workflow pack — combined planning,…
```

**After** (per skill):

```
### `superpowers-engineering` (id: `sp_eng_001`)
**何时调用:** 处理任何非平凡的开发任务时调用 — brainstorm → plan → TDD → review → 发版。
**典型场景:** 新功能开发, bug 修复, 代码评审
**适用角色:** coder, architect, reviewer
📂 `/Users/.../skills_installed/sp_eng_001` — `read_file <path>/SKILL.md` 查完整用法
**规则:**
- 未拿到设计批准前禁止写代码
- 测试必须先于实现
```

**Net effect**: each skill grows from ~80 chars → ~300–500 chars.
For a typical 6-skill agent, total rises from ~500 → ~2500 chars.
This is intentional (the LLM now has structured "when to call" hints
instead of a guess-from-name blurb), but worth budgeting.

### #12 Tool validation error — format-changed

**Before**:

```
read_file(path: string [REQUIRED], offset: integer = 0)
```

**After**:

```
read_file(
  path: string [REQUIRED]  # absolute or workspace-relative file path
  offset: integer = 0  # 0-indexed starting line
  limit: integer = -1  # -1 = read entire file
)
```

**Net effect**: error message ~3-5x longer per tool, but the
description-per-param makes self-correction much easier. Caught the
"agent kept retrying with missing path" symptom in the wild.

### #13 `tools[]` payload — structurally-equivalent (with one bug-fix)

`ToolSchema.to_openai_payload()` re-emits each tool entry with:

- ✅ `_*` prefixed params dropped (server-side injected, never LLM)
- ✅ Required-set normalized
- ✅ `oneOf`/`anyOf`/`allOf`/`$ref`/`not` preserved verbatim via
  `raw_schema` (regression-protected — DeepSeek 400 incident
  2026-05-07)

Verified lossless on all 62 live `TOOL_DEFINITIONS` entries.

## What the tests prove

| Test | Catches |
|---|---|
| `test_plan_state_schema_renders_with_steps` | Structured plan render produces same content shape (`<plan_state>` / `task:` / `current:` / `done:` / `pending:`) |
| `test_plan_state_schema_empty_returns_empty_string` | Empty plan → empty render (no skeleton emitted) |
| `test_workspace_files_schema_renders_with_state` | `✓ has_design_doc` / `✗ has_plan_md` flags + top-level entries |
| `test_workspace_files_schema_empty_returns_empty_string` | Solo agent path skips silently |
| `test_workspace_files_schema_caps_top_level_entries` | Cap at 30 entries with `+N more` notice |
| `test_rule_engine_block_appears_with_rules` | Rule Engine block emitted when rules exist; rule names + actions surfaced |
| `test_skill_roster_format_richer_than_legacy` | Skill markdown contains all 6 new fields (id, path, 何时调用, 适用角色, 典型场景, 规则) |
| `test_tool_error_signature_includes_descriptions` | Multi-line signature has `# description` per param |
| `test_tool_schema_round_trip_all_definitions` | All 62 TOOL_DEFINITIONS round-trip lossless |
| `test_tool_schema_preserves_oneof_anyof_complex_schemas` | DeepSeek 400 regression: oneOf preserved, no `type:any` leak |
| `test_tool_payload_and_error_signature_single_source` | tools[] required-set === error-sig required-set |
| `test_admin_schema_passthrough` | Admin block markdown verbatim |
| `test_*_strips_internal_fields` (3 tests) | Server-side fields don't leak into to_llm_dict() |
| `test_ratelimit_loopback_bypass` | 127.x / ::1 / 0.0.0.0 bypass; LAN/WAN ips throttled |
