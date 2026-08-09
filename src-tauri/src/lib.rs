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
