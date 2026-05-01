# Session Handoff — 2026-05-01

> **Purpose:** Single document the next session can read to pick up right
> where the previous session stopped, without trawling through commit
> messages or chat transcripts.
>
> **How to use it:** Next session, start with `read HANDOFF.md` then
> tell me which item from §3 to work on.

---

## 1. Current state

**Branch**: `main` (in sync with `origin/main`)
**Last commit**: `3430164` — `feat(canvas): workflow.executable schema field + validation + status UI`
**Server**: PID 36509 running on `:9090`, started 2026-05-01 09:50.
**Working tree**: clean.

The system has all four high-level features in working state:

* Skill store (with admin-curated dual-dimension category taxonomy)
* Visual orchestration canvas (drag-drop DAG editor; saves but doesn't
  yet execute)
* Skill execution path with QA-gate guidance in `_build_granted_skills_roster`
* Drawio + pptx skill SKILL.md (runtime — not in git, see §6)

There are **8 open items** before the system meets the user-defined
"上线验收标准" (§3).

---

## 2. What landed today

In commit-recent-first order:

| Commit | Subject | Files changed |
|--------|---------|---------------|
| `3430164` | feat(canvas): workflow.executable schema field + validation + status UI | 3 |
| `993eba7` | feat(orchestration): visual drag-drop canvas for authoring DAG workflows | 5 |
| `610fa9c` | feat(skills): admin-defined two-dimensional category taxonomy | 4 |
| `df6aa1f` | feat: QA-gate guardrails + chat UX + MCP secrets + memory v2 (large batch) | 55 |

The earlier `df6aa1f` is the umbrella batch that included:

* QA-gate roster injection in `_build_granted_skills_roster()`
  (agent.py)
* pptx-author SKILL.md unified QA gate
* portal_bundle.js chat-bubble ring-buffer dedup (4-layer mitigation)
* portal.html textarea max-height 120 → 320 with resize: vertical
* MCP credential at-rest encryption (`app/mcp/secrets.py`)
* memory v2 split (`memory_dream.py` / `memory_extractor.py` / `memory_topic.py`)
* knowledge module split (`knowledge.py` → `knowledge/`)
* LLM resilience (urllib3 retry kill, `_CONNECT_TIMEOUT` 45 → 10s)

---

## 3. Open items — the 上线验收标准

Listed in the order the **user prioritised** them. Status, risk, scope,
and enough breadcrumbs that the next session can start without
re-investigating.

### 🔴 [B] Agent 重复消息 Bug — root cause

**User priority**: 必须做 (P0)
**Risk**: HIGH — touches every agent's chat reply path
**Scope estimate**: 200–500 LoC + a regression test
**Status**: **Mitigated only.** The 4th-layer ring-buffer dedup landed
in `df6aa1f` masks the symptom but doesn't fix the source.

**What's actually wrong**

Symptom: a single agent reply renders 4× in the chat panel.

Diagnosed root cause: in `app/server/static/js/portal_bundle.js` around
lines 7040–7060, the `text_final` event handler uses a single-slot
`window['_lastFinalizedText_' + agentId]` to dedup against the most
recent finalized message. That slot is **cleared** on line 7057 right
after a successful match, so a second identical `text_final` for the
same logical reply (which does happen — see "why it happens" below)
slips through and creates a fresh bubble.

But that's the front-end symptom. The real question is: **why is the
backend emitting the same `text_final` multiple times in one turn?**

**Hypothesis (unverified)**: agent.py reply lifecycle has a path that
triggers re-finalization. Candidates:

1. Wake-up / watchdog re-entry on the same turn → re-runs the LLM and
   emits another `text_final` for the same logical reply.
2. Streaming retry path (`urllib3` chunked stream cuts mid-emit) → the
   retried stream completes and emits a second `text_final`.
3. `flush_action_buffer` on conversation end accidentally re-emits the
   last assistant message.

