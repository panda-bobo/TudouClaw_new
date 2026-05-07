# TudouClaw — Tools Reference

**Total tools registered**: 70
_(Generated from `app.tools.TOOL_DEFINITIONS` — keep regenerating when tools change.)_

## How visibility works

Every agent's `tools[]` payload to the LLM is filtered by **two**
layers (see [`tool_capabilities.py`](../app/tool_capabilities.py)):

1. **CORE Universal** — 4 reflex tools every agent sees regardless of
   per-agent config: `plan_update`, `get_skill_guide`, `memory_recall`,
   `knowledge_lookup`. These don't need to be ticked in Tool Permissions.
2. **Allow-list** — everything else is gated by `agent.profile.allowed_tools`
   (Tool Permissions UI tick boxes). Tools the agent isn't allow-listed
   for don't even appear in the LLM's tools[] payload.

Every tool's schema also receives a universal `_reason: string ≤100 chars`
required field (toggle in System Configuration → "Tool Reason Required")
forcing the LLM to articulate WHY before each call.

**Composite tools** fold multi-step rituals into ONE LLM round-trip:
`finalize_step` (coder/researcher closure) · `submit_review` (reviewer)
`bootstrap_project` (PM init) · `init_project_context` (CC `/init` equivalent).

**Subagent fork** (`spawn_explore_subagent`) off-loads focused read-only
research to a stateless ephemeral subagent — parent context stays clean.

**Background bash** (`bash run_in_background=true` + `bash_logs` + `bash_kill`)
lets long-running dev servers / watchers / daemons not block the agent.

**TodoWrite-style scratch list** (`agent_todo`) gives each agent a private
in-memory todo list across turns — separate from project plan / milestones.

---

## CORE Universal — auto-shipped to every agent

### `get_skill_guide`

Load a granted skill's guide. brief mode (default, ~200 tokens) vs verbose (full body). cd to returned skill_dir before running scripts. NOT for MCP — use mcp_call. NOT for registering — use submit_skill.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `name` | string | ✓ | Skill name (e.g. 'pdf', 'docx', 'xlsx') |
| `brief` | boolean | — | Default true — return headings/summary only. Set false to load the full body (much larger). |
| `agent_id` | string | — | Optional agent ID to resolve agent-local skill path |

### `knowledge_lookup`

