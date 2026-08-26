pub mod dpapi;
mod store;
mod types;

use std::sync::Mutex;

pub use store::SecretVault;
pub use types::{CredentialId, CredentialKind, CredentialReference, RuntimeKeys, VaultError};

pub struct VaultState(pub Mutex<SecretVault>);

impl VaultState {
    pub fn new(vault: SecretVault) -> Self {
        Self(Mutex::new(vault))
    }
}
