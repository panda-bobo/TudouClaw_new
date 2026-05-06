# Session Handoff — 2026-05-02

> **Purpose:** Single document the next session can read to pick up right
> where the previous session stopped, without trawling through commit
> messages or chat transcripts.
>
> **How to use it:** Next session, start with `read HANDOFF.md` then
> tell me which item from §3 to work on (most are now done — see §1).

---

## 0. Update — 2026-05-05 (post-handoff state)

The ~30 commits between `7aebcfb` (2026-05-04 stitch-theme handoff)
and `e7ca242` (today, **HEAD**) are not in §1–§6 below — the original
handoff snapshot froze on 2026-05-02. Two distinct waves landed:

**Wave 1 — Stitch tech-theme rollout (2026-05-04, ~25 commits)**
Visual overhaul of every page in tech mode: canvas glass cards, hero
sections with "kicker" labels, dashed "Add Workflow" cards, stitch_18
channel rows, stitch_19 SVG nodes, MCP catalog bento, role-preset V2.
Touchpoints: `app/server/static/css/theme-tech.css` and many sections
of `portal_bundle.js`. README now has fresh screenshots reflecting
this style — see `assets/screenshots/`.

**Wave 2 — RAG / memory / project workflow / sandbox (2026-05-05, `e7ca242`)**
A single batch commit (18 files, +4840 / −1466) covering:

| Area | Change |
|------|--------|
| RAG quality | HCS-inspired intent classification (`statistical` / `detail` / `normal`); markdown table-atomic chunking with heading prepend; "UNINDEXED N" chip on KB cards |
| Memory provenance | `project_id` / `task_id` / `source_message_id` / `scope` on every fact; vector-search round-trip preserves scope via ChromaDB metadata |
| Memory scope filter | Recall filtered by `global` ∪ `agent:{id}` ∪ `project:{pid}` ∪ `task:{tid}`; identity categories (contact/preference) always `scope='global'` |
| Caller context | `_caller_agent_id` always-injected for ALL tool dispatches (not just an allowlist) — fixes `memory_recall` requiring caller and the `read_file` sandbox jail-escape complaint |
| Stage-aware files | `StepTemplate.input_files` / `output_files`; `ProjectTask` carries the same; `_auto_progress_next_step` builds upstream files block; `system_prompt.py` rule forbids `glob_files` when pinned files block is present |
| Workflow approval | `_find_ready_dag_tasks` skips `metadata.pending_approval=True`; `bind_workflow` preserves `require_approval` (was previously dropped on rebuild) |
| Project ops | Abort cascades all `project:{id}:task:{tid}` keys; queue+merge for project chat; reopen step API + UI; deliverable file delete removes from disk |
| Persistence | Single-source-of-truth load (no JSON-fallback resurrection); dual-write kept consistent (snapshot replace + JSON full rewrite) |

Root cause patterns worth remembering:
- **"Approve button no-op"** — backend returned `approval_id`, frontend read `a.id`. Always alias `a.approval_id || a.id`.
- **"Domain picker input destroyed on each keystroke"** — `oninput` rebuilt the whole modal. Extracted `buildCardsHtml` + `_spOnSearchInput` to update only `#sp-cards`.
- **"Agent delete resurrected"** — JSON fallback re-loaded a deleted snapshot. Fix: strict single-source load.

**Branch state**: `main` is current; `e7ca242` is HEAD. Working tree
clean. Push state: see `git status -uno` — the original handoff said
"NOT pushed per user direction" but multiple pushes have happened
since; verify before assuming.

§1–§6 below are preserved verbatim for historical reference.

---

## 1. Current state

**Branch**: `main` (14 commits ahead of `origin/main` — **NOT pushed**
per user direction; push when ready).
**Working tree**: clean.