Search KB (shared + agent's expert pool). Same-mode ONE-SHOT per turn — different modes are allowed. Modes: search (top-k content, default), count (chunk aggregates by source_file — WARNING chunks ≠ user-meaningful units like cases/scenarios), list (metadata inventory, no content), outline (UNIQUE heading_paths per file — use for 'how many test cases / scenarios / sections per document', this is the right tool for '用例数 / 场景数 / 章节数' questions). Cite hits as [source_file §heading_path]; reason only from retrieved content.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `query` | string | — | Search keyword / substring. Required for mode=search; optional filter for mode=count and mode=list. Ignored by mode=outline. |
| `entry_id` | string | — | Specific entry ID to read (from a previous search result) |
| `mode` | string | — | Retrieval mode. search=top-k content chunks; count=exact aggregate by source_file (CHUNK count, not user-units); list=per-chunk metadata inventory; outline=UNIQUE heading_paths per file (one leaf heading_path = one test case/scenario/section in a structured doc — pick this for 'how many cases/scenarios/sections per service' questions). |
| `source_file` | string | — | (mode=outline only) Substring filter on source_file path. Empty = all files. |
| `heading_pattern` | string | — | (mode=outline only) Regex applied to heading_path; only headings matching this regex are counted. Empty = all headings. |

### `memory_recall`

Query YOUR OWN agent-private long-term memory. ONE-SHOT per turn: pack all keywords in one query (second call rejected). Returns top-K facts by similarity; check before fresh web_search/fetch. For cross-role reference use knowledge_lookup.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `query` | string | ✓ | What are you trying to remember? (topic / keywords / question) |
| `category` | string | — | Optional filter: intent \| reasoning \| outcome \| rule \| reflection (omit for all). |
| `top_k` | integer | — | Max hits (default 5, max 20). |

---

## File Ops

_Capability skill: `file-ops` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `edit_file`

Replace an exact substring in an existing file with a new substring. Requires the old_string to appear EXACTLY ONCE in the file.

**Use when**: making surgical changes to a known file, renaming a unique identifier, adjusting a specific line.

**Not for**: creating new files (use write_file). Not for replacing strings that appear multiple times — widen old_string with surrounding context to force uniqueness.

**Output**: confirmation 'Successfully edited PATH (replaced 1 occurrence)'.

**GOTCHA**: fails with a count error if old_string appears 0 or 2+ times. Whitespace and indentation must match byte-for-byte. Prefer short context anchors over regex patterns.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `path` | string | ✓ | File path to edit |
| `old_string` | string | ✓ | Exact string to find |
| `new_string` | string | ✓ | Replacement string |

### `glob_files`

Find files matching a glob pattern by NAME/PATH. Returns sorted list of paths.

**Use when**: listing files by extension (**/*.py), finding all tests (**/test_*.py), enumerating a subdirectory.

**Not for**: searching inside file contents (use search_files). Not for reading (use read_file).

**Output**: newline-separated paths, capped at 500 with a total-count notice.

**GOTCHA**: uses Python pathlib glob semantics — `**` must be a full path segment (src/**/*.py works, src/**.py does not). Hidden directories are filtered out.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `pattern` | string | ✓ | Glob pattern, e.g. '**/*.py' or 'src/**/*.js' |
| `path` | string | — | Base directory for the search (default: current directory) |

### `read_file`

Read UTF-8 text content from a file with optional line range.

**Use when**: viewing code/config/docs/data files, inspecting a known file path, reading part of a large file with offset+limit.

**Not for**: searching by content (use search_files) or finding files by name (use glob_files). Binary files return replacement chars.

**Output**: header line [path — lines N-M of T] + numbered lines (1-based).

**GOTCHA**: offset is 0-based but output line numbers are 1-based. For binary files prefer file-specific tools (pptx/pdf skills). Path is resolved against the sandbox root.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `path` | string | ✓ | Absolute or relative file path |
| `offset` | integer | — | Start reading from this line number (0-based). Default 0. |
| `limit` | integer | — | Maximum number of lines to read. Default: read all. |

### `search_files`

Regex-search file contents recursively (like `grep -rn`). Returns matching lines with path and line number.

**Use when**: finding where a symbol is referenced, hunting a string/pattern across the repo, locating TODO/FIXME comments.

**Not for**: finding files by name (use glob_files). Not for reading a specific file's content (use read_file). Hidden dirs and node_modules/__pycache__/.git are skipped automatically.

**Output**: `path:lineno: matching_line` per match, truncated at 200 matches with a notice.

**GOTCHA**: pattern is a Python regex — escape special chars. Very broad patterns return a truncated sample; narrow with `include` glob (e.g. '*.py') when scanning large trees.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `pattern` | string | ✓ | Regular expression pattern to search for |
| `path` | string | — | Directory or file to search in (default: current directory) |
| `include` | string | — | Glob pattern to filter files, e.g. '*.py' |

### `write_file`

Create a new file or overwrite an existing one with UTF-8 content. Auto-creates parent directories.

**Use when**: generating a new file, saving agent output, creating config/scripts.

**Not for**: surgical edits to an existing file (use edit_file to avoid clobbering). Do not use to append — this is full overwrite.

**Output**: absolute path and byte count on success. The written path is what artifact cards link to.
⚠️ MANDATORY 必填:
  • `path` MUST be present in arguments — relative paths are resolved against your workspace_dir.
  • Files MUST be created INSIDE your workspace (or shared workspace if the agent has one). Absolute paths outside workspace will be rejected by sandbox.
  • For long content (>500 lines / >20KB) prefer `edit_file` on an existing file — write_file with huge content can hit max_tokens and the JSON gets truncated mid-call (`{path: ..., content: <CUT>` → arguments fail to parse → schema reports 'path missing'). If you MUST write a large file, split into multiple write_file calls or use bash heredoc.

**GOTCHA**: overwrites silently — read_file first if uncertain.
Example: write_file(path="index.html", content="<!DOCTYPE html>...")

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `path` | string | ✓ | REQUIRED. Relative path INSIDE workspace (e.g. 'index.html', 'src/main.py'), or absolute path under workspace_dir. Sandbox rejects paths outside workspace. |
| `content` | string | ✓ | REQUIRED. Full file content (UTF-8). For files larger than ~20KB consider edit_file instead — large content risks max_tokens truncation that corrupts the tool call. |

---

## Shell Ops

_Capability skill: `shell-ops` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `bash`

Execute a shell command. Two modes: foreground (default, sync, blocks until exit/timeout) and background (run_in_background=true, returns immediately with a process_id).

**Use when**: running a compile/test/format command, git operations, quick system queries; or — with run_in_background=true — starting a long-running dev server / build watch / file watcher / daemon.

**Not for**: file reads (use read_file), file searches (use search_files/glob_files), pip installs (use pip_install for clear intent), date math (use datetime_calc). Avoid `bash cd <dir>` as a standalone call — each bash is a fresh shell, cd doesn't persist; chain with && (e.g. `cd /path && ls`) instead.

**Output**: foreground returns stdout + stderr + exit code. Background returns 🟢 status line with pid + first log slice; use bash_logs(process_id) for incremental output, bash_kill(process_id) to terminate.

**GOTCHA**: foreground max timeout 600s. For dev servers / `npx http-server` / `npm run dev` etc. — ALWAYS pass run_in_background=true; they never exit, so foreground will timeout and kill them. Use chain (`cmd1 && cmd2`) when you need cwd to persist across steps.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `command` | string | ✓ | Shell command to execute. Chain multi-step shell work with && or ; in a single call rather than issuing several bash calls. cd is per-call (doesn't persist across calls). |
| `timeout` | integer | — | Timeout in seconds for foreground mode (default 30, max 600). Ignored when run_in_background=true. |
| `run_in_background` | boolean | — | Start the command in the background without blocking. Returns a process_id. Use for dev servers / watchers / daemons. Pull output later with bash_logs. |
| `background_log_lines` | integer | — | When run_in_background=true: how many initial log lines to return in the response (default 30, max 500). |

### `run_tests`

Run the project's test suite and get a STRUCTURED result (pass/fail counts, failure list, framework detected). Auto-detects pytest / npm / go / cargo.

**Use when**: after writing code (TDD loop), before declaring a step complete, verifying a bugfix actually landed. This is the canonical 'did it work' check — prefer it over ad-hoc `bash('pytest')` because the result is parsed, not raw stdout.

**Not for**: starting dev servers or one-off scripts — use bash.

**Output**: JSON with ok/passed/failed/skipped counts + up to 10 failure lines + trailing stdout for context. Exit code alone is NOT trusted — 0 pass tests also counts as failure.

**GOTCHA**: for npm/jest, ensure `npm test` is wired in package.json. For go, runs `./...` by default (whole module). Pass `paths` to narrow scope.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `paths` | string | — | Space-separated test paths/patterns. Empty = all tests in cwd. |
| `framework` | string | — | Force framework: pytest \| npm \| go \| cargo. Empty = auto-detect. |
| `extra_args` | string | — | Additional CLI args appended verbatim (e.g. '-k test_foo' for pytest). |
| `timeout` | integer | — | Seconds, clamped to [10, 1800]. Default 600. |

---

## Web Ops

_Capability skill: `web-ops` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `web_fetch`

Fetch a specific URL and extract plain text (strips script/style, decodes HTML entities).

**Use when**: reading a documentation page, article, or API reference after finding it via web_search or when the user gives an explicit URL.

**Not for**: discovering new URLs (use web_search). Not for JSON API calls (use http_request — it preserves status codes and headers). Not for PDF/binary URLs.

**Output**: `[Content from URL]` header + extracted plain text, truncated to max_length (default 5000 chars).

**GOTCHA**: default 5000-char cap is deliberate — research sessions that ran 10000+ chars/fetch burned 25k+ tokens of context. Raise max_length only when one URL genuinely needs full capture.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `url` | string | ✓ | The URL to fetch |
| `max_length` | integer | — | Maximum number of characters to return (default: 10000) |

### `web_search`

Search the public web via DuckDuckGo (API + HTML fallback). Returns ranked results with title/URL/snippet.

**Use when**: finding documentation, recent news/events, research sources, third-party APIs.

**Not for**: fetching the body of a specific known URL (use web_fetch). Not for searching the local filesystem (use search_files). No deep research chaining — call web_fetch on top results for details.

**Output**: numbered list with title/URL/snippet blocks, capped at `max_results` (default 8).

**GOTCHA**: DDG may rate-limit on burst — one search then reading several results is usually fine. Snippets are short; for substance always follow with web_fetch on the best 1-3 URLs.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `query` | string | ✓ | The search query |
| `max_results` | integer | — | Maximum number of results to return (default: 8) |

---

## Memory Ops

_Capability skill: `memory-ops` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `learn_from_peers`

Import high-quality experiences from another role's library into your own bucket.

**Use when**: you need a capability another role has — e.g. a PM agent learning design heuristics from designer's experiences, a coder learning test patterns from QA's.

**Not for**: one-shot reference lookup (use knowledge_lookup). Not for learning from a specific agent — this is ROLE-level only.

**Output**: imported experiences list with priority / scene / rules / success-rate summary.

**GOTCHA**: imports are FILTERED — only experiences >=75% success rate come through. Returns 0 if the source role has no matching experiences. Topic is a keyword filter, not a semantic query.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `source_role` | string | ✓ | The role to learn from, e.g. 'designer', 'coder', 'analyst' |
| `topic` | string | — | Specific topic to search for, e.g. 'PPTX creation', 'API design' |
| `limit` | integer | — | Max number of experiences to import (default 5) |

### `save_experience`

[DEPRECATED — use `wiki_ingest` instead]. Legacy tool kept for back-compat only. New code MUST call wiki_ingest(kind='experience', title, body) which writes a markdown page to the wiki layer. Calling save_experience now writes to the legacy JSON store that is no longer auto-injected into prompts.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `scene` | string | ✓ | Trigger scenario / when this experience applies |
| `core_knowledge` | string | ✓ | Core insight / knowledge point |
| `action_rules` | array | — | 1-3 positive action rules (do-this) |
| `taboo_rules` | array | — | 1-2 taboo rules (avoid-this) |
| `priority` | string | — | Importance; default medium |
| `tags` | array | — | Optional classification tags |
| `exp_type` | string | — | retrospective = 复盘产出; active_learning = 主动学习产出 |
| `source` | string | — | Human-readable origin (e.g. 'POC 贪吃蛇 产品复盘') |
| `role` | string | — | Override the role bucket; defaults to the calling agent's role |
| `evidence` | array | — | Citation references pointing to the source of truth for this lesson. Conventional formats: 'path/to/file.py:LINE' (code), 'docs/SPEC.md#section' (doc anchor), or a URL. Listed when the experience is injected into future prompts so agents/reviewers can jump back to the raw evidence. Dedup + whitespace-trim applied automatically. |

### `share_knowledge`

Write a new entry to the shared knowledge base so ALL agents can access it via knowledge_lookup.

**Use when**: you have produced a reusable playbook / template / reference that teams would benefit from — API error handling patterns, PPTX best practices, design conventions.

**Not for**: role-local learnings (use save_experience — that stays in one role's bucket). Not for chat-log content. Not for secrets or sensitive data (the KB is shared).

**Output**: 'Knowledge shared' confirmation with entry id. Source attribution (your agent name/role) is auto-appended to the content.

**GOTCHA**: title is the primary search key — make it descriptive. Write content with retrieval in mind: include trigger keywords someone would search for.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `title` | string | ✓ | Concise title for the knowledge entry |
| `content` | string | ✓ | Detailed knowledge content — include steps, tips, examples, templates as needed |
| `tags` | array | — | Tags for categorization, e.g. ['pptx', 'design', 'template'] |

---

## Data Processing

_Capability skill: `data-process` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `datetime_calc`

Date/time operations: current time, date differences, add duration, format conversion, timezone conversion.

**Use when**: computing time intervals, converting between timezones, formatting dates for display.

**Not for**: scheduling tasks (use task_update with run_at). Not for parsing relative text like '5分钟后' — that is task_update's job; here `date` must be a concrete date string.

**Output**: human-readable summary plus ISO representation for downstream tool calls.

**GOTCHA**: accepts many date formats (ISO / YYYY-MM-DD / YYYY/MM/DD / etc) but a few like '今天' are NOT parsed — use action=now + timezone for 'current time in zone'. For 'convert', naive dates are assumed UTC.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `action` | string | ✓ | Action: 'now' (current time), 'diff' (difference between dates), 'add' (add duration to date), 'format' (reformat a date), 'convert' (convert timezone) |
| `date` | string | — | Date string (ISO format preferred, e.g. '2024-03-15T10:30:00') |
| `date2` | string | — | Second date for 'diff' action |
| `days` | integer | — | Days to add (for 'add' action) |
| `hours` | integer | — | Hours to add (for 'add' action) |
| `minutes` | integer | — | Minutes to add (for 'add' action) |
| `timezone` | string | — | Timezone name (e.g. 'Asia/Shanghai', 'US/Eastern', 'UTC') |
| `format` | string | — | Output format string (Python strftime, e.g. '%%Y-%%m-%%d %%H:%%M') |

### `json_process`

Parse / extract / transform / validate JSON data. Can read from a string or a file path.

**Use when**: validating a JSON blob, extracting nested fields with a path expression, flattening / merging / converting to CSV.

**Not for**: raw text manipulation (use text_process). Not for writing JSON to disk (use write_file after json.dumps). The file-path mode is read-only.

**Output**: formatted text. Results capped at 10000 chars (MAX_JSON_RESULT_CHARS for extract) — narrow your path if truncated.

**GOTCHA**: path syntax is JSONPath-like but simplified — users[0].name or data.items works; JSONPath filters ($.., [?...]) do NOT. to_csv expects an array of flat objects.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `action` | string | ✓ | Action: 'parse' (validate & pretty-print), 'extract' (extract field), 'keys' (list top-level keys), 'flatten' (flatten nested), 'to_csv' (JSON array to CSV), 'from_csv' (CSV to JSON), 'merge' (merge two JSON objects), 'count' (count items) |
| `data` | string | ✓ | JSON string or file path to process |
| `path` | string | — | JSONPath-like expression for 'extract' (e.g. 'users[0].name', 'data.items') |
| `data2` | string | — | Second JSON string for 'merge' action |

### `text_process`

Batch text transforms: count / find+replace (regex) / extract / sort / dedup / base64 / url-encode / hash / head / tail / split.

**Use when**: one-off text manipulation that would otherwise need a bash pipeline (grep | sort | uniq).

**Not for**: processing JSON (use json_process). Not for operations on files — pass the file content via read_file first. Regex uses Python syntax.

**Output**: transformed text, capped at 10000 chars. 'count' returns lines/words/chars; 'hash' returns algorithm:hex.

**GOTCHA**: `replace` uses re.sub — backreferences are \1, not $1. `dedup` keeps insertion order; if you want sort+unique call `sort` then `dedup`.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `action` | string | ✓ | Action: 'count' (word/line/char count), 'replace' (find & replace), 'extract' (extract regex matches), 'sort' (sort lines), 'dedup' (remove duplicates), 'base64_encode', 'base64_decode', 'url_encode', 'url_decode', 'hash' (md5/sha256), 'head' (first N lines), 'tail' (last N lines), 'split' (split by delimiter) |
| `text` | string | ✓ | Input text to process |
| `pattern` | string | — | Regex pattern (for replace/extract) |
| `replacement` | string | — | Replacement string (for replace) |
| `n` | integer | — | Number of lines (for head/tail, default: 10) |
| `algorithm` | string | — | Hash algorithm: md5, sha256, sha1 (for hash, default: sha256) |
| `delimiter` | string | — | Delimiter (for split, default: newline) |

---

## UI Visibility

_Capability skill: `ui-visibility` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `emit_ui_block`

Render an interactive UI block (choice buttons or checklist) inline in chat. Max 8 choices / 20 checklist items; unique item IDs. For free-form Q use prose; for execution steps use plan_update.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `kind` | string | ✓ | Block type: 'choice' = clickable buttons, 'checklist' = display-only list. |
| `prompt` | string | ✓ | The question or header text shown at the top of the block (max 400 chars). |
| `options` | array | — | For kind='choice'. Each item: a string label OR {id, label}. |
| `items` | array | — | For kind='checklist'. Each item: a string text OR {id, text, done}. |

---

## Scheduling / Task Tracking

_Capability skill: `scheduling` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `task_update`

🎯 作用对象: **当前 project 的任务列表**(项目右栏 TASKS 显示的那些);在 solo / meeting context 下作用于 agent 个人任务。**不是**给队友派活的工具(派活用 @ 提及 / send_message / create_milestone with responsible_agent_id)。

Create/update/complete/list shared task queue entries; registers recurring or delayed tasks with the scheduler. recurrence_spec: daily='HH:MM', weekly='DOW HH:MM', monthly='D HH:MM'. For your visible execution checklist use plan_update.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `action` | string | ✓ | Action: create \| update \| complete \| list |
| `task_id` | string | — | Task ID (required for update/complete) |
| `title` | string | — | Task title (for create) |
| `description` | string | — | Task description |
| `status` | string | — | New status: todo \| in_progress \| done \| blocked |
| `result` | string | — | Result summary (for complete) |
| `recurrence` | string | — | Recurrence type: once (default, one-time) \| daily \| weekly \| monthly \| cron. Use 'daily' for 每天, 'weekly' for 每周, 'monthly' for 每月. |
| `recurrence_spec` | string | — | Schedule spec: daily='HH:MM' (e.g. '09:00'), weekly='DOW HH:MM' (DOW=SUN\|MON\|TUE\|WED\|THU\|FRI\|SAT, e.g. 'MON 09:00'), monthly='D HH:MM' (e.g. '1 09:00'), cron='m h dom mon dow'. |
| `run_at` | string | — | For delayed one-time tasks: when to execute. Accepts '+Nm' (N minutes from now, e.g. '+5m'), '+Nh' (N hours from now, e.g. '+2h'), or 'HH:MM' (today at specific time, e.g. '18:30'). When set, the scheduler will auto-trigger this task at the specified time. Use this for '5分钟后', 'in 10 mins', '下午3点' etc. |

---

## Messaging

_Capability skill: `messaging` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `ack_message`

Mark one or more inbox messages as acknowledged (state='acked'). Acked messages stop being surfaced in the auto-injected inbox block at chat start.

**Use when**: you've read a message and either acted on it or decided no action is needed — this prevents it from re-appearing every turn.

**Not for**: deleting messages (they remain queryable). Not for replying (use reply_message).

**Output**: count of acked / skipped. Only YOUR messages can be acked — attempting to ack another agent's messages silently skips them.

**GOTCHA**: messages are NOT auto-acked; merely being read in the auto-injection only transitions new→read. Ack is a deliberate 'I'm done with this' marker.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `message_ids` | string | ✓ | One message id, or multiple ids separated by commas or whitespace (e.g. 'msg_abc, msg_def'). |

### `check_inbox`

🎯 作用对象: **你自己**的系统内 inbox(其他 agent 通过 send_message / reply_message 发给你的消息)。**不是**真实邮件 inbox。

Read your inbox — messages sent to you by other agents via send_message / reply_message.

**Use when**: the plan calls for reviewing incoming handoffs, or you suspect teammates have pinged you since last turn.

**Not for**: sending new messages (use send_message / reply_message). Not for ACKing (use ack_message).

**Output**: compact list of unread messages with id / from / priority / timestamp / preview. Does NOT modify state — reading here doesn't mark messages as acked.

**GOTCHA**: unread messages are ALSO auto-injected at the start of each chat turn, so you may not need to call this explicitly. Use it when you want to re-check mid-turn or include already-read items via include_read.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `limit` | integer | — | Max messages to return (default 20, max 100). |
| `include_read` | boolean | — | If true, also include recent read-but-not-acked messages (default false). |

### `reply_message`

Reply to an inbox message (preserves thread_id). Use envelope (summary/key_fields/artifact_refs) over long content. For unrelated new pings use send_message; to silently mark done use ack_message.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `message_id` | string | ✓ | The id of the message you are replying to (from check_inbox or the auto-injected inbox block). |
| `summary` | string | — | 1-3 sentence conclusion / answer. Main thing the recipient reads. |
| `key_fields` | object | — | Structured result — numbers, decisions, file paths. Keep it small. |
| `artifact_refs` | array | — | Paths to any large outputs produced. Recipient reads with read_file. |
| `content` | string | — | (Legacy) raw body. Required only if no summary+structured fields provided. |
| `priority` | string | — | urgent \| normal \| low (default normal). |
| `ttl_s` | integer | — | Optional seconds-to-live; 0 (default) means never expire. |

### `send_message`

🎯 作用对象: **同进程内的另一个 agent**(系统内的 AI 同事,如小刚/小专),**不是**真人也**没有**邮箱。

Send a structured message to another agent's inbox (in-process, async).
Use envelope (summary/key_fields/artifact_refs) over long content. For blocking handoffs use handoff_request; for scheduled tasks use task_update.

**Not for**: external email — use mcp_call for that. Not for posting to project group chat — write @ in your reply text instead.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `to_agent` | string | ✓ | Agent ID or name to send the message to |
| `summary` | string | — | 1-3 sentence conclusion. THIS is what the recipient mainly reads. Be concrete. |
| `key_fields` | object | — | Structured payload — numbers, decisions, names, URLs, status. Keep it small (a handful of keys). Example: {"decision": "B", "risk_level": "low", "target_env": "staging"}. |
| `artifact_refs` | array | — | File paths or artifact IDs pointing at large outputs. Recipient reads them with read_file if needed. Always prefer this over embedding a long body. |
| `content` | string | — | (Legacy) optional raw body. Only use if you genuinely need inline detail that won't fit the summary and isn't big enough to warrant an artifact. If omitted, summary alone is fine. |
| `msg_type` | string | — | Message type: task \| info \| result \| question (default: task) |

---

## Handoff / Delegation

_Capability skill: `handoff` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `emit_handoff`

Structured baton-pass to the next agent (summary + deliverable + followups). AT MOST ONE per task completion. For status updates use send_message; for blocking handoffs use handoff_request.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `summary` | string | ✓ | One-paragraph what-I-did. Max 500 chars. |
| `deliverable_path` | string | — | Relative path of the artifact in the shared workspace, if any (e.g. 'report.pptx', 'analysis/findings.md'). Empty if no file deliverable. |
| `highlights` | array | — | Key findings / decisions / data points (up to 6). String or {text}. |
| `followups` | array | — | Suggested next steps for other agents (up to 8). Each: {for: target_agent_name_or_role, task: concrete_action}. |

### `handoff_request`

🎯 作用对象: **同进程内的另一个 agent**(系统内的 AI 同事),阻塞等结果。**不是**外部邮箱/真人。

BLOCKING task transfer with 3-state handshake (pending → acknowledged → completed). Caller blocks until receiver returns or 600s timeout. For FYI broadcasts use send_message; for parallel independent work use team_create. Always include expected_output.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `to_agent` | string | ✓ | Target agent ID or name (the teammate picking up the work) |
| `task` | string | ✓ | What the receiver should do. Be concrete and self-contained — the receiver may not have your full context. |
| `expected_output` | string | — | What the receiver should return (format / acceptance criteria). Optional but strongly recommended. |
| `context` | string | — | Any extra background the receiver needs (file paths, links, prior findings). Optional. |
| `timeout_seconds` | integer | — | Max wait time before marking the handoff as timed out (default 600). |

### `team_create`

Spawn a background sub-agent to run an independent task in parallel. Inherits caller's model/provider.

**Use when**: a task splits into 2-3+ independent pieces that can run simultaneously (research → 3 aspects, refactor → multiple modules); total wall-clock is bounded by the longest sub-task.

**Not for**: tasks that need your context/conversation history (the worker starts fresh). Not for simple question-answer delegation (use handoff_request for 1:1 work with visible ack). Not for scheduled jobs (use task_update with run_at).

**Output**: worker label + transient task_id; the sub-agent posts its result back to your task list when done.

**GOTCHA**: the worker is TRANSIENT — it disappears after completing. Its result lands in your task list, not as a chat message. Do NOT use for 'fire and forget logging' — use send_message instead.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `name` | string | ✓ | Name for the sub-agent |
| `role` | string | — | Role preset: coder, reviewer, researcher, tester, devops, writer |
| `task` | string | ✓ | Task description for the sub-agent to execute |
| `working_dir` | string | — | Working directory for the sub-agent (default: current dir) |

---

## Project Management

_Capability skill: `project-management` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `create_goal`

Create a measurable project goal (numeric count/percent or qualitative text).

**Use when**: the user says 'add a goal', 'we want to hit X by Y', 'track progress on Z'.

**Not for**: individual tasks (use task_update). Not for milestones (those bundle deliverables — use create_milestone). Only works inside a project context.

**Output**: goal id + name + metric + target + project id.

**GOTCHA**: metric='count' needs target_value (a number); metric='text' needs target_text. Mixing them silently ignores the unused field — double-check which one the user meant.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `name` | string | ✓ | Goal name (short) |
| `description` | string | — | Longer description / rationale |
| `metric` | string | — | count \| percent \| text (default: count) |
| `target_value` | number | — | Numeric target for count/percent metrics |
| `target_text` | string | — | Qualitative target for text metrics |
| `owner_agent_id` | string | — | Optional owner agent id (default: calling agent) |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

### `create_milestone`

Create a project milestone AND optionally delegate it to another agent.

**Use when**: structuring a project into checkpoints; or assigning a chunk of work to a specific teammate.

**Not for**: individual tasks (use task_update). Not for goals (use create_goal — milestones are checkpoints, goals are metrics).

⭐ DELEGATION (the main reason to set responsible_agent_id):
  - Pass another agent's id (NOT your own) → the system AUTO-FIRES a chat
    message into the project group: '@<that agent> 你被指派负责里程碑「X」...',
    AND that agent immediately starts working on it (no need for you to also
    call send_message — that would be a duplicate).
  - Get teammate ids from the team list at the top of your prompt:
    each line shows  `<role>-<name> [id=<agent_id>]: <responsibility>` — copy the
    id= value into responsible_agent_id.
  - Omit it (or pass your own id) → the milestone is yours; nobody else triggered.

**Output**: milestone id + name + responsible agent + due date + project id; if delegated, also `assigned to <name>`.

**GOTCHA**: due_date accepts 'YYYY-MM-DD' or natural form — prefer ISO for unambiguous parsing.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `name` | string | ✓ | Milestone name |
| `responsible_agent_id` | string | — | Agent id of the responsible teammate. Pass ANOTHER agent's id to delegate (auto-fires a chat message + triggers them to start work). Default = caller's own id (self-assignment, no trigger). Look up ids in the [项目群聊] team list. |
| `description` | string | — | Optional one-paragraph context shown to the responsible agent in the delegation message. Helps them understand scope. Skipped if omitted. |
| `due_date` | string | — | Due date in YYYY-MM-DD or natural form |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

### `submit_deliverable`

Register a concrete artifact as a project deliverable and mark it SUBMITTED (enters review queue).

**Use when**: you produced a document / code file / design / analysis for the current project and it is ready for review. Works only inside a project chat context.

**Not for**: intermediate drafts (wait until ready). Not for agents' internal tool outputs that the user does not need to see. Outside a project context this returns an error.

**Output**: deliverable id + title + kind + project id + resolved file path. If content_text is given without file_path, content is auto-written to the project's shared workspace dir.

**GOTCHA**: project auto-discovered from chat context — if you are not in a project chat, pass project_id. Files outside the shared dir are copied in automatically (so the deliverables UI can find them).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `title` | string | ✓ | Short title for the deliverable |
| `file_path` | string | — | Absolute or relative path to the artifact file |
| `content_text` | string | — | Inline content (for text-only deliverables) |
| `url` | string | — | External URL (for hosted artifacts) |
| `kind` | string | — | document \| code \| design \| analysis \| other (default: document) |
| `milestone_id` | string | — | Optional milestone id to link this deliverable to |
| `task_id` | string | — | Optional task id that produced this deliverable |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

### `update_goal_progress`

Update a goal's current value or mark it as done. Persists progress to the project.

**Use when**: progress is made toward a goal you or teammates previously created — e.g. 'closed 3 more tickets', 'goal reached'.

**Not for**: creating goals (use create_goal). Not for milestones (use update_milestone_status).

**Output**: goal id + new current_value + done state (+ optional note).

**GOTCHA**: current_value must be numeric — for text-metric goals only `done=true/false` and `note` matter. Unknown goal_id returns an error (goals are project-scoped).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `goal_id` | string | ✓ | The goal id to update |
| `current_value` | number | — | New current value (for count/percent metrics) |
| `done` | boolean | — | Mark as complete |
| `note` | string | — | Optional progress note |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

### `update_milestone_status`

Update a milestone's status or attach evidence of completion. Typical transitions: pending → in_progress → done.

**Use when**: you or your team completed work toward a milestone and want to record progress / evidence.

**Not for**: creating milestones (use create_milestone). Not for reassigning ownership (use update_milestone_responsibility). Not for admin confirm/reject — that is a separate endpoint.

**Output**: milestone id + new status + optional evidence length.

**GOTCHA**: attach `evidence` when flipping to done — the admin reviewer uses it to verify. Empty status + empty evidence returns an error ('provide at least one').

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `milestone_id` | string | ✓ | The milestone id |
| `status` | string | — | pending \| in_progress \| done |
| `evidence` | string | — | Evidence text (e.g. links, summary of what was completed) |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

---

## PPTX Author

_Capability skill: `pptx-author` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `create_pptx`

Create a simple PowerPoint .pptx with title/content slides. Layout picked from {title, content, title_content, blank}. Auto-installs python-pptx.

**Use when**: the user wants a basic deck — bullet points, section titles, maybe one image per slide.

**Not for**: complex layouts with charts/shapes/tables (use create_pptx_advanced). Not for editing existing pptx files (this overwrites the output_path).

**Output**: file created at output_path; returns '✓ Created presentation: PATH'.

**GOTCHA**: content is rendered as bullet points split on newlines — do not expect markdown formatting. For visual design beyond bullets use create_pptx_advanced.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `output_path` | string | ✓ | Path where the .pptx file will be saved |
| `title` | string | — | Optional title for the presentation deck |
| `slides` | array | ✓ | Array of slide objects, each with title, content, optional layout, and optional images |

### `create_pptx_advanced`

Create a design-rich PowerPoint with shapes, charts, tables, multi-column layouts, and infographics. Layout types: cover / toc / section / cards / process / kpi / comparison / timeline / chart / table / closing.

**Use when**: building a presentation that needs visual design — cover + TOC + content + charts + closing.

**Not for**: simple bullet-only decks (use create_pptx). Not for editing existing pptx (this overwrites).

**Output**: .pptx saved at output_path; returns '✓ Created advanced presentation (N slides): PATH'.

**GOTCHA**: ❌ do NOT invent layout.type strings (overview/analysis/content/summary all get silently downgraded to 'cards'). ✅ for normal content pages use `cards` (1-9 items). Element x/y/w/h are in INCHES and auto-clamped to slide bounds (10.0 x 5.625).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `output_path` | string | ✓ | 输出 .pptx 文件路径 |
| `theme` | object | — | 全局配色主题 |
| `slides` | array | ✓ | 页面数组。推荐用layout自动排版，也可用elements手动控制，或两者结合。 |

---

## Video Forge

_Capability skill: `video-forge` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `create_video`

Stitch image frames into an MP4 video. Optional audio track. Auto-installs moviepy.

**Use when**: producing a slideshow video from generated or captured images (e.g. time-lapse, animated explainer, tutorial).

**Not for**: recording live video (no capture capability). Not for editing existing videos.

**Output**: .mp4 at output_path. Returns '✓ Video created: PATH'.

**GOTCHA**: each frame needs a `duration` (default 3s) — total video length = sum of durations. Audio track is trimmed to match total video length. moviepy install is SLOW on first use (60s timeout).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `output_path` | string | ✓ | Path where the .mp4 video file will be saved |
| `frames` | array | ✓ | Array of frame objects with image_path and optional duration |
| `fps` | integer | — | Frames per second for the video (default: 24) |
| `audio_path` | string | — | Optional path to audio file to add as soundtrack |

---

## Screenshot

_Capability skill: `screenshot` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `desktop_screenshot`

Capture a screenshot of the local desktop primary monitor. Optional region crop.

**Use when**: the user asks to 'screenshot what is on screen', or agent needs to capture an external app's UI that is not accessible via the browser.

**Not for**: web page screenshots (use web_screenshot). Not for the agent's own Portal UI — the agent cannot see its own browser window meaningfully.

**Output**: '✓ Screenshot saved: PATH' after writing a PNG. Default path auto-generated with timestamp.

**GOTCHA**: requires mss or Pillow installed, else falls back to OS tools (scrot on Linux, screencapture on macOS). On headless machines this tool CANNOT work — no X display.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `output_path` | string | — | Optional path where the PNG will be saved (defaults to auto-generated path in working directory) |
| `region` | object | — | Optional region to crop (x, y, w, h coordinates) |

### `web_screenshot`

Capture a PNG screenshot of a web page via Playwright (preferred) or CLI fallback (wkhtmltoimage/cutycapt).

**Use when**: capturing visual state of a web page, generating thumbnails for a report, documenting UI.

**Not for**: desktop screenshots (use desktop_screenshot). Not for screenshots of running preview dev servers inside the repo — use browser MCP with a session instead.

**Output**: file path + size + viewport dimensions. The PNG lives at output_path (auto-generated in /tmp if unset).

**GOTCHA**: requires Playwright installed (pip install playwright && playwright install chromium) — else falls back to CLI tools which may not be available. Default viewport 1280x720; `full_page=true` captures the full scroll.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `url` | string | ✓ | The URL to screenshot |
| `output_path` | string | — | File path to save the screenshot (default: auto-generated in workspace) |
| `full_page` | boolean | — | Capture the full scrollable page (default: false, viewport only) |
| `width` | integer | — | Viewport width in pixels (default: 1280) |
| `height` | integer | — | Viewport height in pixels (default: 720) |

---

## HTTP Client

_Capability skill: `http-client` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `http_request`

Make any HTTP request (GET/POST/PUT/DELETE/PATCH) with custom headers, JSON body, and timeout.

**Use when**: calling a REST API, hitting a webhook, testing an endpoint, anything that needs status code + response headers visible.

**Not for**: plain text page fetches (use web_fetch — it strips HTML to text). Not for MCP-bound APIs (use mcp_call — it adds auth from the binding).

**Output**: 'HTTP status METHOD URL' + headers (first 20) + body (capped at MAX_HTTP_RESPONSE_CHARS).

**GOTCHA**: pass request bodies as json_body (dict) — it auto-sets Content-Type. Using `body` (string) requires you to set Content-Type manually. Max timeout 120s.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `url` | string | ✓ | The URL to request |
| `method` | string | — | HTTP method: GET, POST, PUT, DELETE, PATCH (default: GET) |
| `headers` | object | — | Request headers as key-value pairs |
| `body` | string | — | Request body (string or JSON string) |
| `json_body` | object | — | Request body as JSON object (auto-sets Content-Type) |
| `timeout` | integer | — | Request timeout in seconds (default: 30) |

---

## Admin Ops

_Capability skill: `admin-ops` — agent must have this granted (or it's in the global default capability set) for these tools to ship in tools[]._

### `pip_install`

Install or upgrade Python packages via pip (uses --break-system-packages for system Python).

**Use when**: a specific package is missing for a downstream tool (e.g. pptx auto-install already calls this internally; explicit use when you know exactly which package is needed).

**Not for**: generic shell commands (use bash). Not for non-Python deps (use bash with apt/brew).

**Output**: '✓ Successfully installed: names' on success or pip's stderr on failure. Max timeout 300s.

**GOTCHA**: writes to the agent's system Python — affects ALL agents on this node, not just you. Prefer local venv / uv install for reversible installs.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `packages` | string | ✓ | Space-separated package names to install (e.g., 'requests numpy pandas') |
| `upgrade` | boolean | — | Whether to upgrade packages to the latest version (default: false) |

### `propose_skill`

Scan the experience library for clusterable patterns and auto-generate a skill draft (SKILL.md + manifest.yaml) pending admin approval.

**Use when**: you have accumulated 3+ similar high-success experiences on a specific topic and want to promote them into a reusable skill package.

**Not for**: submitting a hand-written skill package (use submit_skill). Not for viewing existing skills (use get_skill_guide).

**Output**: draft summary with id / description / confidence / export directory / status=pending-approval. The draft is visible to admins in the Portal review queue.

**GOTCHA**: requires >=3 similar experiences with >=75% success rate — returns a 'no patterns found' message if the bar is not met. Drafts need ADMIN APPROVAL before they become real skills — do not assume the skill exists right after calling.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `role` | string | — | Limit scan to experiences of this role (empty = all roles) |
| `topic` | string | — | Optional topic hint to guide which experience cluster to target |

### `request_web_login`

Show the user an interactive login card to authenticate into a specific website before the agent proceeds.

**Use when**: you know upfront the task requires login (e.g. 'help me look at that Jira issue') and want to get credentials/cookies BEFORE navigating.

**Not for**: reactive login walls hit during browsing — those are handled automatically by the browser layer. Not for API key configuration (use the account settings UI).

**Output**: interactive card rendered in the chat; user completes login, then the agent can proceed.

**GOTCHA**: provide a clear `reason` — the card asks the user to trust you with credentials, and an opaque 'I need to log in' message is often declined.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `url` | string | ✓ | The URL that requires login |
| `site_name` | string | ✓ | Human-readable site name, e.g. 'GitHub', 'Jira', '企业微信' |
| `reason` | string | ✓ | Why you need access — what task requires this login |
| `login_url` | string | — | Optional: the specific login page URL if different from the target URL |

---

## Coordination & Project Primitives (allow-list opt-in)

### `accept_task`

Receiver-side tool: pop a task assignment from your inbox and get its structured brief. Without ta_id, returns the highest-priority pending task. Renders the assignment as a concise markdown brief — read ONLY the listed context_refs (no glob/search), produce ALL listed deliverables.

**Use when**: starting your turn and you have inbox assignments. Always preferred over reading 'task 派发' markdown files (those are deprecated).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `ta_id` | string | — | Specific assignment id (optional — defaults to highest-priority pending). |

### `agent_todo`

Maintain YOUR OWN private todo list across the next few turns. In-memory only (not persisted across process restarts). Cap 20 items.

**Use when**: you're juggling multiple sub-tasks within one assignment and want to remember progress across turns; before context-compaction events; whenever you'd otherwise paragraph-write 'I still need to do A, then B, then C'.

**Not for**: project-level steps (use plan_update). Not for milestones (use create_milestone). Not for tasks assigned to other agents (use dispatch_task). NEVER use it as a chat reply substitute — emit text in your reply too.

**Output**: formatted list with status icons (○ pending · ◐ in_progress · ● completed) and ids.

**GOTCHA**: at most ONE item may be in_progress at a time — setting a second errors out. Use action='set' for the initial plan or major pivot, action='update_one' for routine status flips (cheaper, doesn't re-emit the whole list).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `action` | string | — | get \| set \| update_one \| clear (default 'get'). |
| `todos` | array | — | For action='set': the FULL replacement list (max 20). Each item has fields below. |
| `todo_id` | string | — | For action='update_one': which item's status to change. |
| `status` | string | — | For action='update_one': new status (pending \| in_progress \| completed). |

### `bash_kill`

Terminate a background bash process started via bash(run_in_background=true). SIGTERM first, SIGKILL if it doesn't exit within 2s.

**Use when**: cleaning up a dev server / watcher you started and no longer need; or when a background process is misbehaving / wrong-config and needs a restart.

**Not for**: foreground commands (they always exit on their own). Not as a way to abort a still-running unrelated agent task — that's the host's responsibility.

**Output**: ⏹ confirmation with final exit code. Idempotent — calling on an already-finished pid returns its final status without erroring.

**GOTCHA**: process_id must be one returned by an earlier bash(run_in_background=true).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `process_id` | integer | ✓ | The pid returned by bash(run_in_background=true). |

### `bash_logs`

Pull recent log lines from a background bash process started via bash(run_in_background=true).

**Use when**: you started a dev server / long-running command in the background and want to see what it's printing. Repeated calls return the latest tail (no offset / no incremental cursor — just the last N lines).

**Not for**: foreground commands (their output is already in the bash result). Not as a polling loop substitute for waiting — if you just want to wait for a server to be ready, prefer one or two calls separated by other work.

**Output**: status line (running / exited with code) + last N log lines. Records GC'd ~1 hour after exit.

**GOTCHA**: process_id must be one returned by an earlier bash(run_in_background=true). Stale or wrong pids return an error.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `process_id` | integer | ✓ | The pid returned by bash(run_in_background=true). |
| `lines` | integer | — | How many log lines to return (default 30, max 500). |

### `bootstrap_project`

Atomic project skeleton creation: declare folder layout + acceptance, create N milestones, create N goals, dispatch N initial tasks — all in ONE call.

**Use when**: PM (or orchestrator role) is starting a new project and wants to set up the full structure in one shot. Replaces the typical 15+ atomic call ritual (define_project_blueprint + create_milestone × N + create_goal × N + dispatch_task × N).

**Not for**: incremental project edits mid-way through (use the singular create_milestone / create_goal / dispatch_task tools to avoid disturbing existing structure). Not for solo agents — needs a project context.

**Output**: 🚀 header + ✅ per-section confirmations (blueprint registered / N milestones / N goals / N tasks) + ⚠️ partial-failure list.

**GOTCHA**: each list section is independent — passing only goals (or only tasks) works. Per-list-item failures are reported but don't abort the rest. assigned_to in tasks is REQUIRED per task (look up agent_id from the team list at the top of your prompt). dispatch_task auto-fires the @-mention notification path so assigned agents pick up work immediately.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `project_id` | string | ✓ | Project to bootstrap (REQUIRED). |
| `blueprint` | object | — | Optional blueprint dict — keys: folders, acceptance, no_glob_in_chat, tool_budget_per_turn (see define_project_blueprint). |
| `milestones` | array | — | List of milestone specs. |
| `goals` | array | — | List of goal specs. |
| `tasks` | array | — | List of task-dispatch specs. |
| `revision_note` | string | — | Audit-trail note (forwarded to define_project_blueprint). |

### `define_project_blueprint`

PM one-shot configurator: declare folder layout, milestone acceptance, and anti-pattern rules — framework auto-generates engine rules to enforce.

**Use when**: starting a new project, restructuring an existing one, or codifying team conventions. Replaces hand-authoring N rules in the Settings → Rule Engine UI.

**GOTCHA**: re-running with the same project_id REPLACES the prior blueprint's rules (idempotent). Admin-authored rules in the Settings UI are untouched. Only PM/admin/executive role can call — workers can't redefine their own constraints.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `project_id` | string | ✓ | The project this blueprint applies to. |
| `folders` | array | — | Per-folder rules. Each item: {path, writers (list of role-name like 'coder-小新' or '*'), purpose}. Generated as before_file_write rules. |
| `acceptance` | array | — | Per-milestone acceptance criteria. Each item: {milestone_id, must_have_files (list of relative paths)}. Generated as before_task_done deny rules. |
| `no_glob_in_chat` | boolean | — | Generate a warn rule discouraging glob_files / search_files in this project's chat (default: true). |
| `tool_budget_per_turn` | integer | — | Advisory cap noted in blueprint description (informational). |
| `revision_note` | string | — | Why you made this change (audit trail). |

### `dispatch_task`

PM-side tool: hand a structured task to another agent. Replaces 'write 任务派发_X.md to shared workspace' pattern with a typed object (brief + context_refs + deliverables) the receiver can consume directly without parsing markdown.

**Use when**: you (as PM/coordinator) need to assign work to another specific agent. The receiver will see this in their inbox the next time they call accept_task or inbox_assignments.

REQUIRED: brief (≤500 chars, 1-3 sentences) + at least one deliverable (path + must_contain). Without a contract the receiver can't verify completion.

DELIVERABLE FORMAT: each entry is {path: 'src/foo.py', kind: 'code'|'doc'|'data', must_contain: ['def main', 'import x'], min_lines: 20, max_lines: 0, acceptance_cmd: 'pytest tests/foo.py'}. must_contain prefixed with 're:' is treated as regex.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `to_agent` | string | ✓ | Recipient agent_id. |
| `brief` | string | ✓ | 1-3 sentences (≤500 chars). What and why; no how. |
| `context_refs` | array | — | List of [{path, why_relevant, expected_section}]. These are the ONLY files the receiver should read — pin specific files instead of letting them search. |
| `deliverables` | array | ✓ | List of [{path, kind, must_contain[], min_lines, max_lines, acceptance_cmd}]. Required — at least 1 entry. |
| `project_id` | string | — | Project this task belongs to (auto from scope if omitted). |
| `project_task_id` | string | — | Optional ProjectTask.id link. |
| `priority` | integer | — | 0 normal, 1 high, 2 urgent. |
| `deadline` | string | — | Optional ISO timestamp or epoch seconds. |

### `finalize_step`

Atomic step closure: register one-or-more local files as project deliverables, close the plan step, optionally mark a milestone done — all in ONE call.

**Use when**: you just finished writing code/docs (write_file × N) and want to close out the step. Replaces the typical bash cp + submit_deliverable × N + plan_update + update_milestone_status ritual.

**Not for**: mid-step interim saves (use submit_deliverable singly). Not for closing a step you haven't actually completed work on (acceptance still applies). Not in solo mode without a project context.

**Output**: ✅ lines per registered deliverable + step / milestone confirmation, or ⚠️ list of items that failed (partial success is reported, not aborted).

**GOTCHA**: each file's local_path can be in your agent workspace — submit_deliverable copies it into the project shared dir automatically, no need to bash cp first. Per-file kind defaults to 'code'. step_id and milestone_id are optional; supply step_id when you want to close the plan step in the same call.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `files` | array | ✓ | List of file specs to register as deliverables. Each item: {local_path (REQUIRED, abs path), title? (default basename), kind? (default 'code'), milestone_id? (per-file override of top-level milestone_id)}. |
| `step_id` | string | — | Plan step id to close on success. Empty = skip plan_update; only register the deliverables. |
| `milestone_id` | string | — | Optional milestone to mark done after deliverables register. Empty = skip update_milestone_status. |
| `step_summary` | string | — | Short one-liner stamped on the closed step / milestone evidence (auto-built from titles when empty). |
| `project_id` | string | — | Project id (optional; inferred from chat context). |

### `inbox_assignments`

List structured task assignments waiting in your inbox. Different from check_inbox (which is chat messages). Use when looking for work-to-do.

### `init_project_context`

Generate (or refresh) the project's PROJECT_CONTEXT.md file in shared/<project_id>/. Spawns an init subagent that explores the directory, reads README/manifests/entry points, queries project_state, and writes a structured doc. Idempotent — re-running with force=False returns the existing path without regenerating.

**Use when**: starting work on a new project (the very first turn) and you want every future agent in this project to skip the rediscovery cost; or when project structure changed enough that the existing PROJECT_CONTEXT.md is stale (force=true).

**Not for**: documenting individual deliverables (use submit_deliverable). Not for changing project metadata (use update_milestone_status / create_goal). Not for solo agents without a project context.

**Output**: ✅ confirmation with target path + size + elapsed; or ⚠️ if subagent ran but didn't write the file; or Error if it failed / timed out.

**GOTCHA**: this spawns a subagent so it takes 30-180s typically — don't call it inside a tight loop. Subagent has write_file permission BUT scoped to a curated whitelist (no submit_deliverable / no dispatch_task). Default timeout 300s.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `project_id` | string | ✓ | Project to initialise (REQUIRED). |
| `force` | boolean | — | Overwrite existing PROJECT_CONTEXT.md if present (default false). |
| `timeout_s` | integer | — | Caller-side wait timeout in seconds (default 300, clamped 60-900). |
| `extra_focus` | string | — | Optional free-text instructing the init subagent to pay extra attention to a specific area (e.g. 'focus on the auth flow'). |

### `list_issues`

List project issues filtered by status. Defaults to 'open'. Pass status='all' for everything. Use when: starting a turn and want to see what's blocking the team.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `status` | string | — | open (default) \| investigating \| resolved \| wontfix \| all |
| `project_id` | string | — | Project id (auto from scope) |

### `mcp_call`

🎯 作用对象: **外部服务**(真实邮箱、Slack 工作区、GitHub、数据库、浏览器、第三方 API 等)。**绝对不是**同进程的 agent 同事 —— 想给小刚/小专这种系统内 agent 派活,用 send_message / @ 提及,不要走这里。

Invoke a tool on an external MCP server bound to this agent.

**Use when**: sending emails to **real email addresses** / posting to Slack workspaces / hitting third-party APIs / driving a browser / etc.

**Not for**: builtin tools above (call them directly). Not for talking to teammates inside this system. Not for discovering MCPs — pass list_mcps=true first to enumerate what's bound, then call with mcp_id + tool.

**Output**: raw MCP tool response (JSON or text depending on the server).

**GOTCHA**: `arguments` must be a JSON object — not a string. If you don't know what MCPs are available, call with list_mcps=true BEFORE guessing mcp_id. Errors include the MCP server name — check it's bound.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `mcp_id` | string | — | The bound MCP id or name (e.g. 'email', 'slack', 'github') |
| `tool` | string | — | The MCP tool name to invoke (e.g. 'send_email', 'send_message') |
| `arguments` | object | — | Arguments object to pass to the MCP tool. A JSON-encoded string is also accepted and auto-parsed. |
| `list_mcps` | boolean | — | If true, list bound MCPs instead of calling one |

### `project_state`

Snapshot of structured project state — replaces glob_files for status checks.

**Use when**: you want to know what's done / what's yours / what blocks you. ALWAYS prefer this over scanning files with glob_files / search_files when you're inside a project chat — structured stores (Milestone, Deliverable, ProjectTask) are the source of truth, the filesystem is just artifacts.
scope:
  - 'my' (default): your role, your active task, your milestones, what blocks you
  - 'team': cross-team workflow %, who's in progress, open issues
  - 'step': details of one workflow step (requires step_id, partial-prefix accepted)
  - 'milestone': details of one milestone (requires milestone_id)
  - 'all': verbose dump (debugging only)

**GOTCHA**: scope='my' needs the dispatcher to inject _caller_agent_id — works automatically when called from chat.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `scope` | string | — | my (default) \| team \| step \| milestone \| all |
| `project_id` | string | ✓ | Project id. Required: agents may belong to multiple projects, framework can't always infer. |
| `step_id` | string | — | Required when scope='step'. Workflow step task id (partial prefix OK). |
| `milestone_id` | string | — | Required when scope='milestone'. Milestone id (partial prefix OK). |

### `propose_decomposition`

Propose a decomposition of the CURRENT project task into N parallel sub-tasks for multiple agents.

**Use when**: the task is too big for one agent (multi-module code project, multi-chapter report) and can be cleanly split.
DOES NOT immediately create or assign tasks — it persists a draft. The user must confirm in the UI before any sub-task gets dispatched. After calling this, STOP and tell the user to confirm; do not start any sub-task work yourself.

**Output**: draft_id + sub_task_count. Each sub_task should have title, role_hint (coder/researcher/general/advisor), output_path (relative to project root), acceptance criteria, and depends_on (list of earlier sub_task ids).

**GOTCHA**: parent_task_id is required and must point to the big task in the current project. Use unique sub_task ids if you specify them; otherwise leave blank (auto-minted). depends_on entries must reference sub_task ids in the same proposal.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `parent_task_id` | string | ✓ | ProjectTask id being decomposed |
| `title` | string | — | Short label, e.g. 'Decompose: build admin panel' |
| `summary` | string | — | Plain-language pitch of the strategy |
| `prd` | string | — | Optional PRD content (markdown). Required when prd_source=agent_generated. Leave empty to use whatever PRD.md the user already uploaded. |
| `scaffold_dirs` | array | — | Directories to mkdir under project root before sub-tasks start (e.g., ['backend/auth', 'frontend/pages']) |
| `sub_tasks` | array | ✓ |  |

### `query_agent_status`

Get one agent's current action — what task they're on and what they last reported. Useful for checking on a specific worker before re-dispatching.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `agent_id` | string | ✓ | Target agent_id. |
| `project_id` | string | — | Restrict to one project (optional). |

### `query_team_status`

List the current activity of every agent in a project — what they're doing, on what task, and how long ago they last reported progress. Reads from a single SQLite table (no LLM cost, no agent introspection).

**Use when**: you (as PM) are about to dispatch_task and want to see who's idle vs busy. Or when checking team health.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `project_id` | string | — | Project ID (auto from current scope if omitted). |

### `report_back`

Coder-side: report task completion (or blocker) back to the PM. Closes the dispatch loop — PM gets a structured chat message in their inbox. Auto-marks the assignment as done/cancelled in SQLite. If you don't pass ta_id, uses your most recently accepted task.

**Use when**: you finished (or got blocked on) a task you previously accepted via accept_task. Always preferred over a vague 'I'm done' message — this keeps the dispatch table consistent.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `ta_id` | string | — | TaskAssignment id. Optional — defaults to your most recent accepted. |
| `status` | string | ✓ | done \| blocked \| needs_clarification \| cancelled |
| `summary` | string | — | 1-3 sentences describing what you did / what blocked you. |
| `actual_deliverables` | array | — | List of paths actually produced (relative to workspace). |
| `blocker` | string | — | If status=blocked, what's blocking you. |

### `report_issue`

Report a project issue / risk / blocker. Surfaces in the project's Issues tab AND posts a notice to project chat.

**Use when**: you hit a blocker that needs human/PM attention (missing API key, upstream incomplete, repeated failure, ambiguous requirements).
DON'T USE for casual 'I'm slow' — those go in chat. Issues are for things that need explicit status tracking (open → resolved).
Severity: low (FYI) / medium (slows you) / high (blocks delivery) / critical (blocks the whole project).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `title` | string | ✓ | 1-line summary (≤200 chars) |
| `description` | string | — | Details: what happened, what you tried, what you need |
| `severity` | string | — | low \| medium \| high \| critical (default: medium) |
| `related_task_id` | string | — | Optional ProjectTask id this issue is about |
| `related_milestone_id` | string | — | Optional milestone id |
| `project_id` | string | — | Project id (auto from scope if omitted) |

### `sc_get_artifact`

Fetch the full record (path / title / summary / token_count / creator) of an artifact by its ``art_*`` id. Use this when you got an artifact id from sc_query or a handoff and need to know what it points to before deciding whether to read the underlying file.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `artifact_id` | string | ✓ | Artifact id, e.g. art_a1b2c3d4 |

### `sc_handoff`

PULL-MODEL handoff to another agent — writes a row to the handoffs table; the destination agent discovers it via sc_query(table='handoffs', dst_agent='self', status='pending'). Token cost ~0 vs. /handoff which copies content into dst's messages.
Pass artifact_refs as a list of art_* ids the receiver will need (don't paste file content). Summary should be ≤300 chars — point, don't narrate.
Returns the handoff id.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `dst_agent` | string | ✓ | Receiver agent id (or name — resolver tolerates both) |
| `intent` | string | ✓ | What the receiver should do, ≤500 chars |
| `summary` | string | — | Optional 1-2 line context, ≤300 chars |
| `artifact_refs` | array | — | List of art_* ids the receiver will need. AVOID pasting file content here. |
| `project_id` | string | — | Optional; inferred from chat context |

### `sc_query`

Query the project's shared context database. Token-cheap alternative to dumping content through chat — pull just the rows you need.
Tables: artifacts (file refs with summary), decisions (structured choices made), milestones (project goals/phases), handoffs (agent→agent assignments), pending_qs (open Q&A). Pass table='summary' (or omit) to get a compact project state overview.
Filters apply per-table: kind+status for artifacts, status for decisions/milestones, dst_agent+status for handoffs/pending_qs. Returns JSON {table, count, rows}.
Use this BEFORE asking another agent — the answer may already exist.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `table` | string | — |  |
| `kind` | string | — | Filter (artifacts only): document \| code \| data \| image \| report \| config |
| `status` | string | — | Filter by row status (varies by table) |
| `dst_agent` | string | — | Filter handoffs/pending_qs by destination agent |
| `since_ts` | number | — | Unix ts; only return rows newer than this |
| `limit` | integer | — | Max rows (default 10, capped at 50) |
| `project_id` | string | — | Optional; inferred from chat context |

### `sc_record_decision`

Append a structured decision to the project's decision log. Use this for team-wide decisions that other agents need to respect (e.g. 'choose AWS over Azure', 'use REST not GraphQL', 'PPT 用蓝色风格'). Once recorded, future tasks see it in their shared-context summary and via sc_query(table='decisions').
DON'T use for: per-task internal choices, ephemeral preferences. DO supersede an old decision when overriding — pass supersedes_id.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `topic` | string | ✓ | What was being decided |
| `decision` | string | ✓ | The chosen answer |
| `rationale` | string | — | Why this choice (optional but recommended) |
| `supersedes_id` | string | — | If overriding a prior decision, pass its dec_* id |
| `project_id` | string | — | Optional; inferred from chat context |

### `sc_register_artifact`

Record a workspace file as a sharable artifact reference so other agents can find it via sc_query(table='artifacts'). ONLY register the *card* (path + title + ≤200 char summary); the full file stays in the workspace.

**Use when**: you produced a file other agents will need (a document draft, code module, data table, image). DON'T use for ephemeral intermediates.

**Output**: ``OK · registered artifact id=art_*`` — pass that id to sc_handoff or include in your text reply.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `path` | string | ✓ | Workspace-relative file path |
| `title` | string | — | Human-readable title (defaults to path) |
| `summary` | string | — | ≤200 char content preview |
| `kind` | string | — |  |
| `token_count` | integer | — | Approximate token count of the full file (helps consumers budget) |
| `project_id` | string | — | Optional; inferred from chat context |

### `spawn_explore_subagent`

Spawn a stateless ephemeral subagent to handle a focused READ-ONLY exploration / research task. The subagent runs its own chat loop with its own budget; you get back its final reply as a single string. The subagent's intermediate tool calls and reasoning DO NOT enter your context — your prefix cache stays clean.

**Use when**: you'd otherwise spend 10+ tool calls reading / searching / web-fetching just to ANSWER a sub-question. Examples: 'find which file declares the auth middleware', 'survey the existing test framework', 'compile a list of competitor pricing pages'. Especially valuable for orchestrator-role agents (PM / executive) who shouldn't burn their budget on discovery.

**Not for**: writing code / dispatching tasks / submitting deliverables (those are mutations — do them yourself in your context). Not for sub-questions you can answer with one read_file or one project_state call (the spawn overhead isn't worth it).

**Output**: subagent's final assistant text, prefixed with a metadata header showing tool calls used and elapsed time. Errors come back as 'Error: ...' so you can decide whether to retry / handle yourself.

**GOTCHA**: depth limit (default 3) prevents recursive forking. read_only_tools=true (default) restricts subagent to read-only primitives — set false ONLY if you specifically need a writing fork. Subagent shares your model/provider/working_dir/shared_workspace; doesn't share message history.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `prompt` | string | ✓ | The task for the subagent. Be specific and bounded — 'find which file defines auth middleware and list its public exports' beats 'explore the auth code'. |
| `return_format` | string | — | summary (≤500 chars, default) \| full \| list. Hint to the subagent on how to shape its reply. |
| `read_only_tools` | boolean | — | Restrict subagent's tools to read-only primitives (default true). Set false only if you specifically need a writing fork. |
| `timeout_s` | integer | — | Caller-side wait timeout in seconds (default 180, clamped 10-600). |
| `role` | string | — | Optional role hint (default: inherit parent role). Affects role-preset tool defaults when read_only_tools=false. |

### `submit_review`

Atomic milestone-review closure: register the review report as a deliverable, batch-file any issues found, transition the milestone status in ONE call.

**Use when**: you (typically a reviewer-role agent) finished evaluating a milestone's deliverables and want to close out the review. Replaces the typical read_file × N + write report + submit_deliverable + report_issue × M + update_milestone_status ritual.

**Not for**: filing a single ad-hoc bug not tied to a milestone (use report_issue). Not for in-progress drafts of the review (wait until decision is final). Not in solo mode without a project context.

**Output**: 📋 decision summary line + ✅ confirmations per sub-step (review report registered / issues filed / milestone transitioned), with ⚠️ list of any partial failures.

**GOTCHA**: decision controls the milestone target status — approve→done, request_changes→blocked, reject→cancelled. issues are batch-filed AND auto-linked to this milestone (no need to set milestone_id on each one). If you provide deliverable_content without deliverable_path, the content is materialised into the project shared dir automatically (submit_deliverable handles the write+copy).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `milestone_id` | string | ✓ | The milestone being reviewed (REQUIRED). |
| `decision` | string | ✓ | Decision: approve \| request_changes \| reject. Maps to milestone status done / blocked / cancelled. |
| `summary` | string | — | Short review summary (≤200 chars). Stamped on the milestone evidence; used as deliverable title fallback. |
| `issues` | array | — | Optional list of issues to file alongside the review. |
| `deliverable_path` | string | — | Optional path to a pre-written review report (registered as kind='analysis'). |
| `deliverable_title` | string | — | Title for the review-report deliverable. Defaults to 'Review · <milestone_id>'. |
| `deliverable_content` | string | — | Inline review report content; written into the shared dir if no deliverable_path given. |
| `project_id` | string | — | Project id (optional; inferred from chat context). |

### `submit_skill`

Submit a hand-written skill package from your workspace directory for admin approval. Requires manifest.yaml + SKILL.md in the dir.

**Use when**: you have authored a skill (e.g. via writing files in your workspace) and want it reviewed for inclusion in the Skill Store.

**Not for**: auto-proposing from experiences (use propose_skill). Not for installing a granted skill — skills appear automatically after admin approval.

**Output**: draft id + name + runtime + code files list + 'awaiting admin approval' status.

**GOTCHA**: manifest.yaml MUST include name/version/description/runtime/author/entry. runtime must be one of python/shell/markdown. Same name+version combo is rejected — bump version to resubmit. Python skills' entry file must define `def run(ctx, **kwargs)` and cannot use open/exec/eval (sandbox forbids).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `dir_name` | string | ✓ | Name of the skill directory in your workspace (e.g. 'pptx_skill') |

### `update_issue`

Update an existing issue — change status / add resolution / reassign / change severity. Common: open → investigating (you picked it up); investigating → resolved (with resolution text); → wontfix (won't address).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `issue_id` | string | ✓ | Issue id |
| `status` | string | — | open \| investigating \| resolved \| wontfix |
| `resolution` | string | — | What was done to resolve (required when status=resolved) |
| `severity` | string | — | Override severity |
| `assigned_to` | string | — | Reassign to another agent_id |
| `project_id` | string | — | Project id (auto from scope) |

### `update_milestone_responsibility`

Reassign an existing milestone to a different agent AND auto-notify them.

**Use when**: redistributing work after the initial create_milestone — e.g. user asks '把模块④ 从小刚移给小专,小专更熟悉行业需求'.

**Not for**: creating new milestones (use create_milestone). Not for status updates (use update_milestone_status).

⭐ Effect:
  - Updates milestone.responsible_agent_id to the new owner.
  - AUTO-FIRES a chat message to the new owner: '@<new owner> 你接手了里程碑「X」...',
    AND triggers them to start working on it (same delegation path as create_milestone
    with responsible_agent_id of another agent).
  - Also posts a courtesy notice to the old owner so they know they no longer own it
    (skip via notify_old=false if that adds noise).

Get teammate ids from the [项目群聊] team list at the top of your prompt: each line shows
`<role>-<name> [id=<agent_id>]: <responsibility>` — copy the id= value into new_responsible_agent_id.

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `milestone_id` | string | ✓ | The milestone id to reassign |
| `new_responsible_agent_id` | string | ✓ | Agent id of the NEW responsible owner (look up in the team list). |
| `reason` | string | — | One-line reason for the reassignment (shown in both the new-owner trigger message and the old-owner release notice). Helps the recipients understand context. Optional but recommended. |
| `notify_old` | boolean | — | Whether to send a courtesy notice to the previous responsible. Default true. Set false when self-reassigning or when noise is unwanted. |
| `project_id` | string | — | Project id (optional; inferred from chat context) |

### `wiki_ingest`

★ PRIMARY tool for saving any reusable knowledge / experience / methodology / template the agent learns or distills. Writes a markdown page to the wiki layer (auto-indexed, queryable via knowledge_lookup, injected into future role prompts as a title-only index). kinds: experience (scene + rules) | methodology (workflow / steps) | template (writing pattern) | pattern (recurring logic) | reference (specs / wiki). scope: omit for role-scoped; pass scope='global' for cross-role sharing. Use this INSTEAD of save_experience (deprecated).

**Parameters**:

| name | type | required | description |
|------|------|----------|-------------|
| `kind` | string | ✓ | Page kind. experience=lessons; methodology=how-tos; template=writing/structure; pattern=recurring logic; reference=specs/standards. |
| `title` | string | ✓ | Human-readable title (used for slug + index). |
| `body` | string | ✓ | Full markdown body. Self-contained — readers won't have other context. |
| `tags` | array | — | Optional tags for search. |
| `scope` | string | — | Empty (default) → role-scoped; 'global' → shared across roles. |
| `sources` | array | — | Optional: source paths/URLs that informed this page. |
| `related` | array | — | Optional: related page slugs (e.g. 'experience/saudi-cloud'). |

---

## Where to look

- Definitions: `app/tools.py` (`TOOL_DEFINITIONS` at module top, dispatch table `_TOOL_FUNCS`)
- Per-tool implementations: `app/tools_split/<category>.py`
- Composite tools: `app/tools_split/finalize.py`
- Subagent / init: `app/tools_split/subagent.py` · `app/tools_split/project_init.py`
- TodoWrite: `app/tools_split/agent_todo.py`
- Bash background: `app/tools_split/system.py`
- Capability gating: `app/tool_capabilities.py`
- Schema layer: `app/core/prompt_schemas.py`
