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

## Parallel Execution (added 2026-05-02)

The canvas executor runs all ready nodes concurrently up to a configurable cap. Linear DAGs stay sequential (only one node ready at a time); branched DAGs fan out automatically.

### Implicit parallel — just draw branches

```
                ┌─→ [agent A: 抓社交媒体]
[start]         ├─→ [agent B: 抓新闻]
                └─→ [agent C: 抓 GitHub]   →  [汇总分析]  →  [end]
```

When `start` completes, A/B/C all become ready and run on three threads simultaneously. The downstream `汇总分析` node waits for all three to succeed before starting (existing DAG-deps behavior).

### Concurrency cap

`Settings → 系统配置 → 画布编排 → Max parallel nodes per run` (default 6, range 1..32). Reads the live value at the start of each run iteration.

### Failure mode — fail-fast

If any parallel branch fails, the engine cancels the others (`task.abort()` on their LLM calls) and the run state goes to `ABORTED` (not `FAILED`). Sibling nodes that were aborted mid-flight show `NodeState.ABORTED`. Use the existing **重试** button on the failed node — only the failed branch + downstream get re-run.

### Same-agent rule

The same `agent_id` cannot appear in two parallel-reachable nodes. The canvas editor's agent picker hides already-used agents in sibling parallel branches. The validator double-checks at `mark ready` time. Reason: `chat_async` serializes per-agent — two parallel nodes sharing an agent would queue, defeating the parallelism.

## Agent-Internal Parallelism — `delegate_parallel`

When an agent needs to fan-out at runtime (parent decomposes a task only after seeing the input), use the `delegate_parallel` tool inside the agent's prompt/code:

```python
# Inside an agent's reasoning
results = self.delegate_parallel([
    {"task": "write FastAPI backend in backend/", "child_role": "coder"},
    {"task": "write React frontend in frontend/", "child_role": "coder"},
    {"task": "write pytest suite in tests/", "child_role": "coder", "hint_subdir": "tests"},
])
# results: list of {status, output, error, working_subdir, child_role}
```

Each child gets its own subdir under the parent's working_dir (default `child_<idx>_<role>` or the `hint_subdir` you provided). Cap is `Settings → 系统配置 → Agent 委派 → Max parallel children per call` (default 6, range 1..32).

Same fail-fast contract: one child's exception sets a cancel flag; siblings receive `status: aborted`. Parent decides whether to retry, escalate, or proceed with partial results.
