use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;
use serde_json::Value;

const CUSTOM_COMMANDS: [&str; 1] = ["get_backend_bootstrap"];

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
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display())),
    )
    .unwrap_or_else(|error| panic!("failed to parse {}: {error}", path.display()))
}

fn capabilities() -> BTreeMap<String, Capability> {
    let mut result = BTreeMap::new();
    for entry in fs::read_dir(manifest_dir().join("capabilities")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let value = json(&path);
        let identifier = value["identifier"].as_str().unwrap().to_owned();
        let capability = Capability {
            permissions: value["permissions"]
                .as_array()
                .unwrap()
                .iter()
                .map(|permission| permission.as_str().unwrap().to_owned())
                .collect(),
            windows: value["windows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|window| window.as_str().unwrap().to_owned())
                .collect(),
        };
        assert!(result.insert(identifier, capability).is_none());
    }
    result
}

fn permission_commands() -> BTreeMap<String, BTreeSet<String>> {
    let mut result = BTreeMap::new();
    for entry in fs::read_dir(manifest_dir().join("permissions")).unwrap() {
        let path = entry.unwrap().path();
        if path.extension().and_then(|value| value.to_str()) != Some("toml") {
            continue;
        }
        let file: PermissionFile = toml::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        for permission in file.permission {
            result.insert(
                permission.identifier,
                permission.commands.allow.into_iter().collect(),
            );
        }
    }
    result
}

fn allowed_commands(capability: &Capability) -> BTreeSet<String> {
    let permissions = permission_commands();
    capability
        .permissions
        .iter()
        .flat_map(|permission| permissions.get(permission).cloned().unwrap_or_default())
        .collect()
}

#[test]
fn main_window_capability_exposes_only_bootstrap() {
    let all = capabilities();
    assert_eq!(all.keys().cloned().collect::<Vec<_>>(), vec!["main"]);

    let main = &all["main"];
    assert_eq!(main.windows, vec!["main"]);
    assert_eq!(
        main.permissions.iter().cloned().collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "bootstrap".to_owned(),
            "core:window:allow-close".to_owned(),
            "core:window:allow-destroy".to_owned(),
        ])
    );
    assert_eq!(
        allowed_commands(main),
        CUSTOM_COMMANDS.into_iter().map(str::to_owned).collect()
    );
}

#[test]
fn config_uses_dynamic_loopback_http_and_websocket_only() {
    let config = json(&manifest_dir().join("tauri.conf.json"));
    let security = &config["app"]["security"];
    assert_eq!(security["capabilities"], serde_json::json!(["main"]));
    let production = security["csp"]["connect-src"].as_str().unwrap();
    assert_eq!(
        production,
        "'self' ipc: http://ipc.localhost http://127.0.0.1:* ws://127.0.0.1:*"
    );
    let development = security["devCsp"]["connect-src"].as_str().unwrap();
    assert!(development.starts_with(production));
    assert!(development.contains("http://localhost:1420"));
    assert!(development.contains("ws://localhost:1420"));
}

#[test]
fn build_manifest_and_invoke_handler_register_only_bootstrap() {
    let build_script = fs::read_to_string(manifest_dir().join("build.rs")).unwrap();
    for command in CUSTOM_COMMANDS {
        assert!(build_script.contains(&format!("\"{command}\"")));
    }
    assert_eq!(build_script.matches('"').count(), CUSTOM_COMMANDS.len() * 2);

    let library = fs::read_to_string(manifest_dir().join("src/lib.rs")).unwrap();
    let handler = library
        .split_once("tauri::generate_handler![")
        .unwrap()
        .1
        .split_once("])")
        .unwrap()
        .0;
    let registered = handler
        .split(',')
        .map(str::trim)
        .filter(|command| !command.is_empty())
        .collect::<BTreeSet<_>>();
    assert_eq!(registered, CUSTOM_COMMANDS.into_iter().collect());
}
