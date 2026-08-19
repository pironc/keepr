fn main() {
    tauri_build::build();

    // Bake the compile target triple into the binary (e.g.
    // "x86_64-pc-windows-msvc"), so main.rs can derive the bundled backend
    // binary's filename — `keepr-backend-<triple>[.exe]` — without guessing
    // from the OS at runtime. Cargo sets `TARGET` only during the build, so
    // it has to be captured here and embedded via cargo:rustc-env.
    let triple = std::env::var("TARGET").unwrap_or_else(|_| "unknown-target".to_string());
    println!("cargo:rustc-env=KEEPR_TARGET_TRIPLE={}", triple);
    println!("cargo:rerun-if-changed=build.rs");
}
