---
name: safe-artifact-paths
description: Use whenever a tool produces a file (screenshot, PDF, recording, download, generated asset). All artifacts MUST land under $AGENT_WORKSPACE before being reported as deliverables. TudouClaw's sandbox rejects paths outside deliverable_dir — this skill prevents that failure at the source.
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# Safe Artifact Paths — 产出物必须落在工作区

## Why this exists

TudouClaw enforces a strict sandbox on deliverables. If an agent reports a file at a path **outside** its agent workspace, the deliverable endpoint returns:

```
403 {"detail":"path outside deliverable_dir"}
```

This is intentional — it prevents agents from advertising files they can't actually access, files that leak across agents, or files that vanish when a temp dir is cleaned.

**The rule is non-negotiable: every file you claim as output MUST live under `${AGENT_WORKSPACE}`.**

## The Iron Rule

> **If a tool writes a file, and you intend to treat that file as a deliverable / result / attachment, the file's final path MUST start with `${AGENT_WORKSPACE}/`.**

`$AGENT_WORKSPACE` is set on your environment. It points to:

```
~/.tudou_claw/workspaces/agents/<your-agent-id>/workspace
```

Anything outside that — `~/.agent-browser/tmp/`, `/tmp/`, `~/Downloads/`, `/var/folders/...` — is **invisible to the deliverable system**, even if the file physically exists.

## When tools write to fixed external paths

Many MCP tools, CLIs, and libraries dump output to a hardcoded cache directory:

| Tool | Default output dir |
|------|--------------------|
| `npx agent-browser screenshot` (Bash) | `~/.agent-browser/tmp/screenshots/` |
| `npx agent-browser pdf` (Bash, no `-o`) | `~/.agent-browser/tmp/` |
| Some Playwright wrappers | `~/.cache/playwright/` |
| macOS screencapture | `~/Desktop/` |
| yt-dlp default | current working dir |
| pandoc (no -o) | stdout only |

These paths are **NOT** inside your workspace. If you stop there, the file is effectively lost.

### The copy-then-report pattern (required)

```bash
# 1. Tool runs, dumps to its default path
npx agent-browser screenshot   # → ~/.agent-browser/tmp/screenshots/screenshot-XXX.png

# 2. IMMEDIATELY copy into workspace before anything else
mkdir -p "$AGENT_WORKSPACE/screenshots"
cp ~/.agent-browser/tmp/screenshots/screenshot-*.png \
   "$AGENT_WORKSPACE/screenshots/site-home.png"

# 3. Now report the workspace path
echo "截图已保存: $AGENT_WORKSPACE/screenshots/site-home.png"
```

### The specify-output-path pattern (preferred when supported)

If the tool accepts an output path argument, always use it:

```bash
# GOOD — write directly into workspace
mkdir -p "$AGENT_WORKSPACE/pdfs"
npx agent-browser pdf "$AGENT_WORKSPACE/pdfs/report.pdf"

# BAD — let it default somewhere else
npx agent-browser pdf            # where did it go? (most likely ~/.agent-browser/tmp/)
```

## Directory conventions

Organize artifacts by type under workspace:

```
$AGENT_WORKSPACE/
├── screenshots/        — PNG/JPEG screenshots
├── pdfs/              — generated PDF documents
├── recordings/        — video/audio captures
├── downloads/         — files downloaded from the web
├── generated/         — LLM/code-generated assets
└── logs/              — diagnostic logs
```

Create the subdir on first write (`mkdir -p`), don't crash if it already exists.

## Naming conventions

- Use **descriptive names**, not MCP-generated hashes: `alibaba-homepage.png` ✅ not `screenshot-1776488888595.png` ❌
- Include a timestamp when multiple captures over time: `site-20260418-1430.png`
- Keep names shell-safe: lowercase + hyphens, no spaces

## Common pitfalls

| Mistake | Consequence | Fix |
|---------|-------------|-----|
| Reporting `~/.agent-browser/tmp/screenshot-xxx.png` as deliverable | 403 path outside deliverable_dir | Copy into `$AGENT_WORKSPACE/screenshots/` first |
| Using `/tmp/` for intermediate files and forgetting to move | File gets garbage-collected | Stage under `$AGENT_WORKSPACE/tmp/`, clean up at end |
| Symlinking from workspace to external path | Sandbox rejects symlinks that resolve outside | Always **copy**, never symlink |
| Relying on absolute paths hardcoded in tool docs | Breaks on other users' machines | Use `$AGENT_WORKSPACE` env var |

## Quality gate (self-check before you finish)

Before declaring a task done with file outputs, run:

```bash
# All claimed deliverables exist and are inside workspace?
ls -la "$AGENT_WORKSPACE"/**/*.{png,pdf,mp4,zip} 2>/dev/null
```

If the expected file is **not** under `$AGENT_WORKSPACE`, you have broken the sandbox contract — fix it before reporting success.

## Interaction with other skills

Browser automation, PDF generation, screenshot tools, and any skill that produces binary artifacts should **cite this skill** and **apply these rules**. When authoring new skills that produce files, start the Workflow section with a reference to this skill.
