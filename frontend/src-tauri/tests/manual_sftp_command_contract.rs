use std::{collections::BTreeSet, fs, path::PathBuf};

const SFTP_COMMANDS: [&str; 21] = [
    "get_manual_sftp_context",
    "list_manual_sftp_directory",
    "next_manual_sftp_directory_batch",
    "close_manual_sftp_listing",
    "inspect_manual_sftp_entry",
    "hash_manual_sftp_file",
    "open_manual_sftp_link",
    "prepare_manual_sftp_upload",
    "execute_manual_sftp_upload",
    "prepare_manual_sftp_download",
    "execute_manual_sftp_download",
    "discard_manual_sftp_preparation",
    "create_manual_sftp_directory",
    "rename_manual_sftp_entry",
    "remove_manual_sftp_entry",
    "preflight_manual_sftp_delete",
    "execute_manual_sftp_delete",
    "cancel_manual_sftp_operation",
    "list_manual_sftp_recoveries",
    "inspect_manual_sftp_recovery",
    "execute_manual_sftp_recovery",
];

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

#[test]
fn permission_exposes_exactly_the_canonical_manual_sftp_commands() {
    let permission = fs::read_to_string(manifest_dir().join("permissions/sftp.toml"))
        .expect("manual SFTP permission must exist");
    let value: toml::Value = toml::from_str(&permission).expect("permission must be valid TOML");
    let entries = value["permission"]
        .as_array()
        .expect("permission must be an array");
    assert_eq!(entries.len(), 1, "manual SFTP uses one permission only");
    assert_eq!(entries[0]["identifier"].as_str(), Some("sftp"));
    let allowed = entries[0]["commands"]["allow"]
        .as_array()
        .expect("commands.allow must be an array")
        .iter()
        .map(|value| value.as_str().expect("command must be a string"))
        .collect::<BTreeSet<_>>();
    assert_eq!(allowed, SFTP_COMMANDS.into_iter().collect());
}

#[test]
fn commands_are_main_only_typed_and_keep_local_paths_inside_rust() {
    let commands = fs::read_to_string(manifest_dir().join("src/commands/sftp.rs"))
        .expect("manual SFTP command module must exist");

    for command in SFTP_COMMANDS {
        assert!(
            commands.contains(&format!("pub async fn {command}("))
                || commands.contains(&format!("pub fn {command}(")),
            "missing typed command {command}"
        );
    }
    assert_eq!(
        commands.matches("require_main_window(&window)?;").count(),
        SFTP_COMMANDS.len(),
        "every command must reject non-main callers before state or dialog use"
    );
    assert!(commands.contains("ssh_session_id: Option<Uuid>"));
    assert!(commands.contains("\"NO_SESSION\""));
    assert!(commands.contains("app.dialog().file().pick_file"));
    assert!(commands.contains("app.dialog()"));
    assert!(commands.contains(".set_file_name(display_name)"));
    assert!(commands.contains(".save_file("));
    assert!(commands.contains("return Ok(None);"));
    assert!(!commands.contains("pub local_path:"));
    assert!(!commands.contains("raw_sftp"));
    assert!(!commands.contains("agent_sftp"));
    assert!(!commands.contains("serde_json::Map"));
}

#[test]
fn rename_and_remove_commands_do_not_accept_webview_snapshots() {
    let commands = fs::read_to_string(manifest_dir().join("src/commands/sftp.rs"))
        .expect("manual SFTP command module must exist");

    assert!(
        !commands.contains("source_snapshot:"),
        "rename snapshots must be acquired inside Rust, not supplied by WebView"
    );
    assert!(
        !commands.contains("target_snapshot:"),
        "rename snapshots must be acquired inside Rust, not supplied by WebView"
    );
    assert!(
        !commands.contains("expected_snapshot:"),
        "remove snapshots must be acquired inside Rust, not supplied by WebView"
    );
}

#[test]
fn command_module_does_not_add_webview_dialog_or_filesystem_permissions() {
    let capability = fs::read_to_string(manifest_dir().join("capabilities/main.json"))
        .expect("main capability must exist");
    assert!(capability.contains("\"sftp\""));
    assert!(!capability.contains("dialog:"));
    assert!(!capability.contains("fs:"));
}
