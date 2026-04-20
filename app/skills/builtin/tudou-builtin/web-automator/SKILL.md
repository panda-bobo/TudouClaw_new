---
name: web-automator
description: Use when the agent needs to drive a real browser — open a page, read DOM, click/fill/submit, take a screenshot, or save a page as PDF. Wraps the `agent-browser` Node CLI (invoked via Bash). Covers the accessibility-tree + ref-based workflow that makes scripted browsing reliable, and calls out two recurring pitfalls — (1) deliverables landing outside $AGENT_WORKSPACE and (2) multi-URL PDFs with identical pages.
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# Web Automator — 浏览器自动化（Bash + `agent-browser` CLI）

> **Prereq skill:** `safe-artifact-paths` — every file produced here (screenshots, PDFs, traces) MUST land under `$AGENT_WORKSPACE`. Read that skill first.

## What this tool actually is

`web-automator` is **not** a native TudouClaw tool and it is **not** an MCP server. It is the Node package **`agent-browser`** invoked via the Bash tool:

```bash
npx agent-browser <subcommand> [args...]
```

Under the hood it drives Chromium via Playwright. First run downloads the browser (tens of MB) to `~/.cache/ms-playwright/`.

**Verify availability once at the start of a task:**

```bash
command -v npx >/dev/null && npx agent-browser --help >/dev/null 2>&1 \
  && echo "agent-browser ready" \
  || echo "agent-browser unavailable — ask user to npm install -g agent-browser or fall back to a different approach"
```

If it is unavailable, say so plainly — do not pretend you ran it.

## The canonical workflow (open → snapshot -i → act via refs → re-snapshot)

This is the single most important pattern. Blind selector-driven automation fails on modern SPAs; the ref-based workflow is robust.

```bash
# 1. Open the page
npx agent-browser open https://example.com/form

# 2. Get INTERACTIVE elements only (-i). Each gets a stable ref like @e7.
npx agent-browser snapshot -i
#   → textbox "Email"   [ref=e1]
#     textbox "Password" [ref=e2]
#     button  "Submit"   [ref=e3]

# 3. Act on refs (never guess CSS selectors when you have refs)
npx agent-browser fill  @e1 "user@example.com"
npx agent-browser fill  @e2 "hunter2"
npx agent-browser click @e3

# 4. Wait for the transition to settle, THEN re-snapshot
npx agent-browser wait --load networkidle
npx agent-browser snapshot -i
```

Rules:

- Refs are stable **within a page view** and **invalidate on navigation or DOM churn**. Always re-snapshot after `click`/`wait`/`navigate`.
- Prefer `fill` over `type` — `fill` clears the field first.
- Prefer `--load networkidle` over arbitrary `sleep` N.

## Producing deliverables — screenshots

Default path (`~/.agent-browser/tmp/screenshots/…`) is **outside** the agent sandbox and will be rejected when reported. Two acceptable patterns:

### Pattern A — specify output path up-front (preferred)

```bash
mkdir -p "$AGENT_WORKSPACE/screenshots"
npx agent-browser screenshot --output "$AGENT_WORKSPACE/screenshots/home.png" --full-page
# Now report: "$AGENT_WORKSPACE/screenshots/home.png"
```

### Pattern B — copy-then-report

```bash
npx agent-browser screenshot --full-page
# → ~/.agent-browser/tmp/screenshots/screenshot-1729200000000.png
mkdir -p "$AGENT_WORKSPACE/screenshots"
cp ~/.agent-browser/tmp/screenshots/screenshot-*.png \
   "$AGENT_WORKSPACE/screenshots/home.png"
```

Never symlink; the sandbox rejects symlinks that resolve outside.

## Producing deliverables — PDFs (this section fixes a real recurring bug)

### The bug you must avoid

If you naively loop `npx agent-browser pdf` per URL **without re-navigating between calls**, every page of the resulting bundle ends up containing the **same page content** (whatever was loaded first). This has shipped from agents more than once.

### Root cause

`npx agent-browser pdf` captures **whatever is currently loaded in the browser session**. If you do not `open` (or `navigate`) to the next URL before calling `pdf` again, you re-capture the previous page.

### Correct procedure — one PDF per URL, then combine

