use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{Map, Value};
use tauri::State;
use uuid::Uuid;

use crate::{
    protocol::MessageType,
    sidecar::broker::{RuntimeBrokerHandle, RuntimeRequest},
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

#[derive(Deserialize)]
struct ConnectionsResult {
    connections: Vec<ConnectionProfile>,
}

#[derive(Deserialize)]
struct ConnectionResult {
    connection: ConnectionProfile,
}

#[derive(Deserialize)]
struct DeleteResult {
    deleted: bool,
}

#[derive(Deserialize)]
struct HostKeyResult {
    host_key: HostKeyRecord,
}

#[derive(Deserialize)]
struct StatusResult {
    status: ConnectionStatus,
}

#[tauri::command]
pub async fn list_connections(
    broker: State<'_, RuntimeBrokerHandle>,
) -> Result<Vec<ConnectionProfile>, CommandError> {
    request(&broker, "connections.list", Map::new())
        .await
        .map(|result: ConnectionsResult| result.connections)
}

#[tauri::command]
pub async fn create_connection(
    broker: State<'_, RuntimeBrokerHandle>,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    request(&broker, "connections.create", object(input)?)
        .await
        .map(|result: ConnectionResult| result.connection)
}

#[tauri::command]
pub async fn update_connection(
    broker: State<'_, RuntimeBrokerHandle>,
    connection_id: Uuid,
    input: ConnectionProfileInput,
) -> Result<ConnectionProfile, CommandError> {
    let mut params = object(input)?;
    params.insert(
        "connection_id".to_owned(),
        Value::String(connection_id.to_string()),
    );
    request(&broker, "connections.update", params)
        .await
        .map(|result: ConnectionResult| result.connection)
}

#[tauri::command]
pub async fn delete_connection(
    broker: State<'_, RuntimeBrokerHandle>,
    connection_id: Uuid,
) -> Result<bool, CommandError> {
    let params = Map::from_iter([(
        "connection_id".to_owned(),
        Value::String(connection_id.to_string()),
    )]);
    request(&broker, "connections.delete", params)
        .await
        .map(|result: DeleteResult| result.deleted)
}

#[tauri::command]
pub async fn confirm_host_key(
    broker: State<'_, RuntimeBrokerHandle>,
    candidate: HostKeyCandidate,
) -> Result<HostKeyRecord, CommandError> {
    request(&broker, "host_key.confirm", object(candidate)?)
        .await
        .map(|result: HostKeyResult| result.host_key)
}

#[tauri::command]
pub async fn replace_host_key(
    broker: State<'_, RuntimeBrokerHandle>,
    candidate: HostKeyCandidate,
    expected_old_fingerprint: String,
) -> Result<HostKeyRecord, CommandError> {
    let mut params = object(candidate)?;
    params.insert(
        "expected_old_fingerprint".to_owned(),
        Value::String(expected_old_fingerprint),
    );
    request(&broker, "host_key.replace", params)
        .await
        .map(|result: HostKeyResult| result.host_key)
}

#[tauri::command]
pub async fn inspect_host_key(
    broker: State<'_, RuntimeBrokerHandle>,
    vault: State<'_, VaultState>,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    let profile = get_connection(&broker, connection_id).await?;
    let mut params = Map::from_iter([(
        "connection_id".to_owned(),
        Value::String(connection_id.to_string()),
    )]);
    if let Some(proxy_jump_id) = profile.proxy_jump_id {
        let jump = get_direct_proxy_profile(&broker, proxy_jump_id).await?;
        let jump_params = {
            let vault = vault.0.lock().map_err(|_| {
                CommandError::new("VAULT_LOCK_FAILED", "The credential Vault is unavailable.")
            })?;
            authentication_params(&vault, &jump)?
        };
        params.insert("jump".to_owned(), Value::Object(jump_params));
        request_secret(&broker, "host_key.inspect", params)
            .await
            .map(|result: StatusResult| result.status)
    } else {
        request(&broker, "host_key.inspect", params)
            .await
            .map(|result: StatusResult| result.status)
    }
}

#[tauri::command]
pub async fn connect_ssh(
    broker: State<'_, RuntimeBrokerHandle>,
    vault: State<'_, VaultState>,
    connection_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    let profile = get_connection(&broker, connection_id).await?;
    let jump = match profile.proxy_jump_id {
        Some(proxy_jump_id) => Some(get_direct_proxy_profile(&broker, proxy_jump_id).await?),
        None => None,
    };

    let mut params = Map::from_iter([
        (
            "connection_id".to_owned(),
            Value::String(connection_id.to_string()),
        ),
        (
            "profile_updated_at".to_owned(),
            Value::String(profile.updated_at.clone()),
        ),
    ]);
    {
        let vault = vault.0.lock().map_err(|_| {
            CommandError::new("VAULT_LOCK_FAILED", "The credential Vault is unavailable.")
        })?;
        append_authentication_params(&mut params, &vault, &profile)?;
        if let Some(jump) = jump.as_ref() {
            params.insert(
                "jump".to_owned(),
                Value::Object(authentication_params(&vault, jump)?),
            );
        }
    }
    request_secret(&broker, "ssh.connect", params)
        .await
        .map(|result: StatusResult| result.status)
}

#[tauri::command]
pub async fn disconnect_ssh(
    broker: State<'_, RuntimeBrokerHandle>,
    ssh_session_id: Uuid,
) -> Result<ConnectionStatus, CommandError> {
    let params = Map::from_iter([(
        "ssh_session_id".to_owned(),
        Value::String(ssh_session_id.to_string()),
    )]);
    request(&broker, "ssh.disconnect", params)
        .await
        .map(|result: StatusResult| result.status)
}

fn object(value: impl Serialize) -> Result<Map<String, Value>, CommandError> {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| {
            CommandError::new(
                "COMMAND_PAYLOAD_INVALID",
                "The command payload could not be encoded.",
            )
        })
}