**How to attack it next session**

```
1. grep for 'text_final' in app/agent.py and app/agent_execution.py
2. Add structured logging at every emit point: agent_id, turn_id,
   message hash, reason
3. Reproduce locally — open a chat, ask something that takes a few
   tool calls, watch the log for duplicate emits
4. Find the path, fix at source, write a regression test that
   simulates that path and asserts exactly one final per turn
5. Once confident, REMOVE the front-end ring-buffer dedup in
   portal_bundle.js _canvasState... (lines around addChatBubble) so
   the backend fix is load-bearing
```

**Acceptance**: a chat with an 8-step agent task produces exactly one
final assistant bubble per turn. Front-end dedup helpers can be
deleted without regressing.

---

### 🔴 [C] 平台层强制 QA Gate hook

**User priority**: 必须做 (P0)
**Risk**: MEDIUM — touches `fs.py` write_file and `mcp.py` send_email
**Scope estimate**: 300–500 LoC
**Status**: Not started. Currently only documented in
`_build_granted_skills_roster()` (KV-cached prompt) and individual
SKILL.md sections — agent can still ignore them.

**Design**

Three hook points in the tool execution layer; each returns either
`(ok, payload)` or `(blocked, reason)`. A blocked tool call surfaces
to the agent as an error, NOT a silent skip — agent sees the same
shape it sees for any other tool failure and has to fix the input.

```python
# app/tools_split/fs.py  _tool_write_file
hook_result = _qa_gate_check(path, content, agent=self)
if not hook_result.ok:
    return TOOL_ERROR(f"QA gate blocked write: {hook_result.reason}")

# app/mcp/dispatcher.py  before send_email-class MCP calls
hook_result = _validate_email_args(arguments)
if not hook_result.ok: ...

# app/agent.py  before declaring "task done" / "completed"
# (harder — needs intent detection in the assistant message)
```

**QA gate functions per file type** (port from the SKILL.md snippet
table in `_build_granted_skills_roster`):

| Pattern | Check |
|---------|-------|
| `*.pptx` | python-pptx blank-page + overlap detect (already in pptx-author/SKILL.md, copy here) |
| `*.drawio` | as="geometry" count + edge source/target ratio |
| `*.png` (after diagram export) | min size 600×400 + pixel-per-cell ≥5K |
| `*.md` | placeholder regex (xxx/TODO/[insert]/Lorem) |
| email args | recipient regex + subject non-empty + attachment isfile |

**Acceptance**: agent tries to `write_file gpu.pptx` with 30 overlap
issues → write is rejected, error returned, agent retries.

---

### 🔴 [D] 画布执行引擎

**User priority**: 必须做 (P0)
**Risk**: HIGH — completely new subsystem
**Scope estimate**: ~2000 LoC across new module + UI integration
**Status**: Not started. Schema (`executable_status`) is ready as of
commit `3430164`; this commit is what the engine consumes.

**Design sketch**

```
app/canvas_executor.py  (new module, ~800 LoC)
├── class WorkflowRun       — one execution instance, has state
├── class WorkflowEngine    — picks ready workflows, spawns runs
├── class NodeExecutor      — per-node-type implementations
│   ├── _execute_agent     → talks to hub.agents, posts a turn,
│   │                         polls for completion event
│   ├── _execute_tool      → looks up skill, invokes via tool registry
│   ├── _execute_decision  → evaluates condition, picks one outgoing edge
│   ├── _execute_parallel  → kicks off all outgoing edges, awaits all
│   └── _execute_end       → finalize run
└── class RunStateMachine   — pending → running → succeeded/failed
```

**Persistence**

* Run state: `<data_dir>/canvas_runs/<run_id>.json` — incremental log
* Per-node events: stream of `{ts, run_id, node_id, type, payload}` —
  used by [E] for live highlighting

**Integration points**

