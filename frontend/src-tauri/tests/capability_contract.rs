use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use serde_json::Value;

const CUSTOM_COMMANDS: [&str; 21] = [
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
];

const FORBIDDEN_COMMAND_FRAGMENTS: [&str; 8] = [
    "resolve_secret",
    "read_private_key",
    "sidecar_frame",
    "agent_exec",
    "sftp_write",
    "shell",
    "sudo",
    "vault",
];

const INTERNAL_AGENT_METHODS: [&str; 9] = [
    "agent_exec",
    "remote_stat",
    "remote_list",
    "remote_read_range",
    "remote_hash",
    "sftp",
    "lstat",
    "listdir",
    "sha256",
];

#[derive(Debug, Deserialize)]
struct PermissionFile {
    #[serde(default)]
    permission: Vec<Permission>,
}

#[derive(Debug, Deserialize)]
struct Permission {
    identifier: String,
    #[serde(default)]
    commands: PermissionCommands,
}

#[derive(Debug, Default, Deserialize)]
struct PermissionCommands {
    #[serde(default)]
    allow: Vec<String>,
}

#[derive(Debug)]
struct Capability {
    permissions: Vec<String>,
    windows: Vec<String>,
}

fn manifest_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn json(path: &Path) -> Value {
    let contents = fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()));
    serde_json::from_str(&contents)
        .unwrap_or_else(|error| panic!("failed to parse {}: {error}", path.display()))
}

fn capabilities() -> BTreeMap<String, Capability> {
    let directory = manifest_dir().join("capabilities");
    let mut capabilities = BTreeMap::new();

    for entry in fs::read_dir(&directory).expect("capabilities directory must exist") {
        let path = entry.expect("capability entry must be readable").path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let value = json(&path);
        let identifier = value["identifier"]
            .as_str()
            .expect("capability identifier must be a string")
            .to_owned();
        let permissions = value["permissions"]
            .as_array()
            .expect("capability permissions must be an array")
            .iter()
            .map(|permission| {
                permission
                    .as_str()
                    .expect("scoped permissions are not allowed in M1")
                    .to_owned()
            })
            .collect();
        let windows = value["windows"]
            .as_array()
            .expect("capability windows must be an array")
            .iter()
            .map(|window| {
                window
                    .as_str()
                    .expect("window labels must be strings")
                    .to_owned()
            })
            .collect();

        assert!(
            capabilities
                .insert(
                    identifier,
                    Capability {
                        permissions,
                        windows,
                    },
                )
                .is_none(),
            "capability identifiers must be unique"
        );
    }

    capabilities
}

fn permission_commands() -> BTreeMap<String, BTreeSet<String>> {
    let directory = manifest_dir().join("permissions");
    let mut permissions = BTreeMap::new();
    if !directory.exists() {
        return permissions;
    }

    for entry in fs::read_dir(&directory).expect("permissions directory must be readable") {
        let path = entry.expect("permission entry must be readable").path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("toml") {
            continue;
        }
        let contents = fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()));
        let file: PermissionFile = toml::from_str(&contents)
            .unwrap_or_else(|error| panic!("failed to parse {}: {error}", path.display()));
        for permission in file.permission {
            assert!(
                permissions
                    .insert(
                        permission.identifier,
                        permission.commands.allow.into_iter().collect(),
                    )
                    .is_none(),
                "permission identifiers must be unique"
            );
        }
    }

    permissions
}

fn allowed_commands(capability: &Capability) -> BTreeSet<String> {
    let permission_commands = permission_commands();
    capability
        .permissions
        .iter()
        .flat_map(|permission| {
            permission_commands
                .get(permission)
                .cloned()
                .unwrap_or_default()
        })
        .collect()
}

fn capability<'a>(all: &'a BTreeMap<String, Capability>, identifier: &str) -> &'a Capability {
    all.get(identifier)
        .unwrap_or_else(|| panic!("missing capability {identifier}"))
}

