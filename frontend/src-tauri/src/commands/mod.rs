mod agent;
mod approval;
mod connections;
mod credentials;
mod diagnostics;
mod runtime;
mod sftp;
mod terminal;

pub use agent::{
    create_model_api_config, create_model_api_config_with_runtime, delete_model_api_config,
    delete_model_api_config_with_runtime, list_model_api_configs,
    list_model_api_configs_with_runtime, run_agent_turn, run_agent_turn_with_dependencies,
    update_model_api_config, update_model_api_config_with_runtime, AgentRunStatus, AgentTurnResult,
    ApiType, ModelApiConfig, ModelApiConfigInput,
};
pub use approval::{get_approval_context, submit_approval_decision};
pub use connections::{
    confirm_host_key, confirm_host_key_with_runtime, connect_ssh, connect_ssh_with_dependencies,
    create_connection, create_connection_with_runtime, delete_connection,
    delete_connection_with_runtime, disconnect_ssh, disconnect_ssh_with_runtime, inspect_host_key,
    inspect_host_key_with_dependencies, list_connections, list_connections_with_runtime,
    replace_host_key, replace_host_key_with_runtime, update_connection,
    update_connection_with_runtime, AuthKind, ConnectionProfile, ConnectionProfileInput,
    ConnectionState, ConnectionStatus, HostKeyCandidate, HostKeyRecord, HostKeyStatus,
};
pub use credentials::{
    delete_model_api_key, delete_ssh_credential, import_private_key, store_model_api_key,
    store_private_key_passphrase, store_ssh_password,
};
pub use diagnostics::{get_log_directory, open_log_directory};
pub use runtime::{get_runtime_status, open_approval_window};
pub use sftp::{
    cancel_manual_sftp_operation, close_manual_sftp_listing, create_manual_sftp_directory,
    discard_manual_sftp_preparation, execute_manual_sftp_delete, execute_manual_sftp_download,
    execute_manual_sftp_recovery, execute_manual_sftp_upload, get_manual_sftp_context,
    hash_manual_sftp_file, inspect_manual_sftp_entry, inspect_manual_sftp_recovery,
    list_manual_sftp_directory, list_manual_sftp_recoveries, next_manual_sftp_directory_batch,
    open_manual_sftp_link, preflight_manual_sftp_delete, prepare_manual_sftp_download,
    prepare_manual_sftp_upload, remove_manual_sftp_entry, rename_manual_sftp_entry,
};
pub use terminal::{
    close_pty, close_pty_with_runtime, open_pty, open_pty_with_runtime, resize_pty,
    resize_pty_with_runtime, write_pty, write_pty_with_runtime,
};
pub use terminal::{PtySession, PtyState};

use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct CommandError {
    code: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    details: Option<serde_json::Value>,
}

impl CommandError {
    pub(crate) fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            details: None,
        }
    }

    pub(crate) fn with_details(
        code: impl Into<String>,
        message: impl Into<String>,
        details: serde_json::Value,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            details: Some(details),
        }
    }

    #[cfg(test)]
    pub(crate) fn code(&self) -> &str {
        &self.code
    }
}
