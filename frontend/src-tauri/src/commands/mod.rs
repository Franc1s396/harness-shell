mod bootstrap;

pub use bootstrap::{
    get_backend_bootstrap, BackendBootstrap, BackendBootstrapState, BootstrapArgumentError,
};

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
}