```bash
mkdir -p "$AGENT_WORKSPACE/pdfs/_parts"
urls=(
  "https://example.com/page-1"
  "https://example.com/page-2"
  "https://example.com/page-3"
)

i=0
for url in "${urls[@]}"; do
  npx agent-browser open "$url"
  npx agent-browser wait --load networkidle    # DO NOT skip this
  out="$AGENT_WORKSPACE/pdfs/_parts/part-$(printf '%02d' "$i").pdf"
  npx agent-browser pdf "$out"
  i=$((i+1))
done

# Merge (requires pdftk / qpdf / ghostscript — pick one installed on the host)
if command -v qpdf >/dev/null; then
  qpdf --empty --pages "$AGENT_WORKSPACE/pdfs/_parts/"*.pdf -- \
       "$AGENT_WORKSPACE/pdfs/combined.pdf"
elif command -v pdftk >/dev/null; then
  pdftk "$AGENT_WORKSPACE/pdfs/_parts/"*.pdf cat output \
        "$AGENT_WORKSPACE/pdfs/combined.pdf"
else
  echo "No PDF merger installed; leaving per-URL PDFs under $AGENT_WORKSPACE/pdfs/_parts/"
fi
```

### Self-check before reporting a multi-URL PDF

```bash
# Quick sanity check: are the part files different sizes?
ls -la "$AGENT_WORKSPACE/pdfs/_parts/"*.pdf
# If every part is byte-identical, you hit the bug — re-run with explicit `open` between calls.
```

## Authenticated flows — save and reuse session state

```bash
# First run: login, then persist cookies + localStorage
npx agent-browser open https://app.example.com/login
npx agent-browser snapshot -i
npx agent-browser fill  @e1 "$USER_EMAIL"
npx agent-browser fill  @e2 "$USER_PASSWORD"
npx agent-browser click @e3
npx agent-browser wait --url "/dashboard"
npx agent-browser state save "$AGENT_WORKSPACE/.state/auth.json"

# Later runs: restore state, go straight to the authed page
npx agent-browser state load "$AGENT_WORKSPACE/.state/auth.json"
npx agent-browser open https://app.example.com/dashboard
```

Treat `auth.json` as a secret — keep it under workspace, do not echo its contents, do not attach it to user-visible output.

## Command surface (abridged)

The CLI exposes far more than listed here. The subcommands below are the ones you actually need 95% of the time; reach for `npx agent-browser --help` when a task genuinely needs more.

| Category | Commands you will use |
|----------|-----------------------|
| Navigate | `open <url>`, `back`, `forward`, `reload`, `close` |
| Inspect | `snapshot -i` (interactive-only, with refs), `snapshot --compact` |
| Act | `click @ref`, `fill @ref <text>`, `select @ref <value>`, `check @ref`, `hover @ref`, `scroll`, `upload @ref <path>` |
| Wait | `wait --load networkidle`, `wait --text "..."`, `wait --url <pattern>`, `wait --selector <css>` |
| Capture | `screenshot --output <path> [--full-page]`, `pdf <path>` |
| Session | `state save <path>`, `state load <path>` |
| JS | `eval "<js-expr>"` (use sparingly) |

## Common pitfalls (checklist before reporting results)

- [ ] Did you re-snapshot after every navigation / major DOM change?
- [ ] Did every screenshot and PDF land under `$AGENT_WORKSPACE/…`?
- [ ] For multi-URL PDFs: did you `open` + `wait` **between** each `pdf` call, and did the part files have different sizes?
- [ ] Did you `close` the browser at the end of the task to free resources?
- [ ] Are auth-state files kept inside `$AGENT_WORKSPACE/.state/` and never exposed to user output?

## When NOT to use this skill

- Pages behind aggressive anti-bot (Cloudflare Turnstile, DataDome, hCaptcha on every load) — document the failure rather than burn time.
- Sites whose ToS explicitly forbid automated access — ask the user before proceeding.
- Cases where a documented public API exists and returns the same data — use the API instead.

## Workflow summary

1. **Verify** `npx agent-browser --help` succeeds.
2. **Open** the target URL.
3. **Snapshot -i** to get refs.
4. **Act** via refs; **wait** for settle; **re-snapshot** after each navigation.
5. **Capture** outputs directly into `$AGENT_WORKSPACE/` (or copy immediately after).
6. **Close** the browser.
7. **Self-check** the artifact-path and PDF-uniqueness pitfalls before reporting.
