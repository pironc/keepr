// keepr native commands.
//
// This module is intentionally minimal for the initial Tauri integration.
// It will grow native Rust commands for:
//   - Local speech-to-text (SFSpeechRecognizer via objc bindings)
//   - Settings persistence
//   - File-system operations
//   - Clear app data
//
// Keep commands here rather than in main.rs so Rust changes to the
// command layer don't touch process-management code.

/// Native commands invoked from the frontend via `window.__TAURI__.core.invoke`.
pub mod commands {
    use tauri::AppHandle;

    /// Exit the whole application.
    ///
    /// The frontend calls this after the model-switch backend has been asked to
    /// quit (`POST /api/models/quit`). The backend SIGTERMs itself, at which point
    /// nothing else was left running to tell the Tauri shell to close — so this is
    /// the explicit "now close the window too" step. `AppHandle::exit` runs the
    /// normal shutdown path (including the `on_window_event` Destroyed handler in
    /// main.rs, which kills the backend child in release mode; in dev mode the
    /// backend already shut itself down before this is reached).
    #[tauri::command]
    pub fn quit_app(app: AppHandle) {
        app.exit(0);
    }
}