**This session (2026-05-01 → 02) closed out all 8 original open
items.** Most are fully done; [B] is "tested and instrumented but
needs one round of live observation before removing the front-end
ring buffer". New commits on `main` (oldest → newest):

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
| `7d86a28` | docs | docs(handoff): close out [C][B][D][E][H] from 2026-05-02 session |
| `b295294` | [B]+ | refactor(agent): extract _emit dedup to testable EmitDedupState |
| `4f5c46c` | [D]+ | feat(canvas): decision + parallel node types |
| `d05fd67` | [C]+ | feat(qa-gate): completion-claim detection hook |
| `6f6fb1a` | [F]+ | feat(agent): manual POST /agent/{id}/refresh-cache endpoint |

**Status of the 8 original open items** (details in §3):

| ID | Item | Status |
|----|------|--------|
| ✅ | [G] Skill SKILL.md 补齐 | DONE |
| ✅ | [F] KV cache 刷新机制 | DONE (mtime + manual refresh endpoint) |
| ✅ | [C] 平台层强制 QA Gate hook | DONE (3/3 hook points; intent-detection is warning-only MVP) |
| 🟡 | [B] Agent 重复消息 Bug | DONE (testable, 23 tests pass, exact symptom reproduced); needs one round of live observation before removing front-end ring buffer |
| ✅ | [D] 画布执行引擎 | DONE (all 6 node types: start/end/agent/tool/decision/parallel) |
| ✅ | [E] 运行时拓扑高亮 | DONE |
| ✅ | [H] 节点间变量 | DONE (engine + UI hints + lint) |

**Server note**: changes require a restart (or — for [F] mtime
detection, [B] dedup, [C] gates — will be picked up on the agent's
next chat turn naturally). Preview server at :9091 was stopped at
end of session.

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

### 🟡 [B] Agent 重复消息 Bug — DONE 2026-05-02 (commits `eae3c2a` + `b295294`)

**User priority**: 必须做 (P0)
**Risk**: HIGH — touches every agent's chat reply path
**Status**: **Backend dedup is testable, instrumented, and proven
against the exact reported symptom.** One follow-up remains: live
observation, then remove the front-end ring buffer.

**What landed**:

* `eae3c2a` — In `agent.py` `_emit`, replaced the single-slot
  `_last_emitted_text_ref` with a sliding 5-entry ring (60s TTL,
  normalized whitespace, exact-or-mutual-prefix match). Mirrors the
  front-end ring buffer at `portal_bundle.js:4285`. Plus per-emit
  structured logging:
  * **PASSING** emit → `logger.info` with `agent_id[:8]`, turn_id,
    content md5[:8], length.
  * **SUPPRESSED** emit → `logger.warning` with same fields plus
    seconds-since-first-occurrence.
* `b295294` — Extracted dedup logic into `app/_emit_dedup.py`
  (`EmitDedupState` class) so it's unit-testable. Added
  `tests/test_emit_dedup.py` — 23 cases (15 dedup + 8 completion-
  claim). Includes the exact 4×-bubble symptom reproduction:
  `test_4x_bubble_symptom_reproduction` asserts EXACTLY one bubble
  delivered to front-end given 4 identical emits. All pass.

**Code-path analysis** (verified by grep): all assistant `message`
events emit through `_emit` (which has the dedup). Direct `on_event`
calls bypass `_emit` only for non-message types (approval, handoff).
So the dedup is the unique chokepoint.

**One follow-up remaining** (deferred — needs live evidence):

1. Restart server, chat with an agent that does multi-tool work,
   `tail -f <server.log> | grep "SUPPRESSED"` to see if any
   suppressions fire. If yes — the warning includes the agent_id,
   turn_id, hash so you can correlate with the user's experience.
2. After 1-2 weeks of clean logs, **remove the front-end ring-buffer
   dedup** at `portal_bundle.js:4285` (~30 lines). The backend dedup
   becomes load-bearing.

**Acceptance** (original): a chat with an 8-step agent task produces
exactly one final assistant bubble per turn. **Met by tests** for the
hypothesized symptom; live verification still pending.

---

### ✅ [C] 平台层强制 QA Gate hook — DONE 2026-05-02 (commits `1cd8ddb` + `d05fd67`)

