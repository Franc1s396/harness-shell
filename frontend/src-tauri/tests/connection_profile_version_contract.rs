use std::{fs, path::PathBuf};

fn connections_source() -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("src")
        .join("commands")
        .join("connections.rs");
    fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("failed to read {}: {error}", path.display()))
}

#[test]
fn ssh_authentication_uses_numeric_profile_versions() {
    let source = connections_source();

    assert!(source.contains("pub version: u64"));
    assert!(source.contains("\"profile_version\".to_owned()"));
    assert!(source.contains("Value::Number(profile.version.into())"));
    assert!(!source.contains("profile_updated_at"));
}
