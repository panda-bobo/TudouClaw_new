# Parallel Execution + SystemSettings — Design Spec

**Date:** 2026-05-02
**Author:** brainstormed with @pangalano1983-dev
**Status:** Approved (pending review of this spec)
**Skill:** superpowers/brainstorming

---

## Goal

Make canvas workflows and agent delegations actually parallel where appropriate. Today both are sequential — `_drive_loop` picks one ready node at a time, and the existing `delegate(...)` call is single-child synchronous. Workflows with three independent fan-out branches don't go any faster than three serial runs.

**Two execution surfaces, one supporting infrastructure:**

- **Mode A — canvas-level implicit parallel.** When a DAG branches, the engine runs the branches on a thread pool. No special node type required.
- **Mode C — agent-level `delegate_parallel`.** New tool for parent agents that need to fan out at runtime (e.g., "write the backend, frontend, and tests in parallel"). Spawns up to N children concurrently, returns aggregated results.
- **`SystemSettingsStore`.** New small JSON-backed store + Settings UI tab. Replaces hardcoded knobs (and the original env-var proposal) with admin-editable runtime config. Mode A and Mode C both read their concurrency caps from here.

## Background — what we're keeping vs replacing

### Keeping
- Existing canvas executor structure (`_drive_loop`, `_pick_ready`, `_execute_node`). Only the scheduler shape changes.
- Existing `Agent.delegate(task, child_agent=None, child_role=None)` (single child, synchronous) — `delegate_parallel` is **additive**, not a refactor.
- Existing `_max_delegate_depth` (default 3). `delegate_parallel`'s children each consume one depth level.
- Existing per-node subdir + `_meta.json` machinery (just landed in `2264eec`). Mode C's children get sub-subdirs underneath the parent agent's node subdir; everything still routes through `canvas_artifacts`.
- Existing `BrandingStore` pattern as the reference design for `SystemSettingsStore` — same atomic-write, same module singleton, same defaults-fallback discipline.

### Replacing / new
- `_drive_loop`: single-node-per-tick → `pick_all_ready` + `ThreadPoolExecutor`.
- `_exec_parallel` no-op marker stays valid (the explicit-parallel-node case still works because its downstream branches all-ready-at-once gets parallelized by the new scheduler), but the comment that says "true concurrency deferred" goes away.
- `parallel` validator: rejects DAGs where the same `agent_id` is reachable in two parallel-runnable nodes.
- New `delegate_parallel` tool registered alongside existing `delegate`.

## Architecture

### `SystemSettingsStore` (foundation)

```
~/.tudou_claw/system_settings.json
{
  "canvas": {
    "max_parallel_nodes": 6
  },
  "delegate": {
    "max_parallel_children": 6
  }
  // Future-extensible: rag.default_top_k, agent.default_timeout, etc.
}
```

**API:**
- `SystemSettingsStore.get(path: str, default: Any = None)` — dotted path lookup, e.g. `store.get("canvas.max_parallel_nodes", 6)`. Missing keys at any level return `default`.
- `SystemSettingsStore.set(path: str, value: Any)` — dotted path write. Atomic file replace.
- `SystemSettingsStore.update(patch: dict)` — deep-merge patch into current state.
- `SystemSettingsStore.all()` — return whole dict (for the Settings UI).

**Atomic writes:** tmp-write + `os.replace` — same as `BrandingStore`.

**Defaults table (lives in code, not file):**
| Path | Default | Notes |
|---|---|---|
| `canvas.max_parallel_nodes` | `6` | Hard cap to prevent LLM-RPM blowup |
| `delegate.max_parallel_children` | `6` | Same |

**Reads are unbounded-frequency** — `_drive_loop` reads `canvas.max_parallel_nodes` every time it spins up a thread pool. Cheap (in-memory dict + single os.stat); no caching layer needed.

**HTTP API:**
- `GET /api/portal/system-settings` → `{settings: {...}, defaults: {...}}` (lets the UI grey out "Reset" button when current = default)
- `PATCH /api/portal/system-settings` body `{path: "canvas.max_parallel_nodes", value: 4}` (single-key patch). Returns updated store.

Both endpoints require admin role. Validators: `max_parallel_nodes` must be int 1..32; `max_parallel_children` same. Out-of-range → 400.

**Settings UI tab:**

```
Settings → 系统配置（tab — sits next to "品牌" / "通知" / etc.)
──────────────────────────────────────────
  画布编排
    Max parallel nodes per run    [ 6 ▼ ]  ← <select> dropdown, options 1..32
                                            (默认 6 — 每次跑同时最多几个节点)

  Agent 委派
    Max parallel children per call [ 6 ▼ ]  ← <select> dropdown, options 1..32
                                            (默认 6 — 父 agent 一次最多并发几个子)

  [ Reset to defaults ]      [ 保存 ]
```

