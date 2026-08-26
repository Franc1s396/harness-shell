use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use zeroize::Zeroizing;

use super::{
    dpapi,
    types::{CredentialId, CredentialKind, RuntimeKeys, VaultError},
};

const RUNTIME_DATA_KEY_ID: &str = "runtime-data-v1";
const AUDIT_HMAC_KEY_ID: &str = "audit-hmac-v1";
const RUNTIME_KEY_BYTES: usize = 32;

pub struct SecretVault {
    connection: Connection,
}

impl SecretVault {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, VaultError> {
        let connection = Connection::open(path)?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "synchronous", "FULL")?;
        connection.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS vault_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            INSERT OR IGNORE INTO vault_meta(key, value) VALUES ('schema-version', '1');

            CREATE TABLE IF NOT EXISTS vault_secrets (
                credential_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                protected_secret BLOB NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            ) STRICT;

            CREATE TABLE IF NOT EXISTS vault_keys (
                key_id TEXT PRIMARY KEY,
                protected_key BLOB NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            ) STRICT;
            ",
        )?;
        Ok(Self { connection })
    }

    pub fn put_secret(
        &self,
        kind: CredentialKind,
        plaintext: &[u8],
    ) -> Result<CredentialId, VaultError> {
        let credential_id = CredentialId::new();
        let protected = dpapi::protect(plaintext, "Harness Shell credential")?;
        self.connection.execute(
            "INSERT INTO vault_secrets(credential_id, kind, protected_secret)
             VALUES (?1, ?2, ?3)",
            params![credential_id.to_string(), kind.as_str(), protected],
        )?;
        Ok(credential_id)
    }

    pub fn resolve_secret(
        &self,
        credential_id: CredentialId,
        expected_kind: CredentialKind,
    ) -> Result<Zeroizing<Vec<u8>>, VaultError> {
        let stored: Option<(String, Vec<u8>)> = self
            .connection
            .query_row(
                "SELECT kind, protected_secret FROM vault_secrets WHERE credential_id = ?1",
                [credential_id.to_string()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (stored_kind, protected) = stored.ok_or(VaultError::NotFound(credential_id))?;
        let actual_kind = CredentialKind::from_stored(&stored_kind)?;
        if actual_kind != expected_kind {
            return Err(VaultError::KindMismatch {
                credential_id,
                expected: expected_kind,
                actual: actual_kind,
            });
        }
        dpapi::unprotect(&protected).map_err(Into::into)
    }

    pub fn delete_secret(&self, credential_id: CredentialId) -> Result<bool, VaultError> {
        let deleted = self.connection.execute(
            "DELETE FROM vault_secrets WHERE credential_id = ?1",
            [credential_id.to_string()],
        )?;
        Ok(deleted == 1)
    }

    pub fn get_or_create_runtime_keys(&self) -> Result<RuntimeKeys, VaultError> {
        Ok(RuntimeKeys {
            runtime_data_key: self.get_or_create_runtime_key(RUNTIME_DATA_KEY_ID)?,
            audit_hmac_key: self.get_or_create_runtime_key(AUDIT_HMAC_KEY_ID)?,
        })
    }

    fn get_or_create_runtime_key(
        &self,
        key_id: &'static str,
    ) -> Result<Zeroizing<Vec<u8>>, VaultError> {
        let existing: Option<Vec<u8>> = self
            .connection
            .query_row(
                "SELECT protected_key FROM vault_keys WHERE key_id = ?1",
                [key_id],
                |row| row.get(0),
            )
            .optional()?;

        let key = match existing {
            Some(protected) => dpapi::unprotect(&protected)?,
            None => {
                let mut key = Zeroizing::new(vec![0_u8; RUNTIME_KEY_BYTES]);
                getrandom::fill(key.as_mut_slice()).map_err(|_| VaultError::Random)?;
                let protected = dpapi::protect(key.as_slice(), "Harness Shell runtime key")?;
                self.connection.execute(
                    "INSERT INTO vault_keys(key_id, protected_key) VALUES (?1, ?2)",
                    params![key_id, protected],
                )?;
                key
            }
        };

        if key.len() != RUNTIME_KEY_BYTES {
            return Err(VaultError::InvalidRuntimeKeyLength {
                key_id,
                actual: key.len(),
            });
        }
        Ok(key)
    }
}
