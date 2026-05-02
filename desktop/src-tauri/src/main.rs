// TudouClaw desktop floating-agent widget — Mac MVP.
//
// Window is fully declared in tauri.conf.json (frameless, transparent,
// always-on-top, skip-taskbar). All UI lives in ../src/index.html.
// The webview talks to the local FastAPI on 127.0.0.1:9090; no Rust
// commands needed for the MVP.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running TudouClaw desktop");
}
