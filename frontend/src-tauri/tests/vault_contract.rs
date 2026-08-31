#![cfg(target_os = "windows")]

use std::{env, fs, path::PathBuf};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use harness_shell_lib::vault::{
    dpapi::{protect, unprotect, DpapiError},
    CredentialId, CredentialKind, SecretVault, VaultError,
};
use tempfile::tempdir;

const PLAINTEXT_MARKER: &[u8] = b"M1-DPAPI-PLAINTEXT-091a0d3f";

#[test]
fn model_api_key_round_trips_only_as_api_key_kind() {
    let directory = tempdir().expect("create Vault directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open Vault");
    let api_key_id = vault
        .put_secret(CredentialKind::ApiKey, b"model-key-marker")
        .expect("store model API key");

    assert!(matches!(
        vault.resolve_secret(api_key_id, CredentialKind::ApiKey),
        Ok(secret) if secret.as_slice() == b"model-key-marker"
    ));
    assert!(matches!(
        vault.resolve_secret(api_key_id, CredentialKind::SshPassword),
        Err(VaultError::KindMismatch { .. })
    ));
}

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
            .resolve_secret(credential_id, CredentialKind::ApiKey)
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
    let (first, second) = {
        let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");
        let first = vault
            .get_or_create_runtime_keys()
            .expect("create runtime keys");
        let second = vault
            .get_or_create_runtime_keys()
            .expect("resolve runtime keys");
        (first, second)
    };

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

    for entry in fs::read_dir(directory.path()).expect("list Vault files") {
        let path = entry.expect("read Vault file entry").path();
        if !path.is_file() {
            continue;
        }
        let persisted = fs::read(&path).expect("read Vault file");
        for key in [&first.runtime_data_key, &first.audit_hmac_key] {
            let encoded = STANDARD.encode(key.as_slice());
            assert!(
                !persisted
                    .windows(key.len())
                    .any(|window| window == key.as_slice()),
                "runtime key plaintext persisted in {}",
                path.display()
            );
            assert!(
                !persisted
                    .windows(encoded.len())
                    .any(|window| window == encoded.as_bytes()),
                "runtime key base64 persisted in {}",
                path.display()
            );
        }
    }
}

#[test]
fn unknown_credentials_fail_closed() {
    let directory = tempdir().expect("create vault temp directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");
    let unknown_id = CredentialId::new();

    assert!(matches!(
        vault.resolve_secret(unknown_id, CredentialKind::SshPassword),
        Err(VaultError::NotFound(id)) if id == unknown_id
    ));
}

#[test]
fn resolve_rejects_credential_kind_confusion() {
    let directory = tempdir().expect("create vault temp directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");
    let credential_id = vault
        .put_secret(CredentialKind::SshPassword, b"kind-confusion-marker")
        .expect("store credential");

    assert!(matches!(
        vault.resolve_secret(credential_id, CredentialKind::ImportedPrivateKey),
        Err(VaultError::KindMismatch {
            credential_id: actual_id,
            expected: CredentialKind::ImportedPrivateKey,
            actual: CredentialKind::SshPassword,
        }) if actual_id == credential_id
    ));
}

#[test]
fn deleting_a_credential_is_idempotent_and_prevents_resolution() {
    let directory = tempdir().expect("create vault temp directory");
    let vault = SecretVault::open(directory.path().join("vault.sqlite3")).expect("open vault");
    let credential_id = vault
        .put_secret(CredentialKind::PrivateKeyPassphrase, b"delete-marker")
        .expect("store credential");

    assert!(vault
        .delete_secret(credential_id)
        .expect("delete credential"));
    assert!(!vault
        .delete_secret(credential_id)
        .expect("repeat credential deletion"));
    assert!(matches!(
        vault.resolve_secret(credential_id, CredentialKind::PrivateKeyPassphrase),
        Err(VaultError::NotFound(id)) if id == credential_id
    ));
}

#[test]
fn credential_ids_round_trip_through_the_command_wire_shape() {
    let credential_id = CredentialId::new();
    let parsed = credential_id
        .to_string()
        .parse::<CredentialId>()
        .expect("parse credential ID");

    assert_eq!(parsed, credential_id);
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

#[test]
#[ignore = "run by verify-m2 after the SSH lab creates runtime credentials"]
fn writes_runtime_lab_vault_evidence_when_requested() {
    let database_path = PathBuf::from(
        env::var_os("HARNESS_M2_VAULT_EVIDENCE_DB")
            .expect("HARNESS_M2_VAULT_EVIDENCE_DB is required"),
    );
    assert!(database_path.is_absolute());
    let jump_password = env::var("HARNESS_M2_JUMP_PASSWORD")
        .expect("HARNESS_M2_JUMP_PASSWORD is required")
        .into_bytes();
    let target_password = env::var("HARNESS_M2_TARGET_PASSWORD")
        .expect("HARNESS_M2_TARGET_PASSWORD is required")
        .into_bytes();
    let passphrase = env::var("HARNESS_M2_KEY_PASSPHRASE")
        .expect("HARNESS_M2_KEY_PASSPHRASE is required")
        .into_bytes();
    let plain_key = fs::read(
        env::var_os("HARNESS_M2_PLAIN_KEY_PATH").expect("HARNESS_M2_PLAIN_KEY_PATH is required"),
    )
    .expect("read unencrypted lab key");
    let encrypted_key = fs::read(
        env::var_os("HARNESS_M2_ENCRYPTED_KEY_PATH")
            .expect("HARNESS_M2_ENCRYPTED_KEY_PATH is required"),
    )
    .expect("read encrypted lab key");

    let vault = SecretVault::open(&database_path).expect("open evidence Vault");
    let values = [
        (CredentialKind::SshPassword, jump_password.as_slice()),
        (CredentialKind::SshPassword, target_password.as_slice()),
        (CredentialKind::ImportedPrivateKey, plain_key.as_slice()),
        (CredentialKind::ImportedPrivateKey, encrypted_key.as_slice()),
        (CredentialKind::PrivateKeyPassphrase, passphrase.as_slice()),
    ];
    for (kind, plaintext) in values {
        let credential_id = vault
            .put_secret(kind, plaintext)
            .expect("store lab credential");
        let resolved = vault
            .resolve_secret(credential_id, kind)
            .expect("resolve lab credential");
        assert_eq!(resolved.as_slice(), plaintext);
    }
    let keys = vault
        .get_or_create_runtime_keys()
        .expect("create runtime keys");
    assert_eq!(keys.runtime_data_key.len(), 32);
    assert_eq!(keys.audit_hmac_key.len(), 32);
}
