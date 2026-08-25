fn main() {
    let manifest = tauri_build::AppManifest::new().commands(&[
        "get_runtime_status",
        "open_approval_window",
        "get_approval_context",
        "submit_approval_decision",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest)).unwrap();
}
