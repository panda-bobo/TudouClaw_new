# Session Handoff — 2026-05-02

> **Purpose:** Single document the next session can read to pick up right
> where the previous session stopped, without trawling through commit
> messages or chat transcripts.
>
> **How to use it:** Next session, start with `read HANDOFF.md` then
> tell me which item from §3 to work on (most are now done — see §1).

---

## 1. Current state

**Branch**: `main` (8 commits ahead of `origin/main` — **NOT pushed**
per user direction; push when ready).
**Working tree**: clean.

**This session (2026-05-01 → 02) closed out 6 of the 8 open items**
from the prior handoff. New commits on `main` (oldest → newest):

| Commit  | Item | Subject |
|---------|------|---------|
| `2b41389` | [G] | feat(skills): QA gate sections in send_email + take_screenshot SKILL.md |
| `567d52a` | [G] | docs(handoff): mark [G] done |
| `0ece8a0` | [F] | feat(agent): SKILL.md mtime in _compute_static_prompt_hash |
| `5f04bda` | [F] | docs(handoff): mark [F] partial done |
| `1cd8ddb` | [C] | feat(qa-gate): platform-level QA hook for write_file + send_email |
| `eae3c2a` | [B] | fix(agent): sliding-window dedup + structured logging in chat _emit |
| `7f3ea49` | [D]+[H] | feat(canvas): execution engine + run/event API endpoints |
| `8d20144` | [E] | feat(canvas): runtime topology highlighting on the editor |
| `58f2fd3` | [H] | feat(canvas): variable hint panel + lint in node config |

**Status of the 8 original open items** (details in §3):

| ID | Item | Status |
|----|------|--------|
| ✅ | [G] Skill SKILL.md 补齐 | DONE |
| ✅ | [F] KV cache 刷新机制 | PARTIAL DONE (core fix; 2 sub-items deferred) |
| ✅ | [C] 平台层强制 QA Gate hook | DONE (2/3 hook points; intent-detection deferred) |
| 🟡 | [B] Agent 重复消息 Bug | PARTIAL DONE (backend dedup strengthened + diagnostic logs; root path still TBD via live repro) |
| ✅ | [D] 画布执行引擎 | DONE (MVP — start/end/agent/tool; decision/parallel deferred) |
| ✅ | [E] 运行时拓扑高亮 | DONE |
| ✅ | [H] 节点间变量 | DONE (engine + UI hints + lint) |

**Server note**: live preview at port 9091 was used during this
session for [E] / [H] verification; user's PID 36509 on :9090 was
not touched. Either server needs a restart to pick up the new code
(or the new agents will see new code on next chat — for [F] mtime
detection, [B] dedup, [C] gates).

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

### 🟡 [B] Agent 重复消息 Bug — PARTIAL DONE 2026-05-02 (commit `eae3c2a`)

**User priority**: 必须做 (P0)
**Risk**: HIGH — touches every agent's chat reply path
**Status**: **Backend dedup strengthened + diagnostic logging in
place.** Root cause not yet identified (would need live reproduction
on the running server).

**What landed (commit `eae3c2a`)**:

* In `agent.py` `_emit` (chat loop closure), replaced the single-slot
  `_last_emitted_text_ref` with a sliding 5-entry ring (60s TTL,
  normalized whitespace, exact-or-mutual-prefix match). Mirrors the
  front-end ring buffer at `portal_bundle.js:4285` — once we have
  log evidence no escapes remain, the front-end safety net can be
  removed.
* Per-emit structured logging:
  * **PASSING** emit → `logger.info` with `agent_id[:8]`, turn_id,
    content md5[:8], length, ring depth.
  * **SUPPRESSED** emit → `logger.warning` with same fields plus
    seconds-since-first-occurrence.

**Still open**:

