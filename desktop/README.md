# TudouClaw Desktop — 桌面悬浮 Agent (Mac MVP)

A floating, always-on-top widget that lives on your Mac desktop and gives one
of your TudouClaw agents an anthropomorphic, sci-fi presence. Click the
avatar to expand a card with the agent's name, persona, and a chat box.

Pairs with the FastAPI server in `app/` — talks to it on
`http://127.0.0.1:9090` (loopback only, no auth needed for the read side).

## Status (MVP scope)

| Feature | State |
|---|---|
| Frameless transparent always-on-top window | ✅ |
| Default SVG avatar + CSS breathing animation | ✅ |
| Pulls agents from `/api/portal/agents/desktop` | ✅ |
| Click → expand card with name, persona, status | ✅ |
| Multi-agent picker (right-side dropdown) | ✅ |
| Send chat message (write side) | ⚠️ wired but needs JWT — Phase 3 |
| Receive streaming reply (SSE) | ⏳ Phase 3 |
| Lottie animation import | ⏳ Phase 4 |
| Multi-window (one per agent) | ⏳ Phase 4 |

## Toolchain

- macOS 11+
- Rust stable (`rustup` installs it)
- The FastAPI server (`python -m app` from repo root) running locally

You do **not** need Node — `cargo tauri` handles everything.

## First-time setup

```bash
# 1. Install Rust if you haven't:
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# 2. Install the Tauri 2 CLI:
cargo install tauri-cli --version "^2.0"

# 3. Sanity-check the crate compiles:
cd desktop/src-tauri && cargo check
```

## Run in dev

```bash
cd desktop/src-tauri && cargo tauri dev
```

This launches the floating window. It will sit empty until at least one agent
has the **桌面悬浮 Desktop Floater** toggle flipped on (Settings → Agent
edit → Desktop section).

## Build a release `.app` / `.dmg`

```bash
cd desktop/src-tauri && cargo tauri build
```

Output lands in `desktop/src-tauri/target/release/bundle/`.

> **Icons**: `icons/*.png` are placeholder gradients generated at scaffold
> time. Replace with real branding before shipping. `icons/icon.icns` must be
> generated from your final 1024×1024 master (use
> `iconutil -c icns icon.iconset/`).

## Architecture

```
┌─────────────────────┐    HTTP (loopback)    ┌─────────────────────┐
│ Tauri webview       │ ────────────────────→ │ FastAPI 9090        │
│  src/widget.js      │                       │  /api/portal/       │
│  src/widget.css     │ ←──────────────────── │   agents/desktop    │
│  src/index.html     │     poll 5 s          │   agent/{id}/chat   │
└─────────────────────┘                       └─────────────────────┘
       │                                              │
       │ WebviewWindow (frameless, transparent,       │
       │  always-on-top, skip-taskbar)                │
       ▼                                              │
┌─────────────────────┐                               │
│ Rust shell          │                               │
│  src-tauri/         │                               │
│   src/main.rs       │                               │
│   tauri.conf.json   │                               │
└─────────────────────┘                               │
                                                       │
                              flag: agent.desktop_enabled
                              flag: agent.desktop_lottie_url
                              field: agent.soul_md  (persona)
                              field: agent.status   (idle/busy/error)
```

## Auth model

`/agents/desktop` is **loopback-only** (request must come from
`127.0.0.1`/`::1`/`localhost`). No JWT required — the widget is supposed to
run on the same machine as the server.

`/agent/{id}/chat` still uses the regular JWT auth, so the chat input shows
"需要登录令牌" until Phase 3 wires up a token-pickup flow.

## File layout

```
desktop/
├── README.md            ← you are here
├── .gitignore
├── src/                 ← all UI code
│   ├── index.html
│   ├── widget.css       ← styles + breathing/pulse animations
│   └── widget.js        ← agent fetch loop, click→expand, chat
└── src-tauri/           ← Rust shell
    ├── Cargo.toml
    ├── build.rs
    ├── tauri.conf.json  ← window config (frameless/transparent/AOT)
    ├── icons/           ← placeholder PNGs
    └── src/main.rs      ← minimal entry
```
