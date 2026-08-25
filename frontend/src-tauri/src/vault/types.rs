use std::fmt;

use serde::{Deserialize, Serialize};
use uuid::Uuid;
use zeroize::Zeroizing;

use super::dpapi::DpapiError;

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
pub struct CredentialId(Uuid);

impl CredentialId {
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }
}

impl Default for CredentialId {
    fn default() -> Self {
        Self::new()
    }
}

impl fmt::Display for CredentialId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialKind {
    ApiKey,
    SshPassword,
    PrivateKeyPassphrase,
    ImportedPrivateKey,
}

impl CredentialKind {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::ApiKey => "api_key",
            Self::SshPassword => "ssh_password",
            Self::PrivateKeyPassphrase => "private_key_passphrase",
            Self::ImportedPrivateKey => "imported_private_key",
        }
    }
}

#[derive(Debug)]
pub struct RuntimeKeys {
    pub runtime_data_key: Zeroizing<Vec<u8>>,
    pub audit_hmac_key: Zeroizing<Vec<u8>>,
}

#[derive(Debug, thiserror::Error)]
pub enum VaultError {
    #[error("credential {0} was not found")]
    NotFound(CredentialId),
    #[error("DPAPI operation failed: {0}")]
    Dpapi(#[from] DpapiError),
    #[error("vault database operation failed")]
    Database(#[from] rusqlite::Error),
    #[error("stored credential ID is invalid")]
    InvalidCredentialId(#[from] uuid::Error),
    #[error("operating-system random generation failed")]
    Random,
    #[error("runtime key {key_id} has invalid length {actual}")]
    InvalidRuntimeKeyLength { key_id: &'static str, actual: usize },
}