* Trigger: `POST /api/portal/canvas-workflows/{id}/runs` (button on
  editor toolbar — only enabled when status=ready)
* Progress: `GET /api/portal/canvas-workflows/{id}/runs/{run_id}`
* SSE event stream: `GET .../runs/{run_id}/events` (consumed by [E])

**MVP boundary** (NOT in first cut)

* No retries / circuit breakers
* No human-in-the-loop pause nodes
* No conditional fan-in (parallel join always waits for ALL branches)
* Variable substitution NOT in MVP — that's [H]

**Acceptance**: a 5-node linear workflow (start → agent → tool →
agent → end) runs to completion, state machine transitions visible
via API.

---

### 🟡 [E] 运行时拓扑高亮

**User priority**: 必须做 (P0)
**Risk**: MEDIUM — front-end SSE consumer + SVG state updates
**Scope estimate**: ~500 LoC
**Status**: Not started. Depends on [D].

**Design**

* Backend already has `/runs/{run_id}/events` from [D].
* Front-end opens an EventSource when the user opens an executing
  workflow's editor view.
* Per-node CSS state classes (added to existing SVG nodes):
  * `.node-pending` — grey
  * `.node-running` — yellow + pulsing animation
  * `.node-done`    — green
  * `.node-error`   — red + flash

**Existing infrastructure**

