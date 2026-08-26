fn main() {
    let manifest = tauri_build::AppManifest::new().commands(&[
        "get_runtime_status",
        "open_approval_window",
        "get_approval_context",
        "submit_approval_decision",
        "store_ssh_password",
        "store_private_key_passphrase",
        "import_private_key",
        "delete_ssh_credential",
        "list_connections",
        "create_connection",
        "update_connection",
        "delete_connection",
        "confirm_host_key",
        "replace_host_key",
        "inspect_host_key",
        "connect_ssh",
        "disconnect_ssh",
        "open_pty",
        "write_pty",
        "resize_pty",
        "close_pty",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest)).unwrap();
}
