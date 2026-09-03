fn main() {
    let manifest = tauri_build::AppManifest::new().commands(&["get_backend_bootstrap"]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest)).unwrap();
}
