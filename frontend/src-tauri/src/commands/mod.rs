mod approval;
mod runtime;

pub use approval::{get_approval_context, submit_approval_decision};
pub use runtime::{get_runtime_status, open_approval_window};

use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct CommandError {
    code: &'static str,
    message: &'static str,
}

impl CommandError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }
}