**Both fields are `<select>` dropdowns, NOT number `<input>`** — per user feedback 2026-05-02 ("只能选择不能输入"). Prevents typo'ing "60" or "1000" and silently DoS-ing your own LLM provider. The dropdown enumerates 1..32 as options; if a future use case needs higher, we expand the enum and re-release.

The "Reset to defaults" button is enabled iff at least one field differs from default.

---

### Mode A — Canvas-level implicit parallel

**Trigger:** purely topological. When `_pick_all_ready` returns a list with N>0 entries, all N execute concurrently.

**Scheduler:**

```python
def _drive_loop(self, run, workflow):
    max_workers = system_settings.get("canvas.max_parallel_nodes", 6)
    while True:
        ready = self._pick_all_ready(run, ...)   # NEW: returns list, not str
        if not ready:
            # done or stalled — same logic as today
            ...
            return
        # Cancel flag for fail-fast
        cancel_event = threading.Event()
        with ThreadPoolExecutor(max_workers=max_workers) as exe:
            futures = {
                exe.submit(self._execute_node_with_cancel,
                           run, nodes_by_id[nid], edges, cancel_event): nid
                for nid in ready
            }
            for f in as_completed(futures):
                nid = futures[f]
                try:
                    f.result()
                except Exception:
                    # Node already marked FAILED inside _execute_node;
                    # signal others to bail.
                    cancel_event.set()
        # All futures done (success or aborted); next iteration of while.
```

**Cancellation:** `_execute_node_with_cancel` periodically checks `cancel_event.is_set()` during its agent-poll loop. If set, calls `task.abort()` on the chat task and marks the node `ABORTED`. New node state value `ABORTED` joins the existing `TERMINAL_NODE_STATES`. Cascade-skip already exists for `FAILED|SKIPPED`; we extend it to include `ABORTED` so downstream gets SKIPPED cleanly.

**Two-layer same-agent prevention** (per user feedback 2026-05-02):

**Layer 1 — UX (proactive, in the canvas editor):** when the user opens the agent picker dropdown for a node, the picker EXCLUDES any agent_id that is already bound to a parallel-reachable sibling node. The user is never offered a conflicting choice in the first place. The dropdown shows a footer line: "已隐藏 N 个 agent（在并行分支已使用）" so they know why their preferred agent isn't there. UI affordance lives in `portal_bundle.js`'s agent-picker rendering for canvas nodes.

**Layer 2 — Validator (defense in depth):** `validate_for_execution` still does the structural check, in case a wf was edited via raw JSON / API:

```python
# Reject same-agent in parallel-reachable nodes
agent_node_map = {n["id"]: n["config"].get("agent_id")
                  for n in nodes if n["type"] == "agent"}
for nid_a, agent_a in agent_node_map.items():
    for nid_b, agent_b in agent_node_map.items():
        if nid_a >= nid_b or not agent_a or agent_a != agent_b:
            continue
        if _are_parallel_reachable(nid_a, nid_b, edges):
            issues.append(
                f"agent {agent_a} appears in nodes {nid_a} and {nid_b} "
                f"that can run in parallel — same agent can't be on "
                f"two parallel branches (chat_async serializes per-agent)"
            )
```

