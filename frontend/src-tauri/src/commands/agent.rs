use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use tauri::State;
use uuid::Uuid;

use crate::{
    sidecar::broker::RuntimeBrokerHandle,
    vault::{CredentialId, CredentialKind, VaultState},
};

use super::{
    connections::{request, request_secret},
    credentials::{lock_vault, map_vault_error},
    CommandError,
};

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ApiType {
    ChatCompletions,
    Responses,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ModelApiConfigInput {
    pub display_name: String,
    pub api_type: ApiType,
    pub base_url: String,
    pub model: String,
    pub api_key_secret_ref: CredentialId,
    pub enabled: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ModelApiConfig {
    pub api_config_id: Uuid,
    pub display_name: String,
    pub api_type: ApiType,
    pub base_url: String,
    pub model: String,
    pub api_key_secret_ref: CredentialId,
    pub enabled: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AgentRunStatus {
    Running,
    Completed,
    Failed,
    LimitReached,
    Cancelled,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AgentTurnResult {
    pub conversation_id: Uuid,
    pub agent_run_id: Uuid,
    pub status: AgentRunStatus,
    pub final_text: Option<String>,
    pub react_iteration: u8,
    pub error_code: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigsResult {
    configs: Vec<ModelApiConfig>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigResult {
    config: ModelApiConfig,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeleteResult {
    deleted: bool,
}

#[tauri::command]
pub async fn list_model_api_configs(
    broker: State<'_, RuntimeBrokerHandle>,
) -> Result<Vec<ModelApiConfig>, CommandError> {
    request(&broker, "agent.api_configs.list", Map::new())
        .await
        .map(|result: ConfigsResult| result.configs)
}

#[tauri::command]
pub async fn create_model_api_config(
    broker: State<'_, RuntimeBrokerHandle>,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    request(&broker, "agent.api_configs.create", object(input)?)
        .await
        .map(|result: ConfigResult| result.config)
}

#[tauri::command]
pub async fn update_model_api_config(
    broker: State<'_, RuntimeBrokerHandle>,
    api_config_id: Uuid,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    let mut params = object(input)?;
    params.insert(
        "api_config_id".to_owned(),
        Value::String(api_config_id.to_string()),
    );
    request(&broker, "agent.api_configs.update", params)
        .await
        .map(|result: ConfigResult| result.config)
}

#[tauri::command]
pub async fn delete_model_api_config(
    broker: State<'_, RuntimeBrokerHandle>,
    api_config_id: Uuid,
) -> Result<bool, CommandError> {
    let params = Map::from_iter([(
        "api_config_id".to_owned(),
        Value::String(api_config_id.to_string()),
    )]);
    request(&broker, "agent.api_configs.delete", params)
        .await
        .map(|result: DeleteResult| result.deleted)
}

#[tauri::command]
pub async fn run_agent_turn(
    broker: State<'_, RuntimeBrokerHandle>,
    vault: State<'_, VaultState>,
    conversation_id: Option<Uuid>,
    ssh_session_id: Uuid,
    api_config_id: Uuid,
    user_message: String,
) -> Result<AgentTurnResult, CommandError> {
    run_agent_turn_with_dependencies(
        &broker,
        &vault,
        conversation_id,
        ssh_session_id,
        api_config_id,
        user_message,
    )
    .await
}

#[doc(hidden)]
pub async fn run_agent_turn_with_dependencies(
    broker: &RuntimeBrokerHandle,
    vault: &VaultState,
    conversation_id: Option<Uuid>,
    ssh_session_id: Uuid,
    api_config_id: Uuid,
    user_message: String,
) -> Result<AgentTurnResult, CommandError> {
    let config: ModelApiConfig = request::<ConfigResult>(
        broker,
        "agent.api_configs.get",
        Map::from_iter([(
            "api_config_id".to_owned(),
            Value::String(api_config_id.to_string()),
        )]),
    )
    .await?
    .config;
    if config.api_config_id != api_config_id {
        return Err(CommandError::new(
            "SIDECAR_RESPONSE_INVALID",
            "The model configuration response identity did not match the request.",
        ));
    }

    // Resolve only the dedicated API Key kind after reading the current config. The Sidecar
    // compares this opaque ID again before provider invocation to close the lookup race.
    let api_key = lock_vault(vault)?
        .resolve_secret(config.api_key_secret_ref, CredentialKind::ApiKey)
        .map_err(map_vault_error)?;
    let params = object(serde_json::json!({
        "conversation_id": conversation_id,
        "ssh_session_id": ssh_session_id,
        "api_config_id": api_config_id,
        "api_key_credential_id": config.api_key_secret_ref,
        "api_key_b64": STANDARD.encode(api_key.as_slice()),
        "user_message": user_message,
    }))?;
    request_secret(broker, "agent.turn.run", params).await
}

fn object(value: impl Serialize) -> Result<Map<String, Value>, CommandError> {
    serde_json::to_value(value)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| {
            CommandError::new(
                "COMMAND_PAYLOAD_INVALID",
                "The Agent command payload could not be encoded.",
            )
        })
}
