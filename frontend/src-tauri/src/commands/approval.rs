use serde::Serialize;

use super::CommandError;

#[derive(Debug, Serialize)]
pub struct ApprovalContext {
    pending: bool,
}

#[tauri::command]
pub fn get_approval_context() -> ApprovalContext {
    ApprovalContext { pending: false }
}

#[tauri::command]
pub fn submit_approval_decision() -> Result<(), CommandError> {
    Err(CommandError::new(
        "NO_PENDING_APPROVAL",
        "No approval request is pending.",
    ))
}