**User priority**: 必须做 (P0)
**Risk**: MEDIUM
**Status**: **Done.** All 3 HANDOFF hook points landed (one is
warning-only MVP).

**What landed**:

* `1cd8ddb` — `app/qa_gate.py` with `validate_email_args` and
  `validate_file_write`; hooks in `app/tools_split/fs.py:_tool_write_file`
  and `app/mcp/dispatcher.py:NodeMCPDispatcher.dispatch`. New
  `ERR_QA_GATE_BLOCKED` error_kind. Failed validation surfaces as a
  tool error so the agent retries.
* `d05fd67` — Hook 3: completion-claim detection. New
  `qa_gate.detect_completion_claim` (bilingual phrase set) +
  `validate_completion_claim` (cross-checks against active plan).
  Hook in `agent.py` `_emit` after dedup passes — logs a warning
  `COMPLETION-CLAIM MISMATCH — agent claimed completion but plan
  has N open step(s)` if mismatch. **Warning-only**, does NOT
  block message emit (false-positive risk on legitimate partial
  delivery is too high for hard-blocking; escalate to in-band
  correction only if logs show real misalignment in practice).

**Tests**: 8 cases for completion-claim detection in
`tests/test_emit_dedup.py` — zh + en phrase detection, negative
cases, plan-state cross-check. All pass.

**Where to extend**: when a 3rd file type / 3rd tool needs a gate,
the small `validate_*` function set in `qa_gate.py` should grow into
a dispatch registry. Current 2-function surface doesn't need it yet.

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

**Decision + Parallel landed in `4f5c46c`**:

* `_exec_decision` — evaluates `config.condition` (Python boolean
  expression) against run vars. Locals expose vars two ways:
  `vars["n1.output"]` AND flattened `n1_output`. Restricted builtins
  (True/False/None, int/str/float/bool, len/abs/min/max). Returns
  `branch="yes"` or `"no"`.
