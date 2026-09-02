use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use tauri::State;
use uuid::Uuid;

use crate::{
    runtime::{
        CreateAgentApiConfigRequest, DeleteAgentApiConfigRequest, GetAgentApiConfigRequest,
        ListAgentApiConfigsRequest, RunAgentTurnRequest, RuntimeClient, RuntimeClientHandle,
        UpdateAgentApiConfigRequest,
    },
    vault::{CredentialId, CredentialKind, VaultState},
};

use super::{
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

#[tauri::command]
pub async fn list_model_api_configs(
    runtime: State<'_, RuntimeClientHandle>,
) -> Result<Vec<ModelApiConfig>, CommandError> {
    list_model_api_configs_with_runtime(&*runtime).await
}

#[doc(hidden)]
pub async fn list_model_api_configs_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
) -> Result<Vec<ModelApiConfig>, CommandError> {
    runtime
        .execute(ListAgentApiConfigsRequest)
        .await
        .map(|response| response.configs)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn create_model_api_config(
    runtime: State<'_, RuntimeClientHandle>,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    create_model_api_config_with_runtime(&*runtime, input).await
}

#[doc(hidden)]
pub async fn create_model_api_config_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    runtime
        .execute(CreateAgentApiConfigRequest(input))
        .await
        .map(|response| response.config)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn update_model_api_config(
    runtime: State<'_, RuntimeClientHandle>,
    api_config_id: Uuid,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    update_model_api_config_with_runtime(&*runtime, api_config_id, input).await
}

#[doc(hidden)]
pub async fn update_model_api_config_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    api_config_id: Uuid,
    input: ModelApiConfigInput,
) -> Result<ModelApiConfig, CommandError> {
    runtime
        .execute(UpdateAgentApiConfigRequest {
            api_config_id,
            input,
        })
        .await
        .map(|response| response.config)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn delete_model_api_config(
    runtime: State<'_, RuntimeClientHandle>,
    api_config_id: Uuid,
) -> Result<bool, CommandError> {
    delete_model_api_config_with_runtime(&*runtime, api_config_id).await
}

#[doc(hidden)]
pub async fn delete_model_api_config_with_runtime<R: RuntimeClient + ?Sized>(
    runtime: &R,
    api_config_id: Uuid,
) -> Result<bool, CommandError> {
    runtime
        .execute(DeleteAgentApiConfigRequest { api_config_id })
        .await
        .map(|response| response.deleted)
        .map_err(super::connections::map_runtime_error)
}

#[tauri::command]
pub async fn run_agent_turn(
    runtime: State<'_, RuntimeClientHandle>,
    vault: State<'_, VaultState>,
    conversation_id: Option<Uuid>,
    ssh_session_id: Uuid,
    api_config_id: Uuid,
    user_message: String,
) -> Result<AgentTurnResult, CommandError> {
    run_agent_turn_with_dependencies(
        &*runtime,
        &vault,
        conversation_id,
        ssh_session_id,
        api_config_id,
        user_message,
    )
    .await
}

#[doc(hidden)]
pub async fn run_agent_turn_with_dependencies<R: RuntimeClient + ?Sized>(
    runtime: &R,
    vault: &VaultState,
    conversation_id: Option<Uuid>,
    ssh_session_id: Uuid,
    api_config_id: Uuid,
    user_message: String,
) -> Result<AgentTurnResult, CommandError> {
    let config = runtime
        .execute(GetAgentApiConfigRequest { api_config_id })
        .await
        .map_err(super::connections::map_runtime_error)?
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
    runtime
        .execute(RunAgentTurnRequest::new(
            conversation_id,
            ssh_session_id,
            api_config_id,
            config.api_key_secret_ref,
            STANDARD.encode(api_key.as_slice()),
            user_message,
        ))
        .await
        .map(|response| AgentTurnResult {
            conversation_id: response.conversation_id,
            agent_run_id: response.agent_run_id,
            status: response.status,
            final_text: response.final_text,
            react_iteration: response.react_iteration,
            error_code: response.error_code,
        })
        .map_err(super::connections::map_runtime_error)
}
