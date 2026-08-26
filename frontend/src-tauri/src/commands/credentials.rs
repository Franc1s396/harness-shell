use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::MutexGuard,
};

use tauri::{AppHandle, State};
use tauri_plugin_dialog::DialogExt;
use zeroize::Zeroizing;

use crate::vault::{
    CredentialId, CredentialKind, CredentialReference, SecretVault, VaultError, VaultState,
};

use super::CommandError;

const MAX_SECRET_BYTES: usize = 1_048_576;

#[tauri::command]
pub fn store_ssh_password(
    vault: State<'_, VaultState>,
    secret: String,
) -> Result<CredentialReference, CommandError> {
    store_text_secret(vault, secret, CredentialKind::SshPassword)
}

#[tauri::command]
pub fn store_private_key_passphrase(
    vault: State<'_, VaultState>,
    secret: String,
) -> Result<CredentialReference, CommandError> {
    store_text_secret(vault, secret, CredentialKind::PrivateKeyPassphrase)
}

#[tauri::command]
pub async fn import_private_key(
    app: AppHandle,
    vault: State<'_, VaultState>,
) -> Result<Option<CredentialReference>, CommandError> {
    let (selection_sender, selection_receiver) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_file(move |selection| {
        let _ = selection_sender.send(selection);
    });
    let selected = selection_receiver.await.map_err(|_| {
        CommandError::new(
            "PRIVATE_KEY_DIALOG_FAILED",
            "The private key dialog did not return a result.",
        )
    })?;
    let Some(selected) = selected else {
        return Ok(None);
    };
    let path = selected.into_path().map_err(|_| {
        CommandError::new(
            "PRIVATE_KEY_PATH_INVALID",
            "The selected private key path is invalid.",
        )
    })?;
    let bytes = tauri::async_runtime::spawn_blocking(move || read_private_key(path))
        .await
        .map_err(|_| {
            CommandError::new(
                "PRIVATE_KEY_READ_TASK_FAILED",
                "The private key read task failed.",
            )
        })??;
    let credential_id = lock_vault(&vault)?
        .put_secret(CredentialKind::ImportedPrivateKey, bytes.as_slice())
        .map_err(map_vault_error)?;
    Ok(Some(CredentialReference {
        credential_id,
        kind: CredentialKind::ImportedPrivateKey,
    }))
}

#[tauri::command]
pub fn delete_ssh_credential(
    vault: State<'_, VaultState>,
    credential_id: CredentialId,
) -> Result<(), CommandError> {
    lock_vault(&vault)?
        .delete_secret(credential_id)
        .map_err(map_vault_error)?;
    Ok(())
}

fn store_text_secret(
    vault: State<'_, VaultState>,
    secret: String,
    kind: CredentialKind,
) -> Result<CredentialReference, CommandError> {
    let bytes = Zeroizing::new(secret.into_bytes());
    validate_secret_bytes(bytes.as_slice())?;
    let credential_id = lock_vault(&vault)?
        .put_secret(kind, bytes.as_slice())
        .map_err(map_vault_error)?;
    Ok(CredentialReference {
        credential_id,
        kind,
    })
}

fn lock_vault(state: &VaultState) -> Result<MutexGuard<'_, SecretVault>, CommandError> {
    state
        .0
        .lock()
        .map_err(|_| CommandError::new("VAULT_LOCK_FAILED", "The credential Vault is unavailable."))
}

fn validate_secret_bytes(bytes: &[u8]) -> Result<(), CommandError> {
    if bytes.is_empty() || bytes.len() > MAX_SECRET_BYTES {
        return Err(CommandError::new(
            "CREDENTIAL_LENGTH_INVALID",
            "The credential must contain between 1 byte and 1 MiB.",
        ));
    }
    Ok(())
}

fn validate_private_key_path(path: &Path) -> Result<(), CommandError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| {
        CommandError::new(
            "PRIVATE_KEY_METADATA_FAILED",
            "The selected private key metadata could not be read.",
        )
    })?;
    if !metadata.file_type().is_file() {
        return Err(CommandError::new(
            "PRIVATE_KEY_NOT_REGULAR_FILE",
            "The selected private key must be a regular file.",
        ));
    }
    if metadata.len() == 0 || metadata.len() > MAX_SECRET_BYTES as u64 {
        return Err(CommandError::new(
            "PRIVATE_KEY_LENGTH_INVALID",
            "The selected private key must contain between 1 byte and 1 MiB.",
        ));
    }
    Ok(())
}

fn read_private_key(path: PathBuf) -> Result<Zeroizing<Vec<u8>>, CommandError> {
    validate_private_key_path(&path)?;
    let file = fs::File::open(&path).map_err(|_| {
        CommandError::new(
            "PRIVATE_KEY_READ_FAILED",
            "The selected private key could not be read.",
        )
    })?;
    let mut bytes = Zeroizing::new(Vec::new());
    file.take((MAX_SECRET_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            CommandError::new(
                "PRIVATE_KEY_READ_FAILED",
                "The selected private key could not be read.",
            )
        })?;
    validate_private_key_bytes(bytes.as_slice())?;
    Ok(bytes)
}

fn validate_private_key_bytes(bytes: &[u8]) -> Result<(), CommandError> {
    if bytes.is_empty() || bytes.len() > MAX_SECRET_BYTES {
        return Err(CommandError::new(
            "PRIVATE_KEY_LENGTH_INVALID",
            "The selected private key must contain between 1 byte and 1 MiB.",
        ));
    }
    Ok(())
}

fn map_vault_error(error: VaultError) -> CommandError {
    match error {
        VaultError::NotFound(_) => {
            CommandError::new("CREDENTIAL_NOT_FOUND", "The credential could not be found.")
        }
        VaultError::KindMismatch { .. } => CommandError::new(
            "CREDENTIAL_KIND_MISMATCH",
            "The credential kind does not match the requested operation.",
        ),
        _ => CommandError::new("VAULT_OPERATION_FAILED", "The credential operation failed."),
    }
}

#[cfg(test)]
mod tests {
    use super::validate_private_key_bytes;

    #[test]
    fn private_key_input_must_be_non_empty_and_at_most_one_mibibyte() {
        assert!(validate_private_key_bytes(&[]).is_err());
        assert!(validate_private_key_bytes(&vec![0_u8; 1_048_576]).is_ok());
        assert!(validate_private_key_bytes(&vec![0_u8; 1_048_577]).is_err());
    }
}
