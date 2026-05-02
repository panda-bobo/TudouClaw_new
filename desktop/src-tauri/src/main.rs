// TudouClaw desktop floating-agent widget — Mac MVP.
//
// Architecture (Phase 4 — multi-window):
//
//   ┌──────────── Tauri app process ────────────────┐
//   │                                                │
//   │  main window  (hidden keepalive, label="main") │
//   │     never shown — keeps the process alive when │
//   │     no per-agent windows exist.                │
//   │                                                │
//   │  agent supervisor thread (every 5 s):          │
//   │    GET 127.0.0.1:9090/api/portal/agents/desktop│
//   │    diff against currently-spawned set:         │
//   │      added   → WebviewWindowBuilder            │
//   │                label = sanitize(agent.id),     │
//   │                URL   = index.html?agent_id=…   │
//   │                position cascades 100+30·idx    │
//   │      removed → window.close()                  │
//   │                                                │
//   │  local HTTP server (127.0.0.1:9192):           │
//   │    /heartbeat → reset watchdog timer           │
//   │    /show /hide → enumerate non-"main" windows  │
//   │    /health → 200 OK                            │
//   │                                                │
//   │  watchdog (every 5 s):                         │
//   │    if last heartbeat > 30 s ago, hide all      │
//   │    per-agent windows (main untouched).         │
//   │                                                │
//   │  deep-link plugin:                             │
//   │    tudouclaw://open  → show all per-agent      │
//   │    tudouclaw://hide  → hide all per-agent      │
//   └────────────────────────────────────────────────┘

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_deep_link::DeepLinkExt;
use tiny_http::{Header, Method, Response, Server};

const LOCAL_PORT: u16 = 9192;
const IDLE_HIDE_AFTER_SECS: u64 = 30;
const WATCHDOG_TICK_SECS: u64 = 5;
const AGENT_POLL_SECS: u64 = 5;
const FASTAPI_AGENTS_URL: &str = "http://127.0.0.1:9090/api/portal/agents/desktop";
const MAIN_LABEL: &str = "main";

#[derive(Clone)]
struct ServerState {
    handle: AppHandle,
    last_heartbeat: Arc<Mutex<Instant>>,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_deep_link::init())
        .setup(|app| {
            let handle = app.handle().clone();
            app.deep_link().on_open_url({
                let handle = handle.clone();
                move |event| {
                    for url in event.urls() {
                        handle_scheme(&handle, &url);
                    }
                }
            });
            start_local_server(handle.clone());
            start_agent_supervisor(handle);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running TudouClaw desktop");
}

// ── Per-agent windows ───────────────────────────────────────────────

/// Tauri window labels must match `^[a-zA-Z0-9-_]+$` — sanitize the
/// agent_id (which is hex anyway today, but stay defensive).
fn label_for(agent_id: &str) -> String {
    let mut s = String::from("agent-");
    for c in agent_id.chars() {
        if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
            s.push(c);
        } else {
            s.push('_');
        }
    }
    s
}

fn spawn_agent_window(handle: &AppHandle, agent_id: &str, index: usize) {
    let label = label_for(agent_id);
    if handle.get_webview_window(&label).is_some() {
        return; // already spawned
    }
    let url = format!("index.html?agent_id={}", agent_id);
    let offset = (index as f64) * 30.0;
    let result = WebviewWindowBuilder::new(handle, &label, WebviewUrl::App(url.into()))
        .title(format!("TudouClaw {}", &agent_id[..agent_id.len().min(6)]))
        .inner_size(140.0, 140.0)
        .min_inner_size(120.0, 120.0)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .resizable(true)
        .skip_taskbar(true)
        .visible_on_all_workspaces(true)
        .shadow(false)
        .visible(false) // /show or scheme makes it visible
        .position(120.0 + offset, 120.0 + offset)
        .build();
    if let Err(e) = result {
        eprintln!("[tudouclaw] spawn window for {agent_id} failed: {e}");
    }
}

fn close_agent_window(handle: &AppHandle, agent_id: &str) {
    let label = label_for(agent_id);
    if let Some(win) = handle.get_webview_window(&label) {
        let _ = win.close();
    }
}

/// Enumerate all per-agent windows (skip the hidden keepalive).
fn for_each_agent_window<F: Fn(&tauri::WebviewWindow)>(handle: &AppHandle, f: F) {
    for (label, win) in handle.webview_windows() {
        if label == MAIN_LABEL { continue; }
        f(&win);
    }
}

fn show_all_agent_windows(handle: &AppHandle) {
    for_each_agent_window(handle, |win| { let _ = win.show(); });
}

fn hide_all_agent_windows(handle: &AppHandle) {
    for_each_agent_window(handle, |win| { let _ = win.hide(); });
}