#[test]
fn window_capabilities_are_least_privilege() {
    let all = capabilities();
    assert_eq!(
        all.keys().cloned().collect::<Vec<_>>(),
        vec!["approval", "main"],
        "only the main and approval capabilities may exist"
    );

    let main = capability(&all, "main");
    assert_eq!(
        main.permissions.iter().cloned().collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "connections".to_owned(),
            "core:event:allow-listen".to_owned(),
            "core:event:allow-unlisten".to_owned(),
            "credentials".to_owned(),
            "runtime".to_owned(),
            "terminal".to_owned(),
        ])
    );
    assert!(!main.permissions.iter().any(|permission| {
        permission == "core:event:allow-emit" || permission == "core:event:allow-emit-to"
    }));
    let main_commands = allowed_commands(main);
    assert!(main_commands.contains("get_runtime_status"));
    assert!(main_commands.contains("open_approval_window"));
    assert!(!main_commands.contains("get_approval_context"));
    assert!(!main_commands.contains("submit_approval_decision"));
    assert!(main
        .permissions
        .iter()
        .all(|permission| !permission.starts_with("shell:")));

    let approval = capability(&all, "approval");
    assert_eq!(approval.permissions, vec!["approval"]);
    assert!(approval
        .permissions
        .iter()
        .all(|permission| !permission.starts_with("core:event:")));
    let approval_commands = allowed_commands(approval);
    assert!(approval_commands.contains("get_approval_context"));
    assert!(approval_commands.contains("submit_approval_decision"));
    assert!(!approval_commands.contains("get_runtime_status"));
    assert!(!approval_commands.contains("open_approval_window"));
    assert!(approval
        .permissions
        .iter()
        .all(|permission| !permission.starts_with("shell:")));

    for command in main_commands.iter().chain(approval_commands.iter()) {
        for forbidden in FORBIDDEN_COMMAND_FRAGMENTS {
            assert!(
                !command.contains(forbidden),
                "forbidden command fragment {forbidden:?} exposed by {command}"
            );
        }
    }
    for capability in all.values() {
        assert!(
            capability.permissions.iter().all(|permission| {
                !permission.starts_with("dialog:") && !permission.starts_with("fs:")
            }),
            "WebView dialog and filesystem plugin permissions are forbidden"
        );
        assert!(
            capability
                .permissions
                .iter()
                .all(|permission| !permission.starts_with("core:window:")),
            "WebView window commands can target other labels and are forbidden"
        );
        assert!(
            capability
                .windows
                .iter()
                .all(|window| !window.contains('*')),
            "window label wildcards are forbidden"
        );
    }
}

#[test]
fn config_enables_only_explicit_capabilities_and_strict_csp() {
    let config = json(&manifest_dir().join("tauri.conf.json"));
    let security = &config["app"]["security"];
    assert_eq!(
        security["capabilities"],
        serde_json::json!(["main", "approval"])
    );
    assert!(
        security["csp"].is_object(),
        "production CSP must be an object"
    );
    assert!(
        security["devCsp"].is_object(),
        "development CSP must be an object"
    );
}

#[test]
fn build_manifest_scopes_exactly_the_m2_management_commands() {
    let build_script =
        fs::read_to_string(manifest_dir().join("build.rs")).expect("build.rs must be readable");
    assert!(build_script.contains("AppManifest::new()"));

    for command in CUSTOM_COMMANDS {
        assert!(
            build_script.contains(&format!("\"{command}\"")),
            "build manifest is missing {command}"
        );
    }
    assert_eq!(
        build_script.matches('"').count(),
        CUSTOM_COMMANDS.len() * 2,
        "build manifest must declare exactly the approved command strings"
    );

    let library =
        fs::read_to_string(manifest_dir().join("src/lib.rs")).expect("src/lib.rs must be readable");
    let handler = library
        .split_once("tauri::generate_handler![")
        .expect("invoke handler must use generate_handler")
        .1
        .split_once("])")
        .expect("invoke handler command list must close")
        .0;
    let registered = handler
        .split(',')
        .map(str::trim)
        .filter(|command| !command.is_empty())
        .collect::<BTreeSet<_>>();
    assert_eq!(registered, CUSTOM_COMMANDS.into_iter().collect());
}

#[test]
fn approval_window_creation_uses_an_async_command_on_windows() {
    let runtime_commands = fs::read_to_string(manifest_dir().join("src/commands/runtime.rs"))
        .expect("runtime commands must be readable");

    assert!(
        runtime_commands.contains("pub async fn open_approval_window("),
        "WebviewWindowBuilder deadlocks when called from a synchronous command on Windows"
    );
    assert!(
        !runtime_commands.contains("install_approval_window_lifecycle"),
        "native close behavior must not be patched after a deadlocked window is created"
    );
}

#[test]
fn terminal_bridge_exposes_no_remote_control_side_effect_api() {
    let terminal_commands = fs::read_to_string(manifest_dir().join("src/commands/terminal.rs"))
        .expect("terminal commands must be readable");
    for forbidden in [
        "set_title(",
        "clipboard",
        "open_path",
        "open_url",
        "register_uri_scheme_protocol",
        "on_window_event",
    ] {
        assert!(
            !terminal_commands.contains(forbidden),
            "terminal byte bridge must not invoke {forbidden}"
        );
    }
}

#[test]
fn internal_agent_io_has_no_webview_route() {
    let build_script =
        fs::read_to_string(manifest_dir().join("build.rs")).expect("build.rs must be readable");
    let library =
        fs::read_to_string(manifest_dir().join("src/lib.rs")).expect("lib.rs must be readable");
    let permissions = permission_commands()
        .values()
        .flat_map(|commands| commands.iter())
        .cloned()
        .collect::<BTreeSet<_>>();
    let exposed = format!("{build_script}\n{library}\n{permissions:?}");
    for method in INTERNAL_AGENT_METHODS {
        assert!(
            !exposed.contains(method),
            "internal Agent method {method} must not be exposed to WebView"
        );
    }
}
