# Canvas Deliverable Cleanup + Empty-Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the remaining items from the canvas-deliverable spec — clean up leftover variable names, add the empty-deliverable failure check, polish the frontend label, add user docs.

**Architecture:** Builds on commit `2264eec` (per-node subdir + working_dir = subdir + `_meta.json` already in place). Each task is small, has a test, and ends with a commit.

**Tech Stack:** Python 3 (FastAPI backend), vanilla JS (portal_bundle.js), Markdown for docs. No new deps.

---

## File Structure

| File | Role |
|------|------|
| `app/canvas_executor.py` | Where outputs dict is built; where empty-deliverable check goes |
| `app/canvas_workflows.py` | Validator (`validate_for_execution`) — already validates `success_when`; just confirm spec match |
| `app/server/static/js/portal_bundle.js` | Node config panel UI (label rename + help text) |
| `tests/test_canvas_deliverable.py` | New test file — all the empty-deliverable / leftover-cleanup tests |
| `docs/canvas-workflows.md` | New user-facing doc — how to write a canvas workflow with deliverables |

No file restructuring needed. All changes scoped to existing files except the two new ones.

---

## Task 1: Drop the leftover `deliverable_type` and `success_marker_file` outputs

**Files:**
- Modify: `app/canvas_executor.py:1027-1042` (the outputs dict block in `_exec_agent`)
- Test: `tests/test_canvas_deliverable.py` (new)