1. **Identify the actual emit path** that produces the duplicate. The
   3 hypotheses (watchdog re-entry, streaming retry, flush_action_
   buffer re-emit) all live in `app/agent.py` / `app/agent_execution.py`.
   Reproduce on the live server with the new logs:
   `tail -f <server.log> | grep "agent .* turn .* SUPPRESSED"` then
   chat with an agent that does multi-tool work. Log lines tell you
   exactly which path is firing.
2. **Remove the front-end ring-buffer dedup** at
   `portal_bundle.js:4285` once (1) is fixed and proven via the logs.
3. **Add a regression test** that simulates the identified path and
   asserts exactly one assistant emit per turn.

**Acceptance**: a chat with an 8-step agent task produces exactly one
final assistant bubble per turn. Front-end dedup helpers can be
deleted without regressing.

---

### ✅ [C] 平台层强制 QA Gate hook — DONE 2026-05-01 (commit `1cd8ddb`)

**User priority**: 必须做 (P0)
**Risk**: MEDIUM
**Status**: **Done.** Two hook points wired; third deferred (needs
its own design pass).

**What landed (commit `1cd8ddb`, ~190 LoC across 3 files)**:

* New module `app/qa_gate.py` — central validators returning
  `GateResult(ok, reason)`. `validate_email_args` for MCP send_email
  calls; `validate_file_write` for fs writes (binary-extension block,
  empty/placeholder markdown, drawio with no shapes).
* `app/tools_split/fs.py:_tool_write_file` — calls the gate; failed
  validation surfaces as a tool error so the agent retries.
* `app/mcp/dispatcher.py:NodeMCPDispatcher.dispatch` — calls the gate
  for any `tool_name in EMAIL_TOOL_NAMES`. New `ERR_QA_GATE_BLOCKED`
  error_kind so callers can recognize gate refusals distinctly from
  network or subprocess errors.

**What's deferred (HANDOFF [C] item 3)**:

* "Intent detection in agent 'task done' messages" — needs LLM-side
  classification. Separate design pass — current approach (the
  SKILL.md gate runs from the agent itself) covers the common cases
  for now.

**Where to extend**: `_NODE_EXECUTORS`-style dispatch isn't used here
yet because the validator surface is small (2 functions). When a
third file type / third tool needs a gate, factor a registry.

---

### ✅ [D] 画布执行引擎 — DONE 2026-05-02 (commit `7f3ea49`)

**User priority**: 必须做 (P0)
**Risk**: HIGH (completely new subsystem)
**Status**: **MVP done** (~580 LoC engine + 5 API endpoints + hub
init wire). Smaller than the ~2000 LoC estimate because the SVG
front-end work split into [E] and the agent/tool execution paths
turned out to be one-liners against existing APIs (chat_async,
skill_registry.invoke).

**What landed (commit `7f3ea49`)**:

* `app/canvas_executor.py` (new) — `WorkflowRun` dataclass,
  `RunStore` (per-run JSON state + append-only `<run_id>.events.jsonl`),
  `WorkflowEngine` (topological driver, single-threaded per run on a
  daemon thread).
* Per-type node executors: `_exec_start`, `_exec_end`, `_exec_agent`
  (calls `agent.chat_async` + polls for terminal status with timeout),
  `_exec_tool` (calls `hub.skill_registry.invoke`).
* Variable substitution `{{node_id.key}}` (folded in from [H] —
  recurses into dict/list values, missing vars raise; no silent
  empty-string substitution).
* 5 new endpoints under `/api/portal/canvas-workflows/{wf_id}/`:
  `POST /runs` · `GET /runs` · `GET /runs/{run_id}` · `GET
  /runs/{run_id}/events` (SSE stream).
* Hub wires `self.canvas_executor` from `<data_dir>/canvas_runs/`.

**Failure semantics**:

* Node failure → mark FAILED, downstream gets SKIPPED, run finishes
  as FAILED.
* Pre-flight `validate_for_execution` re-runs at trigger time so a
  workflow that turned invalid after marking ready (e.g., referenced
  agent deleted) fails closed.

**What's NOT done (deferred per MVP boundary)**:

* `decision` and `parallel` node types — currently raise "not
  supported in MVP" so the workflow author knows what to remove.
  Architecture is ready: just register a new entry in
  `_NODE_EXECUTORS`. For `decision`, you'll need to pick an
  expression-eval strategy (safe-eval against the vars dict, or a
  declarative comparator schema).
* No retries / circuit breakers / human-in-the-loop pauses.

**Acceptance** (original): 5-node linear workflow runs to completion,
state visible via API. **Met** — verified end-to-end with synthetic
fakes; engine emits 13 events (run_created, run_started, 5 ×
node_started, 5 × node_succeeded, run_succeeded) for the test.

---

### ✅ [E] 运行时拓扑高亮 — DONE 2026-05-02 (commit `8d20144`)

**User priority**: 必须做 (P0)
**Status**: **Done.** ~135 LoC inline in `portal_bundle.js`'s canvas
section.

**What landed**:

* `_canvasEnsureRunStyles` injects CSS keyframes (pulse, fail-flash)
  + `.cv-node-{state}` classes on first invocation.
* `_canvasApplyRunState(stateMap)` toggles per-node classes via
  direct DOM mutation — no full SVG redraw, so user's drag
  position / selection / pending edge are preserved through the run.
* `_canvasResetRunState` strips state classes when starting a fresh
  run so previous run's colors don't bleed in.
* `_canvasStopRunStream` closes any in-flight EventSource — prevents
  leaked connections when navigating away mid-run.
* `window._canvasStartRun` — full flow: POST /runs, open EventSource,
  dispatch state per event type, toast on terminal events.
* "▶ 运行" button added to the editor toolbar, visible only when
  `executable_status == "ready"`.

Color palette: pending = default grey; running = #f59e0b + pulse;
succeeded = #16a34a; failed = #dc2626 + 2-cycle flash; skipped =
#94a3b8 dashed.

**Acceptance** (original): open a running workflow, watch nodes
light up. **Met** — verified live in preview at :9091. The
running-state visual couldn't be caught by a start→end test (too
fast); will exercise on real runs with agent nodes.

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

### ✅ [H] 节点间变量 `{{var_name}}` 系统 — DONE 2026-05-02 (commits `7f3ea49` + `58f2fd3`)

**User priority**: 强烈建议 (P1)
**Status**: **Done.** Engine substitution folded into [D] commit
`7f3ea49`; UI affordance in [H]'s own commit `58f2fd3`.

**Engine side (in `7f3ea49`)**:

* `_substitute_vars` recurses into dict + list values, replaces
  `{{node_id.key}}` from `run.vars`. Missing vars raise (no silent
  empty-string substitution).
* Each node executor's return dict's keys become variables under
  `{node_id.{key}}` after the node completes:
    * agent → `output`, `task_id`, `duration_s`
    * tool  → `output` + any keys the skill returns
    * start → `started_at` · end → `finished_at`

**UI side (in `58f2fd3`, ~145 LoC in portal_bundle.js)**:

* "可用变量" panel in the right config sidebar — one row per upstream
  node with click-to-copy chips for each known output key. Chips
  render the literal `{{node_id.key}}`.
* Live linting on every keystroke: scans `{{...}}` patterns in
  config inputs, surfaces 2 distinct messages — "节点 id ... 不存在"
  vs "该节点存在但不是 ... 的上游" (typed correctly but not
  reachable from this node).
* `_canvasUpstreamNodes(targetId)` — transitive predecessor walk via
  reverse adjacency. Only upstream nodes are legal sources because
  only they have produced outputs by the time `targetId` runs.

**Acceptance** (original): a workflow `start → drawio_agent →
pptx_agent → end` where pptx_agent's prompt references
`{{drawio_agent.png_path}}` runs end-to-end. **Met** for the
substitution path — verified with synthetic 5-node test in [D]
commit (n3.output contains the value substituted from n2.value).
For real drawio→pptx, the tool node's custom `png_path` key isn't
in the auto-list (skill-specific), but the agent can still type
`{{n_drawio.png_path}}` manually — engine substitutes and warns
clearly if missing at run time.