fn poll_enabled_agent_ids() -> Vec<String> {
    let resp = match ureq::get(FASTAPI_AGENTS_URL)
        .timeout(Duration::from_millis(800))
        .call()
    {
        Ok(r) => r,
        Err(_) => return Vec::new(),
    };
    let s = match resp.into_string() {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let body: serde_json::Value = match serde_json::from_str(&s) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    body["agents"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|a| a["id"].as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

fn start_agent_supervisor(handle: AppHandle) {
    thread::spawn(move || {
        let mut known: HashSet<String> = HashSet::new();
        loop {
            let current: HashSet<String> =
                poll_enabled_agent_ids().into_iter().collect();

            // Spawn windows for newly-enabled agents (cascading offset).
            let mut idx = known.len();
            for id in current.difference(&known) {
                spawn_agent_window(&handle, id, idx);
                idx += 1;
            }

            // Close windows for newly-disabled agents.
            for id in known.difference(&current).cloned().collect::<Vec<_>>() {
                close_agent_window(&handle, &id);
            }

            known = current;
            thread::sleep(Duration::from_secs(AGENT_POLL_SECS));
        }
    });
}

// ── URL scheme handler ──────────────────────────────────────────────

fn handle_scheme(app: &AppHandle, url: &url::Url) {
    let action = url.host_str().unwrap_or("").to_lowercase();
    match action.as_str() {
        "open" | "show" => {
            show_all_agent_windows(app);
            // Focus the first one we find so it grabs attention.
            for (label, win) in app.webview_windows() {
                if label == MAIN_LABEL { continue; }
                let _ = win.set_focus();
                break;
            }
        }
        "hide" | "dismiss" => hide_all_agent_windows(app),
        _ => {}
    }
}

// ── Local HTTP server (heartbeat + show/hide) ───────────────────────

fn start_local_server(handle: AppHandle) {
    let state = ServerState {
        handle,
        last_heartbeat: Arc::new(Mutex::new(Instant::now())),
    };
    spawn_watchdog(state.clone());
    spawn_http_server(state);
}

fn spawn_watchdog(state: ServerState) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(WATCHDOG_TICK_SECS));
        let elapsed = match state.last_heartbeat.lock() {
            Ok(t) => t.elapsed(),
            Err(_) => continue,
        };
        if elapsed > Duration::from_secs(IDLE_HIDE_AFTER_SECS) {
            hide_all_agent_windows(&state.handle);
        }
    });
}

fn spawn_http_server(state: ServerState) {
    thread::spawn(move || {
        let server = match Server::http(("127.0.0.1", LOCAL_PORT)) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[tudouclaw] failed to bind 127.0.0.1:{LOCAL_PORT}: {e}");
                return;
            }
        };
        for request in server.incoming_requests() {
            handle_request(&state, request);
        }
    });
}

fn handle_request(state: &ServerState, request: tiny_http::Request) {
    let path = request
        .url()
        .split('?')
        .next()
        .unwrap_or("")
        .to_string();

    if request.method() == &Method::Options {
        let _ = request.respond(with_cors(Response::empty(204)));
        return;
    }

    let body = match path.as_str() {
        "/health" => r#"{"ok":true,"version":"0.1.0"}"#.to_string(),
        "/heartbeat" => {
            if let Ok(mut t) = state.last_heartbeat.lock() {
                *t = Instant::now();
            }
            r#"{"ok":true}"#.to_string()
        }
        "/show" => {
            show_all_agent_windows(&state.handle);
            if let Ok(mut t) = state.last_heartbeat.lock() {
                *t = Instant::now();
            }
            r#"{"ok":true}"#.to_string()
        }
        "/hide" => {
            hide_all_agent_windows(&state.handle);
            r#"{"ok":true}"#.to_string()
        }
        _ => {
            let _ = request.respond(with_cors(
                Response::from_string(r#"{"error":"not found"}"#)
                    .with_status_code(404),
            ));
            return;
        }
    };

    let _ = request.respond(with_cors(Response::from_string(body)));
}

fn with_cors<R: std::io::Read>(resp: Response<R>) -> Response<R> {
    resp.with_header(
        "Access-Control-Allow-Origin: *"
            .parse::<Header>()
            .expect("static header"),
    )
    .with_header(
        "Access-Control-Allow-Methods: GET, POST, OPTIONS"
            .parse::<Header>()
            .expect("static header"),
    )
    .with_header(
        "Access-Control-Allow-Headers: Content-Type"
            .parse::<Header>()
            .expect("static header"),
    )
    .with_header(
        "Content-Type: application/json"
            .parse::<Header>()
            .expect("static header"),
    )
}
