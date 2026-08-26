mod approval;
mod connections;
mod credentials;
mod runtime;
mod terminal;

pub use approval::{get_approval_context, submit_approval_decision};
pub use connections::{
    confirm_host_key, connect_ssh, create_connection, delete_connection, disconnect_ssh,
    inspect_host_key, list_connections, replace_host_key, update_connection,
};
pub use credentials::{
    delete_ssh_credential, import_private_key, store_private_key_passphrase, store_ssh_password,
};
pub use runtime::{get_runtime_status, open_approval_window};
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