* `_canvasRedrawSvg()` already re-renders on every state change.
* Just need an `_canvasApplyRunState(runState)` function that mutates
  node visual state without a full redraw (full redraw would lose
  user's drag position if they happen to interact during a run).

**Acceptance**: open a running workflow, watch nodes light up in
sequence as the engine progresses.

---

### 🟡 [F] KV cache 刷新机制 — PARTIAL DONE 2026-05-01 (commit `0ece8a0`)

**User priority**: 强烈建议 (P1)
**Risk**: LOW
**Status**: **Core fix landed.** Two HANDOFF sub-items DEFERRED based
on code-reading; they are nice-to-haves, not currently load-bearing.

**What landed (commit `0ece8a0`)**:

* `_compute_static_prompt_hash` now also folds each granted skill's
  SKILL.md mtime_ns into the hash inputs. Edit a SKILL.md → next chat
  turn rebuilds the cached static prompt with fresh content. No restart,
  no re-grant. +24 LoC in `app/agent.py`.

**What was DEFERRED and why**:

1. **Explicit `_invalidate_prompt_cache()` on grant / revoke** —
   redundant. Read `_compute_static_prompt_hash` in `app/agent.py`:
   it already pulls `_r.list_for_agent(self.id)` live from the
   registry, so grant_ids change → hash flips → cache rebuilds. The
   HANDOFF item was overcautious; the regression it described doesn't
   exist in current code.
2. **Manual `POST /api/portal/agent/{id}/refresh-cache` endpoint** —
   hold for now. mtime-based invalidation should cover real cases.
   Add later if needed (~15 LoC, just calls `agent._cached_static_prompt
   = ""; agent._static_prompt_hash = ""` then returns ok). Triggers
   for adding it: filesystems with coarse mtime resolution, NFS-cached
   layouts, or evidence agents are still seeing stale prompts after
   SKILL.md edits.

**Verification**: smoke test (in commit message) — hash flips on edit
and on delete; still graceful when SKILL.md missing.

**Acceptance** (original): edit a SKILL.md → next user message in same
chat shows agent following the new rule. **Met** by the mtime change.

---

### ✅ [G] Skill SKILL.md 补齐 — DONE 2026-05-01 (commit `2b41389`)

**User priority**: 强烈建议 (P1)
**Risk**: LOW
**Status**: **Done.** Real scope was much smaller than the original
estimate — see "Reality vs. plan" below.

**What landed**:

1. `app/skills/builtin/send_email/SKILL.md` — added `## 工作流（4 步）`
   (echo plan → validate → call → report message_id) and `## 质量门`
   (pre-call validator: recipient regex, subject non-empty + length,
   body non-empty, absolute-path attachment file existence). +54 lines.
2. `app/skills/builtin/take_screenshot/SKILL.md` — added `## 工作流
   （3 步）` and `## 质量门` (post-call validator: file exists, size
   > 1KB to catch black/empty PNGs from permission errors or display
   sleep, dimensions sanity). +48 lines.

**Reality vs. plan**:

* HANDOFF said send_email/SKILL.md was MISSING — actually it existed
  (a 72-line reference doc); we **augmented** rather than created.
* `jimeng_video/SKILL.md` — already deprecated, `main.py` just raises
  a migration RuntimeError. Skipped, no QA gate makes sense for a
  no-op stub.
* `summarize-pro` — third-party skill in `~/.tudou_claw/`, out of git
  scope. Skipped per the original "not in git" caveat.

**Verification**: both files still parse via
`read_entry_from_skill_md`; all embedded python blocks compile;
validator functions exec correctly on representative valid/invalid
inputs.

**Note for [C]**: the gates here are **doc-only** — agent has to
choose to run them. Platform-level enforcement (the pattern HANDOFF
[C] describes) should reuse this same validation logic. Specifically,
`fs.py write_file` doesn't see send_email args, but the MCP dispatcher
hook ([C]'s 2nd integration point) can port the `validate_send_email`
function from `send_email/SKILL.md` directly.

**Template** (use the QA gate skeleton from
`app/skills/builtin/tudou-builtin/pptx-author/SKILL.md` as the
reference):

```markdown
## 工作流（X 步,不要跳步）
1. 校验输入 ...
2. 执行 ...
3. 校验输出 ...
4. 汇报给用户 ...

## 质量门（声明完成前必须通过）
< pasted python QA gate >
```

**Acceptance**: agent uses send_email skill → validates recipient
locally before hitting the MCP, refuses to send to a fabricated
address, surfaces clear error.

---

### 🟢 [H] 节点间变量 `{{var_name}}` 系统

**User priority**: 强烈建议 (P1)
**Risk**: MEDIUM — couples to [D] execution engine
**Scope estimate**: ~400 LoC
**Status**: Not started. Depends on [D].

**Design**

Each node, when it completes, deposits a structured output into the
run's variable store:

```json
{
  "run_id": "run-...",
  "vars": {
    "n_planner.output":      "...string...",
    "n_planner.success":     true,
    "n_drawio.png_path":     "/.../gpu.png",
    "n_drawio.exit_code":    0,
    ...
  }
}
```

Downstream nodes can reference upstream values in their `config`:

```json
{
  "id": "n_pptx",
  "type": "agent",
  "config": {
    "prompt": "把 {{n_drawio.png_path}} 嵌入到 PPT 第 3 页"
  }
}
```

The executor substitutes `{{...}}` patterns at node-start time. Missing
variables produce a clear error (don't silently substitute empty
string — that masks bugs).

**UI affordance** (canvas editor right panel):

* Hover an upstream node → tooltip lists its expected output keys
* Click a key → copies `{{node_id.key}}` to clipboard
* Linting: highlight `{{...}}` in node config that don't exist

**Acceptance**: a workflow `start → drawio_agent → pptx_agent → end`
where pptx_agent's prompt references `{{drawio_agent.png_path}}`
runs successfully end-to-end.

---

## 4. Recommended next-session opening sequence

```
1. read HANDOFF.md (this file)
2. git status && git log -3 --oneline
3. confirm server still running on :9090
4. user picks ONE item from §3 — work it to completion
5. commit + push that one item before starting another
```

**Recommended order** (low-risk → high-risk, each fully tested before
the next):

```
[G] (Skills SKILL.md补齐)         — safest, immediate UX win
[F] (KV cache refresh)            — independent, fixes "改了不生效" pain
[C] (Platform QA Gate hook)       — narrowed scope, big leverage
[B] (Duplicate-message root)      — risky, but with tests becomes safe
[D] (Execution engine)            — biggest, do it AFTER [B] [C] [F]
[E] + [H] (highlight + vars)      — depend on [D], do last
```

**Total estimated scope**: ~3500–4000 LoC + tests, across 6 sessions.

---

## 5. Quick reference

### File map of the work in §3

```
app/agent.py
  ├─ _build_granted_skills_roster() — done (df6aa1f)
  ├─ chat reply lifecycle           — [B] target
  └─ _static_prompt_hash            — [F] target

app/canvas_workflows.py             — done (993eba7 + 3430164)
app/api/routers/canvas.py           — done

app/canvas_executor.py              — [D] new file
app/canvas_runs/<run_id>.json       — [D] runtime artifacts

app/tools_split/fs.py               — [C] hook here
app/mcp/dispatcher.py               — [C] hook here

app/skills/builtin/send_email/SKILL.md   — [G] create
app/skills/builtin/take_screenshot/SKILL.md  — [G] augment
app/skills/builtin/jimeng_video/SKILL.md     — [G] augment

app/server/static/js/portal_bundle.js
  ├─ _canvasState / canvas editor — done
  ├─ ring-buffer dedup            — REMOVE after [B]
  ├─ _canvasApplyRunState()       — [E] add
  └─ {{var_name}} hover/lint      — [H] add
```

### Auth note

`_require_admin()` helper exists in `app/api/routers/skills.py`. Reuse
that pattern in [C] / [D] / [F] for any admin-gated endpoint.

### Validation note

The cycle-detection / reachability code in
`canvas_workflows.WorkflowStore.validate_for_execution` is reusable —
the executor in [D] can call it again pre-flight before starting a
run, so a workflow that became invalid after marking ready (e.g. a
referenced agent was deleted) still fails closed.

---

## 6. Important: things that are NOT in git

These touched today's work but live in `~/.tudou_claw/` (user-local)
and are NOT tracked by git. If you want them git-tracked you have to
fork them into `app/skills/builtin/` first.

* `~/.tudou_claw/skills_installed/md_Agents365-ai_drawio-skill/SKILL.md`
  — heavily edited today (Step 3.5 pre-flight, --width export, etc.).
  Lives in user dir because drawio-skill is a third-party install.
* `~/.tudou_claw/pending_skills/imported/drawio-skill/SKILL.md` —
  synced copy of the above.
* `~/.tudou_claw/skill_categories.json` — created at runtime by the
  category store (commit 610fa9c) on first launch. Default seeds 8+8.
* `~/.tudou_claw/skill_category_assignments.json` — per-skill category
  memberships. Empty until admin starts tagging.
* `~/.tudou_claw/workflows/` — canvas workflow files. Empty until user
  saves the first workflow.

---

## 7. Known operational state at handoff

* Server PID **36509** running, started 2026-05-01 09:50
* Current `_build_granted_skills_roster` upgrade requires server
  restart to take effect; current PID was started AFTER that change
  was on disk so it should already include it (verified during the
  session by re-importing the function and checking for "强制 QA Gate"
  string).
* Active agents (per Agent leaderboard): 7 idle, 0/0 history.
  Leaderboard counter only ticks on long_task DONE/BLOCKED transitions
  — pure chat doesn't count. Documented; not a bug.
* Latest visible chat for agent `a16c2710acb6` (小刚) showed duplicate
  rendering of "任务已完成,流程图的源文件和预览图均已就绪..." × 4 —
  mitigated by the front-end ring buffer; root [B] still open.
* GPU cluster topology PPT in 小刚's workspace has known overlap
  issues that the QA gate flagged but the agent claimed "0 issue".
  Does NOT need to be fixed in code — it's a demo artifact.

---

*End of handoff. Next session: pick one item from §3, work it
end-to-end, commit + push, then update this file's §3 to mark it
done.*
