use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::State;
use uuid::Uuid;

use crate::{
    runtime::{
        ConfirmHostKeyRequest, ConnectSshRequest, CreateConnectionRequest, DeleteConnectionRequest,
        DisconnectSshRequest, GetConnectionRequest, InspectHostKeyRequest, ListConnectionsRequest,
        ReplaceHostKeyRequest, RuntimeClient, RuntimeClientError, RuntimeClientHandle,
        SshJumpRequest, UpdateConnectionRequest,
    },
    vault::{CredentialId, CredentialKind, SecretVault, VaultError, VaultState},
};

use super::CommandError;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectionProfileInput {
    pub display_name: String,
    pub group_name: Option<String>,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub auth_kind: AuthKind,
    pub credential_id: CredentialId,
    pub passphrase_credential_id: Option<CredentialId>,
    pub proxy_jump_id: Option<Uuid>,
    pub favorite: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthKind {
    Password,
    PrivateKey,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ConnectionProfile {
    pub connection_id: Uuid,
    pub version: u64,
    pub display_name: String,
    pub group_name: Option<String>,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub auth_kind: AuthKind,
    pub credential_id: CredentialId,
    pub passphrase_credential_id: Option<CredentialId>,
    pub proxy_jump_id: Option<Uuid>,
    pub favorite: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HostKeyCandidate {
    pub connection_id: Uuid,
    pub host: String,
    pub port: u16,
    pub key_algorithm: String,
    pub fingerprint_sha256: String,
    pub public_key_openssh_b64: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct HostKeyRecord {
    pub host_key_id: Uuid,
    pub connection_id: Uuid,
    pub key_algorithm: String,
    pub fingerprint_sha256: String,
    pub public_key_openssh_b64: String,
    pub status: HostKeyStatus,
    pub confirmed_at: String,
    pub replaced_at: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HostKeyStatus {
    Active,
    Replaced,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct ConnectionStatus {
    pub connection_id: Uuid,
    pub state: ConnectionState,
    pub session_id: Option<Uuid>,
    pub error_code: Option<String>,
    pub recoverable: bool,
    pub correlation_id: Uuid,
    pub host_key_candidate: Option<HostKeyCandidate>,
    pub trusted_fingerprint_sha256: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConnectionState {
    Disconnected,
    Connecting,
    HostKeyRequired,
    Ready,
    Closing,
    Failed,
}

#[tauri::command]
pub async fn list_connections(
    runtime: State<'_, RuntimeClientHandle>,
) -> Result<Vec<ConnectionProfile>, CommandError> {
    list_connections_with_runtime(&*runtime).await
}

#[doc(hidden)]
pub async fn list_connections_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
) -> Result<Vec<ConnectionProfile>, CommandError> {
    runtime
        .execute(ListConnectionsRequest)
        .await
        .map(|response| response.connections)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn create_connection(
    runtime: State<'_, RuntimeClientHandle>,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    create_connection_with_runtime(&*runtime, input).await
}

#[doc(hidden)]
pub async fn create_connection_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    runtime
        .execute(CreateConnectionRequest(input))
        .await
        .map(|response| response.connection)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn update_connection(
    runtime: State<'_, RuntimeClientHandle>,
    connection_id: Uuid,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    update_connection_with_runtime(&*runtime, connection_id, input).await
}

#[doc(hidden)]
pub async fn update_connection_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    connection_id: Uuid,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    runtime
        .execute(UpdateConnectionRequest {
            connection_id,
            input,
        })
        .await
        .map(|response| response.connection)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn delete_connection(
    runtime: State<'_, RuntimeClientHandle>,
    connection_id: Uuid,
) -> Result<bool, CommandError> {
    delete_connection_with_runtime(&*runtime, connection_id).await
}

#[doc(hidden)]
pub async fn delete_connection_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    connection_id: Uuid,
) -> Result<bool, CommandError> {
    runtime
        .execute(DeleteConnectionRequest { connection_id })
        .await
        .map(|response| response.deleted)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn confirm_host_key(
    runtime: State<'_, RuntimeClientHandle>,
    candidate: HostKeyCandidate,
) -> Result<HostKeyRecord, CommandError> {
    confirm_host_key_with_runtime(&*runtime, candidate).await
}

#[doc(hidden)]
pub async fn confirm_host_key_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    candidate: HostKeyCandidate,
) -> Result<HostKeyRecord, CommandError> {
    runtime
        .execute(ConfirmHostKeyRequest(candidate))
        .await
        .map(|response| response.host_key)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn replace_host_key(
    runtime: State<'_, RuntimeClientHandle>,
    candidate: HostKeyCandidate,
    expected_old_fingerprint: String,
) -> Result<HostKeyRecord, CommandError> {
    replace_host_key_with_runtime(&*runtime, candidate, expected_old_fingerprint).await
}

#[doc(hidden)]
pub async fn replace_host_key_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    candidate: HostKeyCandidate,
    expected_old_fingerprint: String,
) -> Result<HostKeyRecord, CommandError> {
    runtime
        .execute(ReplaceHostKeyRequest {
            candidate,
            expected_old_fingerprint,
        })
        .await
        .map(|response| response.host_key)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn inspect_host_key(
    runtime: State<'_, RuntimeClientHandle>,
    vault: State<'_, VaultState>,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    inspect_host_key_with_dependencies(&*runtime, &vault, connection_id).await
}

#[doc(hidden)]
pub async fn inspect_host_key_with_dependencies<R: RuntimeClient + ?Sized>(
    runtime: &R,
    vault: &VaultState,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    let profile = get_connection_with_runtime(runtime, connection_id).await?;
    let jump = match profile.proxy_jump_id {
        Some(proxy_jump_id) => {
            let jump = get_direct_proxy_profile_with_runtime(runtime, proxy_jump_id).await?;
            let vault = vault.0.lock().map_err(|_| {
                CommandError::new("VAULT_LOCK_FAILED", "The credential Vault is unavailable.")
            })?;
            Some(ssh_jump_request(&vault, &jump)?)
        }
        None => None,
    };
    runtime
        .execute(InspectHostKeyRequest {
            connection_id,
            jump,
        })
        .await
        .map(|response| response.status)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn connect_ssh(
    runtime: State<'_, RuntimeClientHandle>,
    vault: State<'_, VaultState>,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    connect_ssh_with_dependencies(&*runtime, &vault, connection_id).await
}

#[doc(hidden)]
pub async fn connect_ssh_with_dependencies<R: RuntimeClient + ?Sized>(
    runtime: &R,
    vault: &VaultState,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    let profile = get_connection_with_runtime(runtime, connection_id).await?;
    let jump = match profile.proxy_jump_id {
        Some(proxy_jump_id) => {
            Some(get_direct_proxy_profile_with_runtime(runtime, proxy_jump_id).await?)
        }
        None => None,
    };

    let request = {
        let vault = vault.0.lock().map_err(|_| {
            CommandError::new("VAULT_LOCK_FAILED", "The credential Vault is unavailable.")
        })?;
        let jump = jump
            .as_ref()
            .map(|profile| ssh_jump_request(&vault, profile))
            .transpose()?;
        match profile.auth_kind {
            AuthKind::Password => {
                let password = vault
                    .resolve_secret(profile.credential_id, CredentialKind::SshPassword)
                    .map_err(map_vault_error)?;
                ConnectSshRequest::password(
                    connection_id,
                    profile.version,
                    STANDARD.encode(password.as_slice()),
                    jump,
                )
            }
            AuthKind::PrivateKey => {
                let private_key = vault
                    .resolve_secret(profile.credential_id, CredentialKind::ImportedPrivateKey)
                    .map_err(map_vault_error)?;
                let passphrase_b64 = profile
                    .passphrase_credential_id
                    .map(|passphrase_id| {
                        vault
                            .resolve_secret(passphrase_id, CredentialKind::PrivateKeyPassphrase)
                            .map(|passphrase| STANDARD.encode(passphrase.as_slice()))
                            .map_err(map_vault_error)
                    })
                    .transpose()?;
                ConnectSshRequest::private_key(
                    connection_id,
                    profile.version,
                    STANDARD.encode(private_key.as_slice()),
                    passphrase_b64,
                    jump,
                )
            }
        }
    };
    runtime
        .execute(request)
        .await
        .map(|response| response.status)
        .map_err(map_runtime_error)
}

#[tauri::command]
pub async fn disconnect_ssh(
    runtime: State<'_, RuntimeClientHandle>,
    sftp: State<'_, crate::sftp::coordinator::SftpCoordinatorState>,
    ssh_session_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    if sftp
        .coordinator()
        .active_transfer_for_session(ssh_session_id)
        .is_some()
    {
        return Err(CommandError::new(
            "SFTP_TRANSFER_ACTIVE",
            "Choose whether to wait or cancel the active manual SFTP transfer before disconnecting.",
        ));
    }
    disconnect_ssh_with_runtime(&*runtime, ssh_session_id).await
}

#[doc(hidden)]
pub async fn disconnect_ssh_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    ssh_session_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    runtime
        .execute(DisconnectSshRequest { ssh_session_id })
        .await
        .map(|response| response.status)
        .map_err(map_runtime_error)
}

async fn get_connection_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    connection_id: Uuid,
) -> Result<ConnectionProfile, CommandError> {
    runtime
        .execute(GetConnectionRequest { connection_id })
        .await
        .map(|response| response.connection)
        .map_err(map_runtime_error)
}

async fn get_direct_proxy_profile_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    connection_id: Uuid,
) -> Result<ConnectionProfile, CommandError> {
    let profile = get_connection_with_runtime(runtime, connection_id).await?;
    if profile.proxy_jump_id.is_some() {
        return Err(CommandError::new(
            "MULTI_HOP_PROXY_FORBIDDEN",
            "A ProxyJump profile cannot reference another ProxyJump profile.",
        ));
    }
    Ok(profile)
}

fn ssh_jump_request(
    vault: &SecretVault,
    profile: &ConnectionProfile,
) -> Result<SshJumpRequest, CommandError> {
    match profile.auth_kind {
        AuthKind::Password => {
            let password = vault
                .resolve_secret(profile.credential_id, CredentialKind::SshPassword)
                .map_err(map_vault_error)?;
            Ok(SshJumpRequest::password(
                profile.connection_id,
                profile.version,
                STANDARD.encode(password.as_slice()),
            ))
        }
        AuthKind::PrivateKey => {
            let private_key = vault
                .resolve_secret(profile.credential_id, CredentialKind::ImportedPrivateKey)
                .map_err(map_vault_error)?;
            let passphrase_b64 = profile
                .passphrase_credential_id
                .map(|passphrase_id| {
                    vault
                        .resolve_secret(passphrase_id, CredentialKind::PrivateKeyPassphrase)
                        .map(|passphrase| STANDARD.encode(passphrase.as_slice()))
                        .map_err(map_vault_error)
                })
                .transpose()?;
            Ok(SshJumpRequest::private_key(
                profile.connection_id,
                profile.version,
                STANDARD.encode(private_key.as_slice()),
                passphrase_b64,
            ))
        }
    }
}

fn map_vault_error(error: VaultError) -> CommandError {
    match error {
        VaultError::NotFound(_) => CommandError::new(
            "CREDENTIAL_NOT_FOUND",
            "The SSH credential could not be found.",
        ),
        VaultError::KindMismatch { .. } => CommandError::new(
            "CREDENTIAL_KIND_MISMATCH",
            "The SSH credential kind does not match the connection profile.",
        ),
        _ => CommandError::new("VAULT_OPERATION_FAILED", "The credential operation failed."),
    }
}

pub(super) fn map_runtime_error(error: RuntimeClientError) -> CommandError {
    if let Some(problem) = error.problem() {
        return CommandError::with_details(
            problem.error_code.clone(),
            problem.message.clone(),
            Value::Object(problem.details.clone()),
        );
    }
    CommandError::new(error.error_code(), "The SSH runtime is unavailable.")
}