* New driver post-hook `_skip_unchosen_branches` — after a decision
  node succeeds, walks edges from it and marks targets of edges
  whose `label` doesn't match `branch` as SKIPPED. Edges with no
  label fall through (linear flow for authors who didn't label).
* `_exec_parallel` — no-op success. DAG semantics give fan-out for
  free (downstream becomes ready, driver picks them up serially).
  Implicit join via existing `_pick_ready` deps check. True
  concurrency (threads per branch) deferred — perf optimization.

**What's still NOT done**:

* No retries / circuit breakers / human-in-the-loop pauses.
* True concurrent execution of parallel branches (currently serial
  with correct DAG semantics).

**Acceptance** (original): 5-node linear workflow runs to completion,
state visible via API. **Met** — verified end-to-end with synthetic
fakes; engine emits 13 events for the test. Decision + parallel
also smoke-tested (5 cases, all pass: yes-branch, no-branch with var
ref, parallel fan-out, broken syntax, sandbox-escape attempt).

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

### ✅ [F] KV cache 刷新机制 — DONE 2026-05-02 (commits `0ece8a0` + `6f6fb1a`)

**User priority**: 强烈建议 (P1)
**Risk**: LOW
**Status**: **Done.** Core mtime-based invalidation + manual refresh
endpoint both landed. The grant/revoke explicit-invalidation
sub-item judged redundant (existing hash already pulls live registry
state).

**What landed**:

* `0ece8a0` — `_compute_static_prompt_hash` folds each granted
  skill's SKILL.md mtime_ns into the hash. Edit a SKILL.md → next
  chat turn rebuilds the cached static prompt with fresh content.
  +24 LoC in `app/agent.py`.
* `6f6fb1a` — `POST /api/portal/agent/{agent_id}/refresh-cache`.
  Force-clears `_cached_static_prompt` + `_static_prompt_hash` so
  the next chat() call rebuilds. Use cases: coarse-mtime filesystems
  (HFS+, FAT), NFS-cached stat, or force-rebuild after edits to
  inputs other than SKILL.md (system prompt, role, model, soul.md,
  project context). Auth: requires `Permission.MANAGE_AGENT`.

**What's still NOT done**:

* Explicit `_invalidate_prompt_cache()` calls on grant / revoke
  handlers — judged **redundant** in this session. The existing
  `_compute_static_prompt_hash` pulls `list_for_agent` live from the
  registry, so grant_ids change → hash flips → cache rebuilds
  automatically. The HANDOFF item was overcautious.

**Verification**: hash flips on edit and on delete (smoke test in
commit `0ece8a0`); endpoint smoke test verifies route registration +
clear-both-fields semantics + the cache-check guard at
`_build_static_system_prompt:3960`.

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
2. git status && git log -15 --oneline
3. user decides whether to push the 14 unpushed commits
4. user decides what to verify / extend below
```

**Remaining open work** (all small, mostly observation-driven):

```
[B] live observation       ← restart server, chat, watch
                             "agent X turn Y SUPPRESSED" warnings
                             from logger.warning in agent.py:_emit.
                             If clean for 1-2 weeks, remove the
                             front-end ring buffer at
                             portal_bundle.js:4285 (~30 lines).

[C] completion-claim       ← currently warning-only. After
    escalation                observing logs in production, decide
                             whether to escalate to in-band
                             correction (inject system message
                             saying "you claimed done but step X
                             still open, please verify"). Hook is
                             already at agent.py:_emit; just swap
                             logger.warning for an evt injection.

[D] true parallel          ← currently parallel branches run
    concurrency               serially with correct DAG semantics.
                             For perf, spawn threads per branch
                             when the parallel node completes.

[D] decision condition     ← if eval-against-locals proves too
    DSL                       loose for non-admin authors (none
                             today), build a declarative comparator
                             schema instead of safe-eval.

[D] retries / human-in-    ← MVP boundary; add when a real workflow
    the-loop pause             needs them.

(no other open work tracked from the original 8 items)
```

**Estimated scope**: ~100-300 LoC for the live-validation
follow-ups; 0 LoC if production says everything's fine.

---

## 5. Quick reference

### File map of the work in §3

```
app/agent.py
  ├─ _build_granted_skills_roster()  — df6aa1f
  ├─ _emit (sliding-window dedup +   — eae3c2a + d05fd67
  │   completion-claim hook)
  └─ _compute_static_prompt_hash      — 0ece8a0

app/_emit_dedup.py                    — b295294 (extracted helper)
tests/test_emit_dedup.py              — b295294 + d05fd67 (23 tests)

app/canvas_workflows.py               — 993eba7 + 3430164
app/api/routers/canvas.py             — 7f3ea49 (runs + events endpoints)
app/api/routers/agents.py             — 6f6fb1a (refresh-cache endpoint)

app/canvas_executor.py                — 7f3ea49 (engine, 4 node types)
                                      — 4f5c46c (decision + parallel)
<data_dir>/canvas_runs/<run_id>.json + .events.jsonl

app/qa_gate.py                        — 1cd8ddb (email + file_write)
                                      — d05fd67 (completion-claim)
app/tools_split/fs.py                 — 1cd8ddb (hook)
app/mcp/dispatcher.py                 — 1cd8ddb (hook + ERR_QA_GATE_BLOCKED)

app/skills/builtin/send_email/SKILL.md       — 2b41389
app/skills/builtin/take_screenshot/SKILL.md  — 2b41389

app/server/static/js/portal_bundle.js
  ├─ chat ring-buffer dedup       — STILL IN PLACE (safe to remove
  │                                  after live observation per [B])
  ├─ _canvasApplyRunState()       — 8d20144
  └─ {{var_name}} hint + lint     — 58f2fd3

app/hub/_core.py                     — 7f3ea49 (canvas_executor init)
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
  platform gate, [D] executor + decision/parallel, [E] highlighting,
  [H] hints, [F] refresh-cache endpoint all require a server restart.
* Preview server on `:9091` was used during this session for
  [E]/[H] verification; stopped at end of session.
* **14 commits unpushed on `main`.** User explicitly asked NOT to
  push; push when ready.
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
