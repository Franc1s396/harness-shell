#![cfg(target_os = "windows")]

use std::fs;

use harness_shell_lib::vault::{
    dpapi::{protect, unprotect, DpapiError},
    CredentialId, CredentialKind, SecretVault, VaultError,
};
use tempfile::tempdir;

const PLAINTEXT_MARKER: &[u8] = b"M1-DPAPI-PLAINTEXT-091a0d3f";

#[test]
fn vault_round_trips_secrets_without_persisting_plaintext() {
    let directory = tempdir().expect("create vault temp directory");
    let database_path = directory.path().join("vault.sqlite3");

    {
        let vault = SecretVault::open(&database_path).expect("open vault");
        let credential_id = vault
            .put_secret(CredentialKind::ApiKey, PLAINTEXT_MARKER)
            .expect("store credential");

        let resolved = vault
            .resolve_secret(credential_id)
            .expect("resolve credential");
        assert_eq!(resolved.as_slice(), PLAINTEXT_MARKER);
    }

    for entry in fs::read_dir(directory.path()).expect("list vault files") {
        let path = entry.expect("read vault file entry").path();
        if path.is_file() {
            let persisted = fs::read(&path).expect("read vault file");
            assert!(
                !persisted
                    .windows(PLAINTEXT_MARKER.len())
                    .any(|window| window == PLAINTEXT_MARKER),
                "plaintext marker persisted in {}",
                path.display()
            );
        }
    }
}

#[test]
fn runtime_keys_are_stable_and_exactly_32_bytes() {
    let directory = tempdir().expect("create vault temp directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");

    let first = vault
        .get_or_create_runtime_keys()
        .expect("create runtime keys");
    let second = vault
        .get_or_create_runtime_keys()
        .expect("resolve runtime keys");

    assert_eq!(first.runtime_data_key.len(), 32);
    assert_eq!(first.audit_hmac_key.len(), 32);
    assert_eq!(
        first.runtime_data_key.as_slice(),
        second.runtime_data_key.as_slice()
    );
    assert_eq!(
        first.audit_hmac_key.as_slice(),
        second.audit_hmac_key.as_slice()
    );
    assert_ne!(
        first.runtime_data_key.as_slice(),
        first.audit_hmac_key.as_slice()
    );
}

#[test]
fn unknown_credentials_fail_closed() {
    let directory = tempdir().expect("create vault temp directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");
    let unknown_id = CredentialId::new();

    assert!(matches!(
        vault.resolve_secret(unknown_id),
        Err(VaultError::NotFound(id)) if id == unknown_id
    ));
}

#[test]
fn dpapi_rejects_empty_and_oversized_blobs_before_ffi() {
    let oversized = vec![0_u8; 1_048_577];

    assert!(matches!(
        protect(&[], "test"),
        Err(DpapiError::InvalidLength {
            operation: "CryptProtectData",
            length: 0
        })
    ));
    assert!(matches!(
        unprotect(&[]),
        Err(DpapiError::InvalidLength {
            operation: "CryptUnprotectData",
            length: 0
        })
    ));
    assert!(matches!(
        protect(&oversized, "test"),
        Err(DpapiError::InvalidLength {
            operation: "CryptProtectData",
            length: 1_048_577
        })
    ));
    assert!(matches!(
        unprotect(&oversized),
        Err(DpapiError::InvalidLength {
            operation: "CryptUnprotectData",
            length: 1_048_577
        })
    ));
}
