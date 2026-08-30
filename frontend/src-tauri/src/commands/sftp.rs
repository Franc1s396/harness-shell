use std::sync::Arc;

use tauri::{AppHandle, State, WebviewWindow};
use tauri_plugin_dialog::DialogExt;
use uuid::Uuid;

use crate::sftp::{
    coordinator::{
        DeletePreflightInput, DownloadPreparationInput, MkdirInput, RemoveInput, RenameInput,
        SftpCoordinator, SftpCoordinatorState, TransferPreparationReceipt, UploadPreparationInput,
    },
    models::{
        DeletePlanSummary, ListingBatch, ManualSftpContext, ManualSftpError,
        OperationTerminalProjection, RecoveryAction, RecoverySummary, RemoteEntry, RemoteFileHash,
    },
    protocol::RecoveryResponse,
};

use super::CommandError;

const MAIN_WINDOW_LABEL: &str = "main";

#[tauri::command]
pub async fn get_manual_sftp_context(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Option<Uuid>,
) -> Result<ManualSftpContext, CommandError> {
    require_main_window(&window)?;
    let ssh_session_id = ssh_session_id.ok_or_else(|| {
        CommandError::new(
            "NO_SESSION",
            "Manual SFTP requires an explicitly active connected terminal tab.",
        )
    })?;
    state
        .coordinator()
        .open_context(ssh_session_id)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn list_manual_sftp_directory(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<ListingBatch, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .list_directory(ssh_session_id, &remote_path)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn next_manual_sftp_directory_batch(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    listing_id: Uuid,
    sequence: u32,
) -> Result<ListingBatch, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .next_directory_batch(listing_id, sequence)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn close_manual_sftp_listing(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    listing_id: Uuid,
) -> Result<bool, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .close_listing(listing_id)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn inspect_manual_sftp_entry(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<RemoteEntry, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .inspect_entry(ssh_session_id, &remote_path)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn hash_manual_sftp_file(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<RemoteFileHash, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .hash_file(ssh_session_id, &remote_path)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn open_manual_sftp_link(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<RemoteEntry, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .open_link(ssh_session_id, &remote_path)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn prepare_manual_sftp_upload(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_directory: String,
    target_name: String,
) -> Result<Option<TransferPreparationReceipt>, CommandError> {
    require_main_window(&window)?;
    let remote_path = join_remote_path(&remote_directory, &target_name)?;
    let (sender, receiver) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_file(move |selection| {
        let _ = sender.send(selection);
    });
    let selection = receiver.await.map_err(|_| {
        CommandError::new(
            "SFTP_UPLOAD_DIALOG_FAILED",
            "The upload file dialog did not return a result.",
        )
    })?;
    let Some(selection) = selection else {
        return Ok(None);
    };
    let local_path = selection.into_path().map_err(|_| {
        CommandError::new(
            "SFTP_LOCAL_PATH_INVALID",
            "The selected upload file path is invalid.",
        )
    })?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .prepare_upload(UploadPreparationInput {
            ssh_session_id,
            connection_id: context.connection_id,
            local_path,
            remote_path,
            host_label: context.host_label,
        })
        .await
        .map(Some)
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn execute_manual_sftp_upload(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    preparation_id: Uuid,
    confirmed: bool,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .execute_upload(preparation_id, confirmed)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn prepare_manual_sftp_download(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
    display_name: String,
) -> Result<Option<TransferPreparationReceipt>, CommandError> {
    require_main_window(&window)?;
    validate_basename(&display_name)?;
    let (sender, receiver) = tokio::sync::oneshot::channel();
    app.dialog()
        .file()
        .set_file_name(display_name)
        .save_file(move |selection| {
            let _ = sender.send(selection);
        });
    let selection = receiver.await.map_err(|_| {
        CommandError::new(
            "SFTP_DOWNLOAD_DIALOG_FAILED",
            "The download file dialog did not return a result.",
        )
    })?;
    let Some(selection) = selection else {
        return Ok(None);
    };
    let local_path = selection.into_path().map_err(|_| {
        CommandError::new(
            "SFTP_LOCAL_PATH_INVALID",
            "The selected download path is invalid.",
        )
    })?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .prepare_download(DownloadPreparationInput {
            ssh_session_id,
            connection_id: context.connection_id,
            local_path,
            remote_path,
            host_label: context.host_label,
        })
        .await
        .map(Some)
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn execute_manual_sftp_download(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    preparation_id: Uuid,
    confirmed: bool,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .execute_download(preparation_id, confirmed)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn discard_manual_sftp_preparation(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    preparation_id: Uuid,
) -> Result<(), CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .discard_preparation(preparation_id)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn create_manual_sftp_directory(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    parent_path: String,
    name: String,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    validate_basename(&name)?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .mkdir(MkdirInput {
            ssh_session_id,
            connection_id: context.connection_id,
            parent_path,
            name,
            host_label: context.host_label,
        })
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn rename_manual_sftp_entry(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    source_path: String,
    target_path: String,
    overwrite: bool,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .rename(RenameInput {
            ssh_session_id,
            connection_id: context.connection_id,
            source_path,
            target_path,
            overwrite,
            host_label: context.host_label,
        })
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn remove_manual_sftp_entry(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .remove(RemoveInput {
            ssh_session_id,
            connection_id: context.connection_id,
            path: remote_path,
            host_label: context.host_label,
        })
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn preflight_manual_sftp_delete(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    ssh_session_id: Uuid,
    remote_path: String,
) -> Result<DeletePlanSummary, CommandError> {
    require_main_window(&window)?;
    let coordinator = state.coordinator();
    let context = validated_context(&coordinator, ssh_session_id).await?;
    coordinator
        .preflight_delete(DeletePreflightInput {
            ssh_session_id,
            connection_id: context.connection_id,
            path: remote_path,
            host_label: context.host_label,
        })
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn execute_manual_sftp_delete(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    delete_plan_id: Uuid,
    confirmed: bool,
) -> Result<OperationTerminalProjection, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .execute_delete(delete_plan_id, confirmed)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub fn cancel_manual_sftp_operation(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    operation_id: Uuid,
) -> Result<(), CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .cancel(operation_id)
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn list_manual_sftp_recoveries(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
) -> Result<Vec<RecoverySummary>, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .list_recoveries()
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn inspect_manual_sftp_recovery(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    recovery_id: Uuid,
) -> Result<RecoveryResponse, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .inspect_recovery(recovery_id)
        .await
        .map_err(map_sftp_error)
}

#[tauri::command]
pub async fn execute_manual_sftp_recovery(
    window: WebviewWindow,
    state: State<'_, SftpCoordinatorState>,
    recovery_id: Uuid,
    action: RecoveryAction,
    confirmed: bool,
) -> Result<RecoveryResponse, CommandError> {
    require_main_window(&window)?;
    state
        .coordinator()
        .execute_recovery(recovery_id, action, confirmed)
        .await
        .map_err(map_sftp_error)
}

fn require_main_window(window: &WebviewWindow) -> Result<(), CommandError> {
    if window.label() != MAIN_WINDOW_LABEL {
        return Err(CommandError::new(
            "SFTP_WINDOW_FORBIDDEN",
            "Manual SFTP is available only from the fixed main window.",
        ));
    }
    Ok(())
}

async fn validated_context(
    coordinator: &Arc<SftpCoordinator>,
    ssh_session_id: Uuid,
) -> Result<ManualSftpContext, CommandError> {
    coordinator
        .open_context(ssh_session_id)
        .await
        .map_err(map_sftp_error)
}

fn join_remote_path(parent: &str, name: &str) -> Result<String, CommandError> {
    validate_basename(name)?;
    if parent.is_empty() || !parent.starts_with('/') || parent.contains('\0') {
        return Err(CommandError::new(
            "SFTP_PATH_INVALID",
            "The remote directory path is invalid.",
        ));
    }
    if parent == "/" {
        Ok(format!("/{name}"))
    } else {
        Ok(format!("{}/{name}", parent.trim_end_matches('/')))
    }
}

fn validate_basename(name: &str) -> Result<(), CommandError> {
    if name.is_empty()
        || matches!(name, "." | "..")
        || name.contains('/')
        || name.contains('\\')
        || name.contains('\0')
    {
        return Err(CommandError::new(
            "SFTP_PATH_INVALID",
            "The remote entry name is invalid.",
        ));
    }
    Ok(())
}

fn map_sftp_error(error: ManualSftpError) -> CommandError {
    CommandError::new(error.code(), error.message())
}