`_are_parallel_reachable` returns True iff neither is an ancestor of the other (there's no path from one to the other in the DAG → they could be ready simultaneously).

**Failure semantics:** **fail-fast → whole run ABORTED.** When any node fails:
1. That node's state = `FAILED`.
2. `cancel_event.set()` — sibling parallel nodes call `task.abort()` and exit as `ABORTED`.
3. **Run state = `ABORTED`** (NOT `FAILED`). Per user feedback 2026-05-02: "兄弟节点 Abort，整个任务都受影响。直接任务 abort" — partial completion of unrelated downstream branches has no business value when the workflow contract has been broken.
4. Cascade-skip propagates: any not-yet-started downstream nodes go to `SKIPPED`.
5. Existing retry feature (commit `7a8438f`) handles re-running just the failed node — when retried, the run lifts back to `RUNNING` and only re-executes failed/skipped/aborted nodes (`resume` mode) per the existing engine.

---

### Mode C — Agent-level `delegate_parallel`

**New tool registered alongside existing `delegate`:**

```python
@tool("delegate_parallel")
def delegate_parallel(self, tasks: list[dict]) -> list[dict]:
    """Spawn up to N child agents concurrently, return aggregated results.

    Args:
        tasks: list of {task: str, child_role: str, hint_subdir?: str}.
               max items = system_settings("delegate.max_parallel_children", 6).

    Returns:
        list aligned with input — each entry:
            {status: "succeeded" | "failed" | "aborted",
             output: str (LLM final reply or error message),
             child_agent_id: str,
             working_subdir: str}

    Children inherit parent.working_dir + sandbox + hub. Each child gets
    its own sub-subdir (default "child_{idx}_{role_slug}", or
    `hint_subdir` if provided). Depth check applies — each child consumes
    one level of _max_delegate_depth.
    """
```

**Implementation outline:**

```python
def delegate_parallel(self, tasks: list[dict]) -> list[dict]:
    max_children = system_settings.get("delegate.max_parallel_children", 6)
    if len(tasks) > max_children:
        raise ValueError(f"too many tasks: {len(tasks)} > max {max_children}")

    cancel_event = threading.Event()
    parent_wd = Path(self.working_dir)
    results = [None] * len(tasks)

    def run_one(idx, t):
        if cancel_event.is_set():
            return idx, {"status": "aborted", ...}
        slug = t.get("hint_subdir") or f"child_{idx}_{slugify(t['child_role'])}"
        child_wd = parent_wd / slug
        child_wd.mkdir(parents=True, exist_ok=True)
        try:
            # Reuse existing delegate machinery, just pin working_dir
            child_result = self.delegate(t["task"], child_role=t["child_role"], _override_working_dir=str(child_wd))
            return idx, {"status": "succeeded", "output": child_result, ...}
        except Exception as e:
            cancel_event.set()
            return idx, {"status": "failed", "error": str(e), ...}

    with ThreadPoolExecutor(max_workers=max_children) as exe:
        futures = [exe.submit(run_one, i, t) for i, t in enumerate(tasks)]
        for f in as_completed(futures):
            idx, result = f.result()
            results[idx] = result
    return results
```

**Existing single-child `delegate` is untouched.** `delegate_parallel` does NOT call `delegate` recursively — it shares lower-level child-spawn helpers but orchestrates concurrency itself. (We don't want N nested `delegate` calls each acquiring its own lock, etc.)

**Failure semantics:** same fail-fast pattern. First child to fail sets `cancel_event`. Other children currently running their LLM loop check the event each poll iteration and abort. Aborted children show `status: aborted` in the returned list. Parent agent sees the failure and decides whether to retry, escalate, or proceed with partial results.

**Sandboxing:** child sandbox already inherits parent's `allowed_dirs` (canvas_executor pinned shared/ to whole tree). `delegate_parallel` doesn't change that — children write under parent's working_dir which is already permitted.

---

### Failure-state matrix (cross-cutting)

| Trigger | Node state | Run state |
|---|---|---|
| Node fails its own logic | FAILED | **ABORTED** (whole run aborts on first failure per user 2026-05-02) |
| Sibling already failed → this one aborted by cancel_event | ABORTED (new) | ABORTED |
| LLM-COMPLETED + EMPTY_DELIVERABLE check fails | FAILED | ABORTED |
| Cascade-skip from upstream FAILED/ABORTED/SKIPPED | SKIPPED | ABORTED |
| User clicks "abort run" button (existing manual abort) | ABORTED | ABORTED |
| All nodes SUCCEEDED | (n/a) | SUCCEEDED |

Note: the previous spec draft proposed run state = FAILED on first node failure; user feedback prefers run state = ABORTED ("the workflow's contract is broken — there's no value in calling it 'partially failed'"). Existing retry-from-node feature works the same way regardless of whether the run was ABORTED or FAILED.

---

## Out of scope (explicitly NOT in this spec)

- **Same agent in parallel cloning.** Mode A validator rejects same-agent-in-parallel; we don't auto-clone agent instances. If needed, future spec can add explicit clone-or-share toggle.
- **Distributed execution.** Threads, not processes; thread pool, not job queue. Single Python process. Cross-host parallel is a different problem.
- **Other system settings.** `SystemSettingsStore` is built generic on purpose, but only the two parallel-related knobs are wired in this round. RAG defaults, agent timeouts, etc., land later as their own opts.
- **Resource-aware throttling.** Max workers is a flat int. No "this LLM provider is at 80% RPM, slow down" feedback loop.
- **GUI on the canvas showing live thread count.** Status pills already update via SSE; no new UI affordance for "running on thread N of M".

---

## Backward compatibility

| Concern | Status |
|---|---|
| Existing canvas workflows (linear DAGs) | Unchanged — `_pick_all_ready` returns list of size 1 → ThreadPoolExecutor with 1 future = same as today |
| Existing fan-out DAGs with multiple ready nodes | **Behavior changes** — they now run concurrently. Was always sequential before; users may have implicit dependencies on serial order. Spec assumes "if you drew them as parallel branches, you wanted parallel"; document loudly in release notes |
| Existing `parallel` node type | Unchanged. Its docstring's "deferred concurrency" caveat goes away because the engine itself parallelizes |
| Existing `delegate(...)` calls | Unchanged. `delegate_parallel` is additive |
| Validator rejection of same-agent-in-parallel | New rejection — existing wfs that violated this never could have produced correct concurrent results anyway, so the rejection surfaces a latent bug. Migration: validator runs at "mark ready" time → they stay in `draft` until fixed |
| Old `executable_status: ready` workflows already saved | They've already passed the OLD validator. New validator runs only on the next "mark ready" transition or on explicit re-validate. No surprise rejection at next run |
| Env var TUDOU_CANVAS_MAX_PARALLEL etc. | **Never existed in production** — original spec proposed them but per user feedback they're replaced with SystemSettings before implementation. No back-compat burden |

---

## Implementation Status

Nothing started. Everything below pending plan + execution.

- [ ] `SystemSettingsStore` module + tests
- [ ] HTTP API endpoints
- [ ] Portal Settings UI tab (`<select>` dropdowns, not number inputs)
- [ ] `_pick_all_ready` (canvas executor)
- [ ] `_drive_loop` thread-pool refactor (run state = ABORTED on first failure)
- [ ] `_execute_node_with_cancel` + `ABORTED` node state
- [ ] Cascade-skip extension to include `ABORTED`
- [ ] Validator: `same_agent_parallel_reachable` check (defense in depth)
- [ ] **Canvas editor agent-picker UX**: exclude already-used-in-parallel-branch agents (Layer 1 prevention, user-facing)
- [ ] `delegate_parallel` tool implementation
- [ ] Tool registration / agent grant integration
- [ ] Release notes / docs update

---

## Self-Review

**Spec coverage of brainstormed Q&A:**

| Q | Decision | Where in spec |
|---|---|---|
| Q1: implicit vs explicit parallel | a1 implicit (DAG topology drives concurrency) | "Mode A — Trigger" |
| Q2: concurrency cap | per-execution-unit max=6 (default), runtime-configurable, range 1..32 dropdown | "SystemSettingsStore — Defaults table" + "Settings UI tab" |
| Q3: same agent in parallel branches | two-layer prevention: editor picker excludes + validator rejects | "Mode A — Two-layer same-agent prevention" |
| Q4: one branch fails | fail-fast → whole run ABORTED (not FAILED) | "Mode A — Failure semantics" + "Failure-state matrix" |
| Q5 (post-hoc): env var vs settings | SystemSettings + UI tab with `<select>` (not number input) | "SystemSettingsStore" section + "Settings UI tab" |

**Placeholder scan:** no TBDs, every code block has a concrete enough sketch (signature + 5-15 line body) to drive a plan task. Real implementation may differ in details (exact `as_completed` vs `wait` choice, exact slug regex), but no requirement is left as "TBD" or "implement later".

**Type consistency:** `cancel_event` is a `threading.Event` everywhere. `max_workers` (canvas) and `max_children` (delegate) named distinctly so they're independently configurable. `tasks: list[dict]` in `delegate_parallel` consistent with `results: list[dict]` return shape.

**Scope check:** three concerns (SystemSettings infra + Mode A + Mode C). All share concurrency primitives (ThreadPoolExecutor + cancel_event), so bundling them in one spec/plan is right — splitting would force triplicating the "what is fail-fast cancellation" discussion. Each concern is a discrete chunk in the plan.

**Risks captured:**
- Existing fan-out DAGs change behavior silently → loud release note
- Same-agent rejection may surface latent bugs in saved wfs → user-visible at next "mark ready"
- Threading + per-agent `chat_async` queue → already serializes same-agent; the validator catches the misuse

---

## Handoff

Once user approves this spec, transition to **superpowers/writing-plans** to produce a checklist-style implementation plan in `docs/superpowers/plans/2026-05-02-parallel-execution-implementation.md` covering:

1. SystemSettingsStore module + unit tests
2. HTTP API endpoints + integration tests
3. Portal Settings UI tab + live verification
4. `_pick_all_ready` + canvas executor refactor + tests
5. `_execute_node_with_cancel` + ABORTED state propagation
6. Cascade-skip update for ABORTED
7. Same-agent validator
8. `delegate_parallel` tool + tests
9. Documentation update
10. End-to-end verification on user's existing wf-555814df2864 (now parallel-able if we add a third branch)
