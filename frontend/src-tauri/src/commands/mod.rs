mod agent;
mod approval;
mod connections;
mod credentials;
mod runtime;
mod sftp;
mod terminal;

pub use agent::{
    create_model_api_config, delete_model_api_config, list_model_api_configs, run_agent_turn,
    run_agent_turn_with_dependencies, update_model_api_config, AgentRunStatus, AgentTurnResult,
    ApiType, ModelApiConfig, ModelApiConfigInput,
};
pub use approval::{get_approval_context, submit_approval_decision};
pub use connections::{
    confirm_host_key, connect_ssh, create_connection, delete_connection, disconnect_ssh,
    inspect_host_key, list_connections, replace_host_key, update_connection,
};
pub use credentials::{
    delete_model_api_key, delete_ssh_credential, import_private_key, store_model_api_key,
    store_private_key_passphrase, store_ssh_password,
};
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
pub use terminal::{close_pty, open_pty, resize_pty, write_pty};

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
}
