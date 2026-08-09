// Prevents an additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

impl Drop for BackendProcess {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(ref mut child) = *guard {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn app_data_dir(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .app_data_dir()
        .expect("failed to resolve app data directory")
}

/// Locate the bundled backend binary inside the .app Resources directory
/// (copied there by Tauri from ``src-tauri/binaries/``).
fn find_backend_binary(resource_dir: &Path) -> Option<PathBuf> {
    let binaries_dir = resource_dir.join("binaries");
    let candidates = [
        "keepr-backend",
        "keepr-backend-aarch64-apple-darwin",
        "keepr-backend-x86_64-apple-darwin",
    ];
    for name in &candidates {
        let path = binaries_dir.join(name);
        if path.is_file() {
            return Some(path);
        }
    }
    None
}

fn show_alert(title: &str, message: &str) {
    let script = format!(
        "display dialog {:?} with title {:?} buttons {{\"OK\"}} default button \"OK\" with icon stop",
        message, title
    );
    let _ = Command::new("osascript")
        .args(["-e", &script])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn spawn_backend(binary: &PathBuf, app_data: &PathBuf) -> Option<Child> {
    let db_path = app_data.join("keepr.db");
    let index_dir = app_data.join("index");
    let upload_dir = app_data.join("uploads");
    let log_path = app_data.join("backend.log");

    let _ = std::fs::create_dir_all(&index_dir);
    let _ = std::fs::create_dir_all(&upload_dir);

    let log_file = std::fs::File::create(&log_path).ok()?;

    Command::new(binary)
        .env("DATABASE_PATH", &db_path)
        .env("INDEX_DIR", &index_dir)
        .env("UPLOAD_DIR", &upload_dir)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(log_file.try_clone().unwrap())
        .stderr(log_file)
        .spawn()
        .ok()
}

fn wait_for_backend(timeout: Duration) -> bool {
    let client = reqwest::blocking::Client::new();
    let deadline = std::time::Instant::now();
    let url = "http://127.0.0.1:8000/health";

    while deadline.elapsed() < timeout {
        match client.get(url).timeout(Duration::from_millis(500)).send() {
            Ok(resp) if resp.status().is_success() => return true,
            _ => std::thread::sleep(Duration::from_millis(250)),
        }
    }
    false
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let data_dir = app_data_dir(&app.handle());

            if cfg!(debug_assertions) {
                // Dev mode: Tauri's beforeDevCommand already started the
                // backend — just print where data is stored.
                println!(
                    "keepr: dev mode — backend managed by beforeDevCommand\n\
                     data directory → {}",
                    data_dir.display()
                );
            } else {
                // Release mode: spawn the bundled backend, wait for it, navigate.
                let resource_dir = app.path().resource_dir()
                    .expect("failed to resolve resource directory");

                let backend_bin = match find_backend_binary(&resource_dir) {
                    Some(p) => p,
                    None => {
                        show_alert(
                            "keepr — Backend Missing",
                            "The backend binary was not found inside the\n\
                             application bundle.  The build may be incomplete."
                        );
                        std::process::exit(1);
                    }
                };

                let child = match spawn_backend(&backend_bin, &data_dir) {
                    Some(c) => c,
                    None => {
                        show_alert(
                            "keepr — Backend Error",
                            &format!("Could not start the backend.\n\nPath: {}", backend_bin.display())
                        );
                        std::process::exit(1);
                    }
                };

                app.manage(BackendProcess(Mutex::new(Some(child))));
                println!("keepr: data directory → {}", data_dir.display());

                if !wait_for_backend(Duration::from_secs(30)) {
                    show_alert(
                        "keepr — Timeout",
                        "The backend did not start within 30 seconds.\n\
                         Check the logs in the data directory."
                    );
                    std::process::exit(1);
                }

                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(ref mut child) = *guard {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                        *guard = None;
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running keepr");
}
