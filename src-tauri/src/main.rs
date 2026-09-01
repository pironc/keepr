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

/// Block until `child` exits or `timeout` elapses. `std::process::Child` has
/// no blocking-wait-with-timeout in std, so poll `try_wait` instead.
#[cfg(unix)]
fn wait_for_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(100));
            }
            _ => return false,
        }
    }
}

/// Kill and reap the backend child if one is still tracked. Shared by every
/// place that might be the one to actually notice the app is exiting — no
/// single one of them is reliably first on every platform/quit path, so this
/// runs from three: `RunEvent::Exit` (the authoritative "event loop is
/// exiting" signal, covers Cmd+Q and any other full-app-termination path),
/// the main window's own `Destroyed` event (a window can go away slightly
/// ahead of the full app-exit event on some platforms), and `Drop` (a final
/// backstop if the state value itself is dropped some other way). Calling it
/// more than once is harmless: `guard.take()` leaves nothing for the next
/// caller to act on.
///
/// The tracked child is only the PyInstaller onefile *bootloader* — it
/// unpacks to a temp dir and forks a second, real interpreter process that it
/// monitors and forwards signals to. `Child::kill()` sends SIGKILL on Unix,
/// which can never be caught or forwarded by anything: the bootloader dies
/// instantly, orphaning the interpreter, which then keeps running (and
/// listening on the port) indefinitely — see tauri-apps/tauri#11686 for the
/// same PyInstaller/Tauri interaction. SIGTERM instead *can* be relayed by
/// the bootloader, and uvicorn already shuts down gracefully on it (closing
/// the DB pool, stopping the workers) — the same SIGTERM path `/api/models/quit`
/// uses. Only fall back to killing the whole process group (both processes
/// share one — see `process_group(0)` in `spawn_backend`) if that doesn't
/// finish quickly, so a hung or non-cooperating bootloader still can't leave
/// an orphan behind.
fn kill_backend_process(process: &BackendProcess) {
    if let Ok(mut guard) = process.0.lock() {
        if let Some(mut child) = guard.take() {
            let pid = child.id();
            #[cfg(unix)]
            {
                let _ = Command::new("kill").args(["-TERM", &pid.to_string()]).status();
                if !wait_for_exit(&mut child, Duration::from_secs(5)) {
                    let _ = Command::new("kill")
                        .args(["-KILL", &format!("-{pid}")])
                        .status();
                }
            }
            #[cfg(windows)]
            {
                // No forwarding signal on Windows; `/T` walks the OS's own
                // process-tree bookkeeping so the forked interpreter is
                // killed along with the bootloader, not just the bootloader.
                let _ = Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T", "/F"])
                    .status();
            }
            let _ = child.wait();
        }
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        kill_backend_process(self);
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

fn spawn_backend(binary: &PathBuf, app_data: &PathBuf) -> std::io::Result<Child> {
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

    let log_file = std::fs::File::create(&log_path)?;

    let mut command = Command::new(binary);
    command
        .env("DATABASE_PATH", &db_path)
        .env("INDEX_DIR", &index_dir)
        .env("UPLOAD_DIR", &upload_dir)
        .env("MODELS_DIR", &models_dir)
        .env("MODEL_SELECTION_PATH", &selection_path)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(log_file.try_clone()?)
        .stderr(log_file);

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // New process group (pgid = its own pid), isolated from Tauri's own
        // group — the PyInstaller bootloader's forked interpreter inherits
        // it too, so `kill_backend_process`'s group-kill fallback can reach
        // both processes without ever touching this (Tauri) one.
        command.process_group(0);
    }

    command.spawn()
}

/// Outcome of waiting for the freshly spawned backend to become healthy.
#[derive(PartialEq)]
enum WaitResult {
    /// The backend answered ``/health`` with 200 in time.
    Healthy,
    /// The spawned backend process exited before ever becoming healthy — a
    /// definitive startup failure (almost always a port-conflict: it can't
    /// bind 127.0.0.1:8000 because a previous instance is still there). There
    /// is no point waiting out the rest of the timeout; surface it now.
    ChildExited,
    /// The backend neither became healthy nor exited within the window — a
    /// hung or stuck startup.
    Timeout,
}

/// Poll ``/health`` until the backend is up, the child exits, or the timeout
/// elapses (whichever comes first).
///
/// Detecting an early child exit is what turns the old blind 30-second stall
/// into an immediate, specific error: a backend that fails to bind dies fast,
/// and calling ``try_wait()`` each loop catches that in the first ~250 ms
/// instead of waiting out the whole window.
fn wait_for_backend(child: &mut Child, timeout: Duration) -> WaitResult {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if probe_health() {
            return WaitResult::Healthy;
        }
        // The backend died before serving — stop waiting (a bind conflict is
        // the usual cause; see backend_main's port_in_use pre-check).
        if let Ok(Some(_)) = child.try_wait() {
            return WaitResult::ChildExited;
        }
        if std::time::Instant::now() >= deadline {
            return WaitResult::Timeout;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// One `GET /health` attempt against the backend's loopback port.
///
/// Hand-rolled instead of pulling in an HTTP client crate: this only ever
/// talks to a process we just spawned on 127.0.0.1, so there's no TLS, no
/// redirects, and no proxy env vars to honor — a real HTTP client would
/// route even a localhost request through `HTTP_PROXY` if the user happened
/// to have one set, which this can't. A single fixed-size `read()` isn't
/// guaranteed to return the whole status line in one TCP segment, so this
/// keeps reading until it sees one, the buffer fills, or the connection ends.
fn probe_health() -> bool {
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};

    let addr = SocketAddr::from(([127, 0, 0, 1], 8000));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    if stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }

    let mut buf = [0u8; 64];
    let mut filled = 0;
    while filled < buf.len() {
        match stream.read(&mut buf[filled..]) {
            Ok(0) => break,
            Ok(n) => {
                filled += n;
                if buf[..filled].windows(2).any(|w| w == b"\r\n") {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    String::from_utf8_lossy(&buf[..filled]).starts_with("HTTP/1.1 200")
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

                // Pre-flight: is a keepr backend already answering on the port?
                // If so, a previous instance didn't shut down (its shell exited
                // without reaping the backend, leaving it orphaned). Spawning a
                // second backend would just fail to bind, and waiting out the
                // health time-out tells the user nothing. Detect it now, tell
                // them why, and adopt the already-running backend rather than
                // erroring out — the app works, and the message explains the
                // orphan for next time. We deliberately DON'T kill it here:
                // killing a possibly-intended running instance is riskier than
                // informing the user.
                if probe_health() {
                    show_alert(
                        "keepr — Already Running",
                        "A previous Keepr instance is still running.\n\
                         Keepr will use the running backend.\n\n\
                         If the window doesn't show, close the previous\n\
                         instance (or its stray backend process) and relaunch.",
                    );
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                    }
                    return Ok(());
                }

                let mut child = match spawn_backend(&backend_bin, &data_dir) {
                    Ok(c) => c,
                    Err(err) => {
                        show_alert(
                            "keepr — Backend Error",
                            &format!(
                                "Could not start the backend.\n\nPath: {}\nError: {}",
                                backend_bin.display(),
                                err
                            ),
                        );
                        std::process::exit(1);
                    }
                };
                println!("keepr: data directory → {}", data_dir.display());

                // Wait for the backend to come up, but bail fast on an early
                // child exit (port conflict) or the overall health time-out,
                // reaping the child so we never leave a new orphan behind.
                // The child is handed to app.manage() only once it's healthy,
                // so the failure paths own disposal themselves.
                let result = wait_for_backend(&mut child, Duration::from_secs(30));
                let mut child_guard = Some(child);
                match result {
                    WaitResult::Healthy => {}
                    WaitResult::ChildExited => {
                        show_alert(
                            "keepr — Backend Stopped",
                            "The backend exited during startup.\n\n\
                             The most common cause is port 8000 already being\n\
                             in use by a previous Keepr instance — close it\n\
                             and relaunch. See backend.log for details.",
                        );
                        if let Some(mut c) = child_guard.take() {
                            let _ = c.kill();
                            let _ = c.wait();
                        }
                        std::process::exit(1);
                    }
                    WaitResult::Timeout => {
                        show_alert(
                            "keepr — Timeout",
                            "The backend did not become healthy within 30 seconds.\n\
                             Check the logs in the data directory.",
                        );
                        if let Some(mut c) = child_guard.take() {
                            let _ = c.kill();
                            let _ = c.wait();
                        }
                        std::process::exit(1);
                    }
                }

                app.manage(BackendProcess(Mutex::new(child_guard.take())));

                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.navigate("http://127.0.0.1:8000".parse().unwrap());
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(state) = window.try_state::<BackendProcess>() {
                    kill_backend_process(&state);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building keepr")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    kill_backend_process(&state);
                }
            }
        });
}
