use tauri::{AppHandle, Manager, State, WebviewUrl, WebviewWindowBuilder};

use crate::{app_state::RuntimeStateHandle, sidecar::RuntimeStatus};

use super::CommandError;

#[tauri::command]
pub fn get_runtime_status(state: State<'_, RuntimeStateHandle>) -> RuntimeStatus {
    state.status()
}

#[tauri::command]
pub fn open_approval_window(app: AppHandle) -> Result<(), CommandError> {
    if let Some(window) = app.get_webview_window("approval") {
        return window.set_focus().map_err(|_| {
            CommandError::new(
                "APPROVAL_WINDOW_FOCUS_FAILED",
                "The approval window could not be focused.",
            )
        });
    }

    WebviewWindowBuilder::new(&app, "approval", WebviewUrl::App("approval.html".into()))
        .title("Harness Shell Approval")
        .inner_size(760.0, 640.0)
        .build()
        .map(|_| ())
        .map_err(|_| {
            CommandError::new(
                "APPROVAL_WINDOW_OPEN_FAILED",
                "The approval window could not be opened.",
            )
        })
}