pub(super) async fn request<T: DeserializeOwned>(
    broker: &RuntimeBrokerHandle,
    method: &str,
    params: Map<String, Value>,
) -> Result<T, CommandError> {
    request_with_sensitivity(broker, method, params, false).await
}

pub(super) async fn request_secret<T: DeserializeOwned>(
    broker: &RuntimeBrokerHandle,
    method: &str,
    params: Map<String, Value>,
) -> Result<T, CommandError> {
    request_with_sensitivity(broker, method, params, true).await
}

async fn request_with_sensitivity<T: DeserializeOwned>(
    broker: &RuntimeBrokerHandle,
    method: &str,
    params: Map<String, Value>,
    secret: bool,
) -> Result<T, CommandError> {
    let payload = Map::from_iter([
        ("method".to_owned(), Value::String(method.to_owned())),
        ("params".to_owned(), Value::Object(params)),
    ]);
    let runtime_request = if secret {
        RuntimeRequest::secret(payload)
    } else {
        RuntimeRequest::normal(payload)
    };
    let frame = broker.request(runtime_request).await.map_err(|error| {
        CommandError::new(error.error_code(), "The SSH runtime is unavailable.")
    })?;

    match frame.message_type {
        MessageType::Response => {
            serde_json::from_value(Value::Object(frame.payload)).map_err(|_| {
                CommandError::new(
                    "SIDECAR_RESPONSE_INVALID",
                    "The SSH runtime returned an invalid response.",
                )
            })
        }
        MessageType::Error => {
            let code = frame
                .payload
                .get("error_code")
                .and_then(Value::as_str)
                .unwrap_or("SIDECAR_REQUEST_FAILED")
                .to_owned();
            let mut details = frame.payload;
            details.remove("error_code");
            details.remove("message");
            Err(CommandError::with_details(
                code,
                "The SSH runtime rejected the request.",
                Value::Object(details),
            ))
        }
        _ => Err(CommandError::new(
            "SIDECAR_RESPONSE_INVALID",
            "The SSH runtime returned an invalid response.",
        )),
    }
}

async fn get_connection(
    broker: &RuntimeBrokerHandle,
    connection_id: Uuid,
) -> Result<ConnectionProfile, CommandError> {
    let params = Map::from_iter([(
        "connection_id".to_owned(),
        Value::String(connection_id.to_string()),
    )]);
    request(broker, "connections.get", params)
        .await
        .map(|result: ConnectionResult| result.connection)
}

async fn get_direct_proxy_profile(
    broker: &RuntimeBrokerHandle,
    connection_id: Uuid,
) -> Result<ConnectionProfile, CommandError> {
    let profile = get_connection(broker, connection_id).await?;
    if profile.proxy_jump_id.is_some() {
        return Err(CommandError::new(
            "MULTI_HOP_PROXY_FORBIDDEN",
            "A ProxyJump profile cannot reference another ProxyJump profile.",
        ));
    }
    Ok(profile)
}

fn authentication_params(
    vault: &SecretVault,
    profile: &ConnectionProfile,
) -> Result<Map<String, Value>, CommandError> {
    let mut params = Map::from_iter([
        (
            "connection_id".to_owned(),
            Value::String(profile.connection_id.to_string()),
        ),
        (
            "profile_updated_at".to_owned(),
            Value::String(profile.updated_at.clone()),
        ),
    ]);
    append_authentication_params(&mut params, vault, profile)?;
    Ok(params)
}

fn append_authentication_params(
    params: &mut Map<String, Value>,
    vault: &SecretVault,
    profile: &ConnectionProfile,
) -> Result<(), CommandError> {
    match profile.auth_kind {
        AuthKind::Password => {
            let password = vault
                .resolve_secret(profile.credential_id, CredentialKind::SshPassword)
                .map_err(map_vault_error)?;
            params.insert(
                "password_b64".to_owned(),
                Value::String(STANDARD.encode(password.as_slice())),
            );
        }
        AuthKind::PrivateKey => {
            let private_key = vault
                .resolve_secret(profile.credential_id, CredentialKind::ImportedPrivateKey)
                .map_err(map_vault_error)?;
            params.insert(
                "private_key_b64".to_owned(),
                Value::String(STANDARD.encode(private_key.as_slice())),
            );
            if let Some(passphrase_id) = profile.passphrase_credential_id {
                let passphrase = vault
                    .resolve_secret(passphrase_id, CredentialKind::PrivateKeyPassphrase)
                    .map_err(map_vault_error)?;
                params.insert(
                    "passphrase_b64".to_owned(),
                    Value::String(STANDARD.encode(passphrase.as_slice())),
                );
            }
        }
    }
    Ok(())
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
