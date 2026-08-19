// Prevents an additional console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
// `Stdio` is only used by the mac/Linux alert dialogs (the Windows branch
// uses MessageBoxW instead) — gate the import so a Windows-only build has no
// unused-import warning.
#[cfg(not(windows))]
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

// Windows-native message box (declared by hand so we don't need an extra
// Win32 crate for a single user-facing dialog).
#[cfg(windows)]
unsafe extern "system" {
    fn MessageBoxW(hwnd: isize, text: *const u16, caption: *const u16, buttons: u32);
}

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

/// Locate the bundled backend binary inside the bundle's resources directory.
///
/// The backend is a PyInstaller ``--onefile`` binary named
/// ``keepr-backend-<target-triple>[.exe]`` that the CI matrix copies into
/// ``src-tauri/binaries/`` (which Tauri ships into the bundle as
/// ``binaries/`` under ``resource_dir()``). We derive the expected name from
/// the target triple that ``build.rs`` bakes in at compile time
/// (``env!("KEEPR_TARGET_TRIPLE")``), which matches exactly the
/// ``x86_64-pc-windows-msvc`` / ``aarch64-apple-darwin`` / … names the
/// workflow uses — so one function covers every platform/arch without a
/// hardcoded per-OS list.
///
/// The bare ``keepr-backend`` name is kept as a fallback for local builds that
/// drop a backend in without the triple suffix (e.g. a developer running
/// ``cargo tauri build`` after placing a hand-built ``keepr-backend``).
fn find_backend_binary(resource_dir: &Path) -> Option<PathBuf> {
    let exe_ext = if cfg!(windows) { ".exe" } else { "" };
    let tripled = format!("keepr-backend-{}{}", env!("KEEPR_TARGET_TRIPLE"), exe_ext);
    let binaries_dir = resource_dir.join("binaries");
    for name in ["keepr-backend", &tripled] {
        let path = binaries_dir.join(name);
        if path.is_file() {
            return Some(path);
        }
    }
    None
}

/// Show a blocking error dialog on the host platform (best-effort).
///
/// We branch per OS to avoid a cross-platform dialog dependency:
///   - macOS  -> `osascript` (a display dialog);
///   - Windows -> `MessageBoxW` (Win32, zero extra crate);
///   - Linux   -> `zenity`, else `kdialog`, else `xmessage`, else stderr.
///
/// A failure to show a dialog is never fatal here — the callers log and exit
/// with a clear status either way.
fn show_alert(title: &str, message: &str) {
    #[cfg(windows)]
    {
        fn wide(s: &str) -> Vec<u16> {
            s.encode_utf16().chain(std::iter::once(0)).collect()
        }
        let text = wide(message);
        let caption = wide(title);
        unsafe {
            MessageBoxW(0, text.as_ptr(), caption.as_ptr(), 0x10); // MB_ICONERROR
        }
    }
    #[cfg(target_os = "macos")]
    {
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
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        // Try the common freedesktop/GNOME dialog launchers in turn, stopping
        // at the first one that actually launches (status() returning Ok
        // with any exit code means the binary existed and ran). Each command
        // is a distinct tool so a terminal-less or kdialog-less desktop still
        // shows at least one box.
        let zenity = Command::new("zenity")
            .args(["--error", "--title", title, "--text", message])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok();
        let kdialog = Command::new("kdialog")
            .args(["--error", message, "--title", title])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok();
        let xmessage = Command::new("xmessage")
            .arg(format!("{}: {}", title, message))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok();

        if !(zenity || kdialog || xmessage) {
            eprintln!("keepr error: {} — {}", title, message);
        }
    }
}

fn spawn_backend(binary: &PathBuf, app_data: &PathBuf) -> Option<Child> {
    let db_path = app_data.join("keepr.db");
    let index_dir = app_data.join("index");
    let upload_dir = app_data.join("uploads");
    let models_dir = app_data.join("models");
    // Selection lives next to the other app state so it survives regardless of
    // the process cwd (the config defaults are cwd-relative).
    let selection_path = app_data.join("model_selection.json");
    let log_path = app_data.join("backend.log");

    let _ = std::fs::create_dir_all(&index_dir);
    let _ = std::fs::create_dir_all(&upload_dir);
    let _ = std::fs::create_dir_all(&models_dir);
    let _ = std::fs::create_dir_all(app_data);

    let log_file = std::fs::File::create(&log_path).ok()?;

    Command::new(binary)
        .env("DATABASE_PATH", &db_path)
        .env("INDEX_DIR", &index_dir)
        .env("UPLOAD_DIR", &upload_dir)
        .env("MODELS_DIR", &models_dir)
        .env("MODEL_SELECTION_PATH", &selection_path)
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
        .invoke_handler(tauri::generate_handler![keepr_lib::commands::quit_app])
        .setup(|app| {
            let data_dir = app_data_dir(app.handle());

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
                let resource_dir = app
                    .path()
                    .resource_dir()
                    .expect("failed to resolve resource directory");

                let backend_bin = match find_backend_binary(&resource_dir) {
                    Some(p) => p,
                    None => {
                        show_alert(
                            "keepr — Backend Missing",
                            "The backend binary was not found inside the\n\
                             application bundle.  The build may be incomplete.",
                        );
                        std::process::exit(1);
                    }
                };

                let child = match spawn_backend(&backend_bin, &data_dir) {
                    Some(c) => c,
                    None => {
                        show_alert(
                            "keepr — Backend Error",
                            &format!(
                                "Could not start the backend.\n\nPath: {}",
                                backend_bin.display()
                            ),
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
                         Check the logs in the data directory.",
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
