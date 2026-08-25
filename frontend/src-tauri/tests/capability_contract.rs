use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use serde_json::Value;

const CUSTOM_COMMANDS: [&str; 4] = [
    "get_runtime_status",
    "open_approval_window",
    "get_approval_context",
    "submit_approval_decision",
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
        assert!(
            !command.contains("vault"),
            "Vault command exposed: {command}"
        );
    }
    for capability in all.values() {
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
fn build_manifest_scopes_exactly_the_four_custom_commands() {
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
        "build manifest must declare exactly four command strings"
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
