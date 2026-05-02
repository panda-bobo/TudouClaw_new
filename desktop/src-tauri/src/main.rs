// TudouClaw desktop floating-agent widget — Mac MVP.
//
// Two channels glue the floater to the portal:
//
//   1. URL scheme (tauri-plugin-deep-link)
//      tudouclaw://open  → show window, focus
//      tudouclaw://hide  → hide window
//      Used for the *cold-launch* case: portal page opens, app isn't
//      running yet, JS triggers the scheme and macOS launches us.
//
//   2. Local HTTP server on 127.0.0.1:9192 (tiny_http)
//      POST /heartbeat → record now (no UI side-effects)
//      GET  /health    → 200 OK (lets portal detect we're already up)
//      POST /show      → show + focus
//      POST /hide      → hide
//      Once the app is running, portal pings /heartbeat every 10s.
//      A watchdog hides the window after 30s of silence — that's
//      how "portal closed" auto-collapses the floater without
//      relying on the unreliable browser `beforeunload` event.
//
// Window starts hidden (visible: false in tauri.conf.json) so
// launching the .app from Finder doesn't pop a stray window — it
// stays invisible until /show or tudouclaw://open arrives.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};
use tauri_plugin_deep_link::DeepLinkExt;
use tiny_http::{Header, Method, Response, Server};

const LOCAL_PORT: u16 = 9192;
const IDLE_HIDE_AFTER_SECS: u64 = 30;
const WATCHDOG_TICK_SECS: u64 = 5;

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
            start_local_server(handle);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running TudouClaw desktop");
}

/// Apply a `tudouclaw://<action>` URL.
fn handle_scheme(app: &AppHandle, url: &url::Url) {
    let Some(window) = app.get_webview_window("main") else { return; };
    let action = url.host_str().unwrap_or("").to_lowercase();
    match action.as_str() {
        "open" | "show" => {
            let _ = window.show();
            let _ = window.set_focus();
        }
        "hide" | "dismiss" => {
            let _ = window.hide();
        }
        _ => {}
    }
}

fn start_local_server(handle: AppHandle) {
    let state = ServerState {
        handle,
        last_heartbeat: Arc::new(Mutex::new(Instant::now())),
    };

    spawn_watchdog(state.clone());
    spawn_http_server(state);
}

/// Hide the window if we haven't heard from the portal in a while.
/// Doesn't touch the window if no heartbeat has ever been received
/// since startup (Instant::now() is initial value, elapsed near zero).
fn spawn_watchdog(state: ServerState) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(WATCHDOG_TICK_SECS));
        let elapsed = match state.last_heartbeat.lock() {
            Ok(t) => t.elapsed(),
            Err(_) => continue,
        };
        if elapsed > Duration::from_secs(IDLE_HIDE_AFTER_SECS) {
            if let Some(win) = state.handle.get_webview_window("main") {
                let _ = win.hide();
            }
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

    // CORS preflight — always allow.
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
            if let Some(win) = state.handle.get_webview_window("main") {
                let _ = win.show();
                let _ = win.set_focus();
            }
            // Treat /show as a heartbeat too — the portal just told us
            // it's active, no point hiding 30s later if no follow-up.
            if let Ok(mut t) = state.last_heartbeat.lock() {
                *t = Instant::now();
            }
            r#"{"ok":true}"#.to_string()
        }
        "/hide" => {
            if let Some(win) = state.handle.get_webview_window("main") {
                let _ = win.hide();
            }
            r#"{"ok":true}"#.to_string()
        }
        _ => {
            let _ = request.respond(
                with_cors(
                    Response::from_string(r#"{"error":"not found"}"#)
                        .with_status_code(404),
                ),
            );
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