---

## 4. Recommended next-session opening sequence

```
1. read HANDOFF.md (this file)
2. git status && git log -10 --oneline
3. user decides whether to push the 8 unpushed commits
4. user picks one of the remaining open work items below
```

**Remaining open work** (in dependency / risk order):

```
[B] root cause     ← live-reproduce on running server with the
                     new logs (commit eae3c2a), find the offending
                     emit path, fix at source, then remove the
                     front-end ring buffer.
[C] hook 3         ← intent-detection in agent "task done" messages
                     (the only HANDOFF [C] sub-item not landed —
                     needs LLM-side classification, separate design).
[D] decision/parallel ← drop-in additions to _NODE_EXECUTORS in
                     canvas_executor.py. Decision needs a safe
                     expression-eval strategy first.
[F] refresh-cache endpoint  ← optional escape hatch; only add if
                     mtime-based invalidation proves insufficient
                     in practice.
```

**Estimated scope of remaining work**: ~600–1000 LoC + a regression
test for [B]. 2-3 sessions if all are tackled.

---

## 5. Quick reference

### File map of the work in §3

```
app/agent.py
  ├─ _build_granted_skills_roster() — done (df6aa1f)
  ├─ _emit sliding-window dedup    — done [B] (eae3c2a)
  └─ _compute_static_prompt_hash    — done [F] (0ece8a0)

app/canvas_workflows.py             — done (993eba7 + 3430164)
app/api/routers/canvas.py           — done [D] runs + events endpoints (7f3ea49)

app/canvas_executor.py              — done [D] (7f3ea49)
<data_dir>/canvas_runs/<run_id>.json + .events.jsonl — runtime artifacts

app/qa_gate.py                      — done [C] (1cd8ddb) — central validators
app/tools_split/fs.py               — done [C] hook (1cd8ddb)
app/mcp/dispatcher.py               — done [C] hook + ERR_QA_GATE_BLOCKED (1cd8ddb)

app/skills/builtin/send_email/SKILL.md       — done [G] (2b41389)
app/skills/builtin/take_screenshot/SKILL.md  — done [G] (2b41389)
(jimeng_video/summarize-pro skipped — see [G] notes)

app/server/static/js/portal_bundle.js
  ├─ _canvasState / canvas editor — done
  ├─ chat ring-buffer dedup       — STILL IN PLACE (remove after [B] root identified)
  ├─ _canvasApplyRunState()       — done [E] (8d20144)
  └─ {{var_name}} hint panel + lint — done [H] (58f2fd3)

app/hub/_core.py                    — canvas_executor init wired in (7f3ea49)
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

* User's server PID **36509** still on `:9090` (started 2026-05-01
  09:50). It does **NOT** have any of this session's changes —
  needs a restart, OR new chats will pick up the [F] mtime detection
  and [B] dedup naturally on next agent prompt rebuild. The [C]
  platform gate, [D] executor, [E] highlighting, [H] hints all
  require a server restart.
* Preview server on `:9091` was used during this session for
  [E]/[H] verification; it should be stopped if still running:
  `mcp__Claude_Preview__preview_stop` with serverId from the session.
* 8 commits unpushed on `main`. User explicitly asked NOT to push;
  push when ready.
* Latest visible chat for agent `a16c2710acb6` (小刚) showed duplicate
  rendering of "任务已完成,流程图的源文件和预览图均已就绪..." × 4 —
  mitigated by the front-end ring buffer + now also by the new
  backend sliding-window dedup (eae3c2a). Root identification still
  pending — see [B] section.
* GPU cluster topology PPT in 小刚's workspace has known overlap
  issues that the QA gate flagged but the agent claimed "0 issue".
  Does NOT need to be fixed in code — it's a demo artifact.

---

*End of handoff. Next session: read this file, decide whether to
push the 8 commits, then pick one of the remaining items from §4 if
you want to keep going.*