The spec settled on a single string `{{nid.deliverable}}` (always abs path of the node subdir). `deliverable_type` is always `"directory"` so it's useless; `success_marker_file` is a legacy name from `d2a14a6` superseded by the deliverable concept.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_canvas_deliverable.py
"""Tests for the canvas-executor deliverable variable contract.

Companion to docs/superpowers/specs/2026-05-02-canvas-deliverable-design.md.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
import pytest

from app.canvas_artifacts import ArtifactStore


def test_outputs_dict_has_deliverable_no_legacy_keys():
    """outputs returned by _exec_agent contain `deliverable` and
    `deliverable_relative` but NOT the legacy `deliverable_type` or
    `success_marker_file` keys (cleaned up after spec approval)."""
    # Imports done lazily so this test doesn't pull the whole hub
    # dependency tree at collection time.
    import app.canvas_executor as ce
    src = Path(ce.__file__).read_text(encoding="utf-8")
    # We don't run a full DAG here — the contract is what _exec_agent
    # writes into the outputs dict, and the simplest robust assertion
    # is "those tokens don't appear in source anymore". A full e2e
    # test lives in test_canvas_engine.py.
    assert '"deliverable_type"' not in src, (
        "deliverable_type leftover in canvas_executor.py — should be dropped"
    )
    assert '"success_marker_file"' not in src, (
        "success_marker_file leftover in canvas_executor.py — superseded by deliverable"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_canvas_deliverable.py::test_outputs_dict_has_deliverable_no_legacy_keys -v
```

Expected: FAIL — both string assertions fail (current source still has the legacy keys).

- [ ] **Step 3: Remove the legacy keys from the outputs dict**

In `app/canvas_executor.py`, replace the block that currently reads:

```python
        if node_dir is not None:
            if marker_file:
                deliverable_abs = str(node_dir / marker_file)
                outputs["deliverable"] = deliverable_abs
                outputs["deliverable_type"] = "file"
                outputs["deliverable_relative"] = f"{node_id}/{marker_file}"
                outputs["success_marker_file"] = marker_file   # legacy alias
            else:
                outputs["deliverable"] = str(node_dir)
                outputs["deliverable_type"] = "directory"
                outputs["deliverable_relative"] = f"{node_id}/"
```

with:

```python
        # The deliverable is ALWAYS the node's subdir (per spec
        # 2026-05-02 — file vs directory distinction is YAGNI).
        # If success_when.file_glob fired, the matched file is just
        # one item inside that subdir; downstream LLM finds it via
        # ls / read_file.
        if node_dir is not None:
            outputs["deliverable"] = str(node_dir)
            outputs["deliverable_relative"] = f"{node_id}/"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_canvas_deliverable.py::test_outputs_dict_has_deliverable_no_legacy_keys -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_canvas_deliverable.py app/canvas_executor.py
git commit -m "$(cat <<'EOM'
refactor(canvas): drop legacy deliverable_type / success_marker_file outputs

Spec finalized on "always subdir, no file vs directory distinction".
Both leftover keys were never persisted in any released workflow
(deliverable_type added today in 2264eec; success_marker_file added
today in d2a14a6) — safe to drop with no migration.

Test asserts source no longer contains the legacy strings.
EOM
)"
```

---

## Task 2: Drop the `deliverable.file_glob` legacy alias

**Files:**
- Modify: `app/canvas_executor.py` (the `deliverable_cfg` parsing block in `_exec_agent`)
- Test: `tests/test_canvas_deliverable.py` (extend)

Per the spec, `success_when.file_glob` is the canonical config; the brief detour through `deliverable.file_glob` was a misstep.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_canvas_deliverable.py`:

```python
def test_no_deliverable_filegio_alias_in_executor():
    """Spec says success_when.file_glob is the canonical config name.
    The short-lived deliverable.file_glob alias from this morning's
    misstep should be gone."""
    import app.canvas_executor as ce
    src = Path(ce.__file__).read_text(encoding="utf-8")
    # Searches for the alias parsing pattern. Specifically we want
    # NO references to a config key path like config["deliverable"]
    # — that pattern was the alias misstep.
    assert 'deliverable_cfg' not in src, (
        "deliverable_cfg parsing leftover in _exec_agent — drop it; "
        "use success_when.file_glob exclusively"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_canvas_deliverable.py::test_no_deliverable_filegio_alias_in_executor -v
```

Expected: FAIL — `deliverable_cfg` still in source.

- [ ] **Step 3: Replace the alias parsing block with simple success_when read**

In `app/canvas_executor.py`, replace:

```python
    # ── deliverable: declarative output of this agent node ──
    # Optional dict that ties three concerns together:
    #   1. WHAT this node produces (passed to downstream as
    #      {{nid.deliverable}})
    #   2. WHEN to consider the node done (declarative termination —
    #      file lands → we abort the chat + mark SUCCEEDED, even if
    #      the LLM is still narrating; solves the "finished 9s after
    #      timeout" race)
    #   3. Provenance — _meta.json captures the resolved path.
    #
    # Default (no config): deliverable = the entire node subdir.
    # Narrow shapes:
    #   deliverable.file_glob: "report_*.md"   → single file (or first
    #                                           matching new file)
    # Legacy alias accepted for back-compat:
    #   success_when.file_glob (committed in d2a14a6, never used in
    #   prod) maps to deliverable.file_glob.
    deliverable_cfg = config.get("deliverable") or {}
    if not isinstance(deliverable_cfg, dict):
        deliverable_cfg = {}
    legacy_sw = config.get("success_when") or {}
    if isinstance(legacy_sw, dict) and not deliverable_cfg.get("file_glob"):
        legacy_glob = str(legacy_sw.get("file_glob") or "").strip()
        if legacy_glob:
            deliverable_cfg = dict(deliverable_cfg)
            deliverable_cfg["file_glob"] = legacy_glob
    success_file_glob = str(deliverable_cfg.get("file_glob") or "").strip()
```

with:

```python
    # ── success_when: optional early-termination by file marker ──
    # When configured, executor polls the node's subdir for a NEW
    # file matching the glob. First match → task.abort() + node
    # SUCCEEDED, even if the LLM is still narrating. Solves the
    # "finished 9s after timeout" race seen on 2026-05-02.
    # The deliverable variable is unaffected — always points at
    # the whole subdir per the design spec.
    success_when = config.get("success_when") or {}
    if not isinstance(success_when, dict):
        success_when = {}
    success_file_glob = str(success_when.get("file_glob") or "").strip()
```

- [ ] **Step 4: Update _meta.json write site**

Two `write_node_meta` calls in `_exec_agent` reference `deliverable_cfg`. Find:

```python
            artifact_store.write_node_meta(run.id, node_id, {
                "node_id": node_id,
                "node_label": node.get("label", ""),
                "agent_id": agent_id,
                "agent_name": getattr(agent, "name", ""),
                "started_at": time.time(),
                "deliverable_cfg": deliverable_cfg,
            })
```

Replace with:

```python
            artifact_store.write_node_meta(run.id, node_id, {
                "node_id": node_id,
                "node_label": node.get("label", ""),
                "agent_id": agent_id,
                "agent_name": getattr(agent, "name", ""),
                "started_at": time.time(),
                "success_when": success_when if success_file_glob else {},
            })
```

And the second write (after artifact post-scan), find the block that uses `outputs.get("deliverable_type", "directory")` — drop that line entirely since deliverable_type was removed in Task 1:

```python
                    artifact_store.write_node_meta(run.id, node_id, {
                        "node_id": node_id,
                        "node_label": node.get("label", ""),
                        "agent_id": agent_id,
                        "agent_name": getattr(agent, "name", ""),
                        "task_id": task.id,
                        "started_at": task.created_at,
                        "finished_at": time.time(),
                        "deliverable": {
                            "abs_path": outputs.get("deliverable", str(node_dir)),
                            "rel_path": outputs.get("deliverable_relative", f"{node_id}/"),
                        },
                        "artifact_count": len(new_artifacts),
                    })
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_canvas_deliverable.py -v
```

Expected: PASS for both tests in the file.

- [ ] **Step 6: Commit**

```bash
git add tests/test_canvas_deliverable.py app/canvas_executor.py
git commit -m "refactor(canvas): drop deliverable.file_glob alias; success_when is canonical"
```

---

## Task 3: Add EMPTY_DELIVERABLE check after agent completes

**Files:**
- Modify: `app/canvas_executor.py:_exec_agent` (after artifact post-scan)
- Test: `tests/test_canvas_deliverable.py` (extend)

Per spec: if subdir is empty (excluding `_meta.json`), node FAILS with code `EMPTY_DELIVERABLE`. Existing cascade-skip handles downstream.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_canvas_deliverable.py`:

```python
def test_empty_deliverable_check_helper(tmp_path):
    """The helper that decides whether a node produced something.
    Returns False when the subdir is empty or contains only _meta.json."""
    import app.canvas_executor as ce

    # Empty dir
    empty = tmp_path / "n_empty"
    empty.mkdir()
    assert ce._has_real_deliverable(empty) is False

    # Only _meta.json — still empty
    only_meta = tmp_path / "n_only_meta"
    only_meta.mkdir()
    (only_meta / "_meta.json").write_text("{}")
    assert ce._has_real_deliverable(only_meta) is False

    # Real file
    has_file = tmp_path / "n_real"
    has_file.mkdir()
    (has_file / "report.md").write_text("hello")
    assert ce._has_real_deliverable(has_file) is True

    # Nested file
    nested = tmp_path / "n_nested"
    (nested / "app/backend").mkdir(parents=True)
    (nested / "app/backend/server.py").write_text("# code")
    assert ce._has_real_deliverable(nested) is True

    # Nonexistent dir → False (never created)
    missing = tmp_path / "n_never"
    assert ce._has_real_deliverable(missing) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_canvas_deliverable.py::test_empty_deliverable_check_helper -v
```

Expected: FAIL with `AttributeError: module 'app.canvas_executor' has no attribute '_has_real_deliverable'`.

- [ ] **Step 3: Add the helper**

In `app/canvas_executor.py`, near `_scan_for_marker_file` (around line 79):

```python
def _has_real_deliverable(node_dir: "Path | str") -> bool:
    """Return True iff the agent node's subdir contains any file
    other than _meta.json. Used by _exec_agent to fail-fast on
    EMPTY_DELIVERABLE per spec 2026-05-02 — protects against the
    silent "LLM said it wrote a file but actually didn't" failure
    mode.

    A nonexistent path returns False (caller wants the same
    "no real output" semantics).
    """
    from pathlib import Path as _P
    base = _P(str(node_dir))
    if not base.is_dir():
        return False
    try:
        for p in base.rglob("*"):
            if p.is_file() and p.name != "_meta.json":
                return True
    except OSError:
        return False
    return False
```

- [ ] **Step 4: Wire the check into `_exec_agent`**

In `_exec_agent`, find the artifact post-scan block (looks like this after Task 2):

```python
        if artifact_store is not None and node_dir is not None:
            try:
                new_artifacts = artifact_store.diff_and_register(
                    ...
```

After the post-scan + _meta.json write but BEFORE `return outputs`, add:

```python
        # EMPTY_DELIVERABLE check (spec 2026-05-02): even if the
        # LLM reported COMPLETED or success_when fired, if the agent
        # produced no real files we treat the node as FAILED. The
        # error code is grep-friendly so the UI can color it
        # specifically and the user knows to retry the agent (rather
        # than blame timeout / LLM).
        if node_dir is not None and not _has_real_deliverable(node_dir):
            raise RuntimeError(
                f"agent {agent_id} produced no deliverable in "
                f"shared/{node_id}/ (error_code: EMPTY_DELIVERABLE)"
            )

        return outputs
```

- [ ] **Step 5: Test the helper**

```bash
pytest tests/test_canvas_deliverable.py::test_empty_deliverable_check_helper -v
```

Expected: PASS.

- [ ] **Step 6: Add an integration-ish test for the executor flow**

Append to `tests/test_canvas_deliverable.py`:

```python
def test_exec_agent_raises_on_empty_subdir(tmp_path, monkeypatch):
    """Smoke: when an agent leaves its subdir empty, _exec_agent
    raises RuntimeError containing EMPTY_DELIVERABLE. We don't
    actually invoke an LLM — patch the chat path to return
    immediately with a COMPLETED task and no files written.
    """
    import time
    from unittest.mock import MagicMock
    from app import canvas_executor as ce
    from app import canvas_artifacts as ca
    from app.canvas_executor import WorkflowEngine, WorkflowRun, RunState

    # Init artifact store at tmp dir
    ca.init_store(tmp_path)
    store = ca.get_store()

    # Build a minimal Run + WorkflowEngine
    run = WorkflowRun(
        id="run-test",
        workflow_id="wf-test",
        workflow_name="t",
        state=RunState.RUNNING,
        started_at=time.time(),
    )
    engine = MagicMock(spec=WorkflowEngine)
    engine.hub = MagicMock()
    engine.artifact_store = store

    # Fake agent: chat_async returns a "COMPLETED" task instantly,
    # but writes nothing.
    fake_task = MagicMock()
    from app.chat_task import ChatTaskStatus
    fake_task.status = ChatTaskStatus.COMPLETED
    fake_task.id = "fake-task-id"
    fake_task.result = "I'm done!"
    fake_task.created_at = time.time()
    fake_task.updated_at = time.time()
    fake_task.error = None

    fake_agent = MagicMock()
    fake_agent.id = "ag-x"
    fake_agent.name = "test-agent"
    fake_agent._lock = MagicMock()
    fake_agent._lock.__enter__ = MagicMock(return_value=None)
    fake_agent._lock.__exit__ = MagicMock(return_value=None)
    fake_agent._active_context_id = "solo"
    fake_agent._messages_by_context = {}
    fake_agent._switch_context = MagicMock()
    fake_agent.chat_async = MagicMock(return_value=fake_task)
    fake_agent.working_dir = ""
    engine.hub.get_agent = MagicMock(return_value=fake_agent)

    node = {"id": "n_empty", "label": "test", "config": {
        "agent_id": "ag-x", "prompt": "do nothing", "timeout": 5,
    }}

    with pytest.raises(RuntimeError, match="EMPTY_DELIVERABLE"):
        ce._exec_agent(engine, run, node, node["config"])
```

- [ ] **Step 7: Run the integration test**

```bash
pytest tests/test_canvas_deliverable.py::test_exec_agent_raises_on_empty_subdir -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/test_canvas_deliverable.py app/canvas_executor.py
git commit -m "feat(canvas): EMPTY_DELIVERABLE check — fail node when subdir produced nothing"
```

---

## Task 4: Frontend — rename UI label + update help text

**Files:**
- Modify: `app/server/static/js/portal_bundle.js` (the agent node config panel — `_canvasRenderConfigPanel`)

Old label was "完成条件 — 文件名 (可选)". Per spec, "completion condition" was the wrong framing — it's an early-termination signal, the deliverable concept is separate.

- [ ] **Step 1: Update the label and helper text**

In `app/server/static/js/portal_bundle.js`, find the block (in `_canvasRenderConfigPanel` for agent type):

```javascript
      // success_when.file_glob — closes the agent node as soon as a
      // matching file shows up in the run's shared dir, even if the
      // LLM is still grinding. Solves the "agent finished writing
      // the file 9s after timeout" race.
      + '<div style="margin-bottom:4px"><label style="font-size:11px;color:var(--text3)">完成条件 — 文件名 (可选)</label>'
      + '<input data-cfg="success_when.file_glob" type="text" placeholder="e.g. report_*.md  或  AI热点*.md" value="' + esc(((n.config.success_when || {}).file_glob) || '') + '" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);font-size:12px"></div>'
      + '<div style="font-size:10px;color:var(--text3);line-height:1.4;margin-bottom:8px">指定后，shared 目录里出现匹配文件就立刻结束（不等 LLM 自报）。留空走标准 LLM-COMPLETED 路径。</div>';
```

Replace with:

```javascript
      // success_when.file_glob — early-termination signal. When the
      // agent writes a matching file to its node subdir, the canvas
      // aborts the LLM and marks SUCCEEDED. Independent from the
      // deliverable variable, which always = the whole subdir.
      + '<div style="margin-bottom:4px"><label style="font-size:11px;color:var(--text3)">提前结束 — 交付文件名 (可选)</label>'
      + '<input data-cfg="success_when.file_glob" type="text" placeholder="e.g. report_*.md  或  AI热点*.md" value="' + esc(((n.config.success_when || {}).file_glob) || '') + '" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);font-size:12px"></div>'
      + '<div style="font-size:10px;color:var(--text3);line-height:1.4;margin-bottom:8px">填了就：节点子目录里出现匹配文件 → 立刻 abort LLM + 节点 SUCCEEDED（不等 LLM 自报完成）。<br>留空：走标准 LLM-COMPLETED + 兜底 timeout。<br>下游用 <code>{{节点id.deliverable}}</code> 永远拿到子目录路径，不论这里填没填。</div>';
```

- [ ] **Step 2: Verify the JS still parses**

```bash
node --check app/server/static/js/portal_bundle.js
```

Expected: no output (= success).

- [ ] **Step 3: Verify in browser via preview**

Restart the running preview server (template + JS hot-reload not in use):

```bash
# (claude-preview restart sequence — done by tool, not bash)
```

Then run a sanity check via the existing preview eval:

```javascript
(async () => {
  const r = await fetch('/static/js/portal_bundle.js?_v=' + Date.now());
  const txt = await r.text();
  return {
    bundle_status: r.status,
    has_new_label: txt.includes('提前结束 — 交付文件名'),
    no_old_label: !txt.includes('完成条件 — 文件名'),
    has_deliverable_hint: txt.includes('{{节点id.deliverable}}'),
  };
})()
```

Expected: all four booleans true / 200.

- [ ] **Step 4: Commit**

```bash
git add app/server/static/js/portal_bundle.js
git commit -m "feat(canvas): UI rename 完成条件 → 提前结束 + clarify deliverable separately"
```

---

## Task 5: User-facing documentation

**Files:**
- Create: `docs/canvas-workflows.md` (new)

Captures everything a user needs to write a working canvas workflow with deliverables — without reading the spec or source.

- [ ] **Step 1: Write the doc**

Create `docs/canvas-workflows.md`:

```markdown
# Writing Canvas Workflows

A canvas workflow is a DAG of agent / decision / parallel nodes that produces deliverables in a per-run shared directory. This doc covers the contract you write against; for design rationale see `superpowers/specs/2026-05-02-canvas-deliverable-design.md`.

## File Layout per Run

```
~/.tudou_claw/canvas_runs/<run_id>/
└── shared/
    ├── <node_id_1>/         ← node 1's outputs go here
    │   ├── _meta.json       ← who produced this, when, etc.
    │   └── (your files)
    └── <node_id_2>/
        └── ...
```

Each agent node automatically gets:

- `working_dir = shared/<node_id>/`. Anything the agent writes via `write_file` / `bash` lands there.
- Read access to the WHOLE `shared/` tree (sibling node subdirs included).
- A `_meta.json` with audit trail.

On retry, the node's subdir is wiped and recreated fresh — audit log preserves the history.

## Variable Layer

Downstream nodes reference upstream outputs via `{{node_id.key}}` placeholders in `prompt` (or any string config field):

| Variable | What you get |
|---|---|
| `{{n_search.deliverable}}` | Absolute path to `n_search/` (always a directory) |
| `{{n_search.deliverable_relative}}` | `"n_search/"` for display |
| `{{n_search.output}}` | The agent's final text reply (LLM stdout) |
| `{{n_search.task_id}}` | The chat task id |
| `{{n_search.duration_s}}` | Wall-clock seconds the node ran |
| `{{n_search.artifact_count}}` | How many files registered |
| `{{n_search.artifact_ids}}` | List of artifact ids |
| `{{n_search.file_<sanitized_name>}}` | Absolute path to one specific file |

Most prompts only ever use `{{nid.deliverable}}` — point downstream at the directory and let the LLM `ls` / `read_file` what's inside.

## Agent Node Config

```jsonc
{
  "id": "n_search",
  "type": "agent",
  "label": "搜索 AI 热点",
  "config": {
    "agent_id": "3ea6b18d4de5",
    "prompt": "上网搜索今日 AI 热点 TOP10，写到 trends.md 里。",
    "timeout": 1200,
    "retry": 1,
    "success_when": { "file_glob": "trends.md" }
  }
}
```

| Field | Required | Behavior |
|---|---|---|
| `agent_id` | yes | Which agent runs this node |
| `prompt` | yes | The user message handed to the agent. Supports `{{...}}` from upstream. |
| `timeout` | yes | Hard wall in seconds. Beyond this the canvas aborts the LLM and node FAILS. |
| `retry` | optional, default 0 | Number of automatic retries before FAILED |
| `success_when.file_glob` | optional | Early-termination glob. When a NEW file in `shared/<node_id>/` matches, abort LLM + mark SUCCEEDED. Solves the "LLM done but won't shut up" race. |

## Failure Modes

| Mode | Cause | Where it surfaces |
|---|---|---|
| `EMPTY_DELIVERABLE` | Agent finished but didn't write anything | Node FAILED with this error code; downstream SKIPPED |
| `TimeoutError` | Beyond `timeout` and no success_when match | Node FAILED |
| Tool / LLM error | Anything bubbling out of the agent | Node FAILED with the original error message |
| Canvas validator rejection | Bad config (no agent_id, no prompt, missing edge) | Run never starts; `executable_status` stays `draft` |

A FAILED node cascade-skips its descendants automatically; the workflow run state ends FAILED. Use the **重试** button on the failed node to retry just that one (existing feature).

## Examples

### Single-file deliverable

```
n_search → n_analyze
```

`n_search` writes one file, `n_analyze` reads it:

```
n_search.config.prompt:
  上网搜索今日 AI 热点 TOP10，
  写到 trends.md（用中文）。
n_search.config.success_when.file_glob: "trends.md"

n_analyze.config.prompt:
  基于上游搜索结果分析变现机会，输出 monetization.md。
  上游交付件: {{n_search.deliverable}}
  里面有一个 trends.md，read_file 读它。
```

### Multi-file deliverable (app + tests)

```
n_dev → n_review
```

`n_dev` produces a whole project tree, `n_review` audits it:

```
n_dev.config.prompt:
  开发一套用户管理系统，包含：
  - backend/app.py (FastAPI)
  - frontend/index.jsx (React)
  - tests/test_app.py
  写到 working_dir 里。

n_review.config.prompt:
  上游开发了一套系统，目录: {{n_dev.deliverable}}
  请：
  1. glob_files 该目录下所有 .py 和 .jsx
  2. read_file 每个 + code-review
  3. 输出 review.md（按文件分节，标 critical/major/minor）
```

## Tips

- **Start small**: 2-node DAG first. Verify the deliverable path actually appears via the run log drawer before adding more nodes.
- **Specific prompts**: tell the agent the exact filename you want (e.g., "写到 trends.md") — then `success_when.file_glob: "trends.md"` becomes a reliable end-signal.
- **Keep timeouts realistic**: web-search agents often take 5-15 minutes. Set `timeout: 1200` or higher when you're going to crawl pages.
- **Use the retry button**, not the run button, when fixing a single-node failure mid-DAG. Run starts everything from scratch; retry only does the failed node + downstream.
```

- [ ] **Step 2: Commit**

```bash
git add docs/canvas-workflows.md
git commit -m "docs(canvas): user-facing workflow authoring guide"
```

---

## Task 6: End-to-end sanity check on the existing AI 热点 workflow

**Files:**
- No code changes — this is a verification task.

The user's `wf-555814df2864` is already configured with `success_when.file_glob` (added in commit `d2a14a6`) plus prompt-level `{{n_monwm3sl.output}}` reference. After tasks 1-3 it should run end-to-end without the EMPTY_DELIVERABLE check tripping.

- [ ] **Step 1: Verify the workflow file is still valid**

```bash
python3 -c "
import json
from app.canvas_workflows import WorkflowStore
wf = json.load(open('/Users/pangwanchun/.tudou_claw/Orchestration_workflows/wf-555814df2864.json'))
issues = WorkflowStore.validate_for_execution(wf)
print('issues:', issues if issues else 'PASS')
print('executable_status:', wf.get('executable_status'))
"
```

Expected: `issues: PASS`. If executable_status is `draft`, that's because the user changed the prompt; user can re-validate via the canvas UI.

- [ ] **Step 2: Confirm the deliverable variable wiring is in place**

```bash
grep -n '"deliverable"\|"deliverable_relative"' app/canvas_executor.py
```

Expected: at least 4 hits (in the outputs dict and _meta.json write).

- [ ] **Step 3: Restart preview to pick up backend changes**

(Use `mcp__Claude_Preview__preview_stop` then `preview_start` — same pattern as previous turns.)

- [ ] **Step 4: Done — manual UI verification by the user**

The user reruns the workflow from the canvas UI. They should observe:

- `shared/n_monwm3sl/` and `shared/n_monwis0f/` subdirs now exist after the run
- Each contains `_meta.json` plus the agent's output files
- The downstream prompt resolves `{{n_monwm3sl.output}}` to the search result text (already working from commit history)

No additional commit — this task is verification only.

---

## Self-Review

**Spec coverage:**

| Spec section | Tasks |
|---|---|
| Per-node subdir | Already done in `2264eec` ✓ |
| _meta.json | Already done in `2264eec` ✓ |
| Variable Layer (deliverable, deliverable_relative) | Task 1 cleans up cruft ✓ |
| Early Termination (success_when) | Task 2 makes it canonical ✓ |
| Empty-Deliverable Handling | Task 3 ✓ |
| Backward Compatibility | Tasks 1-2 don't break the live workflow (Task 6 verifies) ✓ |
| Non-Agent Nodes | Already excluded in `2264eec` (only `_exec_agent` calls `node_dir`) ✓ |
| Frontend label rename | Task 4 ✓ |
| Documentation | Task 5 ✓ |

**Placeholder scan:** All steps have explicit code blocks, exact file paths, and expected output. No "TODO", "fill in details", or "similar to Task N" patterns. ✓

**Type consistency:**
- `_has_real_deliverable(node_dir: Path | str)` introduced in Task 3, used in Task 3. ✓
- `success_when` and `success_file_glob` consistent across Tasks 2-3.
- `deliverable` / `deliverable_relative` consistent across spec + Tasks 1, 5.

**Scope check:** Single subsystem (canvas), 6 small tasks, ~1-2 hours total. No further decomposition needed.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-05-02-canvas-deliverable-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch with checkpoints

**Which approach?**
