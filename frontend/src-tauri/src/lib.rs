pub mod app_state;
pub mod commands;
pub mod protocol;
pub mod sftp;
pub mod sidecar;
pub mod vault;

use std::{fs, sync::Arc, time::Duration};

use app_state::RuntimeStateHandle;
use commands::{
    cancel_manual_sftp_operation, close_manual_sftp_listing, close_pty, confirm_host_key,
    connect_ssh, create_connection, create_manual_sftp_directory, create_model_api_config,
    delete_connection, delete_model_api_config, delete_model_api_key, delete_ssh_credential,
    discard_manual_sftp_preparation, disconnect_ssh, execute_manual_sftp_delete,
    execute_manual_sftp_download, execute_manual_sftp_recovery, execute_manual_sftp_upload,
    get_approval_context, get_manual_sftp_context, get_runtime_status, hash_manual_sftp_file,
    import_private_key, inspect_host_key, inspect_manual_sftp_entry, inspect_manual_sftp_recovery,
    list_connections, list_manual_sftp_directory, list_manual_sftp_recoveries,
    list_model_api_configs, next_manual_sftp_directory_batch, open_approval_window,
    open_manual_sftp_link, open_pty, preflight_manual_sftp_delete, prepare_manual_sftp_download,
    prepare_manual_sftp_upload, remove_manual_sftp_entry, rename_manual_sftp_entry,
    replace_host_key, resize_pty, run_agent_turn, store_model_api_key,
    store_private_key_passphrase, store_ssh_password, submit_approval_decision, update_connection,
    update_model_api_config, write_pty,
};
use sftp::{
    coordinator::{
        SftpCoordinator, SftpCoordinatorState, TransferProgressSink, TransferProgressSinkError,
    },
    journal::LocalSftpOperationJournal,
    models::TransferProgressProjection,
    protocol::ManualSftpRuntimeClient,
};
use sidecar::{
    broker::runtime_broker_channel, process::supervise_runtime, RuntimeState, RuntimeStatus,
};
use tauri::{AppHandle, Emitter, Manager, RunEvent};
use vault::{SecretVault, VaultState};

const MAIN_WINDOW_LABEL: &str = "main";
const MANUAL_SFTP_TRANSFER_EVENT: &str = "manual-sftp://transfer-state";

/// Production transfer sink. The coordinator can only provide the typed, path-free projection,
/// and this owner fixes both the destination window and event name.
struct MainWindowTransferProgressSink {
    app: AppHandle,
}

impl TransferProgressSink for MainWindowTransferProgressSink {
    fn emit(
        &self,
        projection: TransferProgressProjection,
    ) -> Result<(), TransferProgressSinkError> {
        self.app
            .emit_to(MAIN_WINDOW_LABEL, MANUAL_SFTP_TRANSFER_EVENT, projection)
            .map_err(|_| TransferProgressSinkError::event_emit_failed())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_runtime_status,
            open_approval_window,
            get_approval_context,
            submit_approval_decision,
            store_ssh_password,
            store_private_key_passphrase,
            import_private_key,
            delete_ssh_credential,
            store_model_api_key,
            delete_model_api_key,
            list_connections,
            create_connection,
            update_connection,
            delete_connection,
            confirm_host_key,
            replace_host_key,
            inspect_host_key,
            connect_ssh,
            disconnect_ssh,
            open_pty,
            write_pty,
            resize_pty,
            close_pty,
            get_manual_sftp_context,
            list_manual_sftp_directory,
            next_manual_sftp_directory_batch,
            close_manual_sftp_listing,
            inspect_manual_sftp_entry,
            hash_manual_sftp_file,
            open_manual_sftp_link,
            prepare_manual_sftp_upload,
            execute_manual_sftp_upload,
            prepare_manual_sftp_download,
            execute_manual_sftp_download,
            discard_manual_sftp_preparation,
            create_manual_sftp_directory,
            rename_manual_sftp_entry,
            remove_manual_sftp_entry,
            preflight_manual_sftp_delete,
            execute_manual_sftp_delete,
            cancel_manual_sftp_operation,
            list_manual_sftp_recoveries,
            inspect_manual_sftp_recovery,
            execute_manual_sftp_recovery,
            list_model_api_configs,
            create_model_api_config,
            update_model_api_config,
            delete_model_api_config,
            run_agent_turn
        ])
        .setup(|app| {
            let app_data = app.path().app_local_data_dir()?;
            let extraction_directory = app_data.join("sidecar-tmp");
            fs::create_dir_all(&extraction_directory)?;
            let runtime_db_path = app_data.join("runtime.sqlite3");
            let manual_sftp_journal = LocalSftpOperationJournal::open(
                &app_data.join("manual-sftp.sqlite3"),
            )?;
            let vault = SecretVault::open(app_data.join("vault.sqlite3"))?;
            let runtime_keys = vault.get_or_create_runtime_keys()?;
            app.manage(VaultState::new(vault));

            let state = RuntimeStateHandle::new(RuntimeStatus::starting("desktop"));
            let (control_sender, control_receiver) = tokio::sync::mpsc::unbounded_channel();
            state.attach_control(control_sender);
            app.manage(state.clone());
            let (runtime_broker, broker_commands) = runtime_broker_channel();
            let coordinator = SftpCoordinatorState::new(SftpCoordinator::new_with_progress_sink(
                ManualSftpRuntimeClient::new(runtime_broker.clone()),
                manual_sftp_journal,
                Arc::new(MainWindowTransferProgressSink {
                    app: app.handle().clone(),
                }),
            ));
            app.manage(runtime_broker);
            app.manage(coordinator);
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(error) = supervise_runtime(
                    app_handle,
                    state,
                    control_receiver,
                    broker_commands,
                    &runtime_db_path,
                    &extraction_directory,
                    runtime_keys,
                )
                .await
                {
                    log::error!(target: "harness_shell::sidecar", "runtime supervisor stopped: {error}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            let coordinator = app_handle.state::<SftpCoordinatorState>();
            if coordinator.coordinator().active_transfer().is_some() {
                // The WebView normally presents the typed wait/cancel choice first. This Core
                // guard prevents a second window or direct close path from abandoning an active
                // journal while that user decision is still outstanding.
                api.prevent_exit();
                return;
            }
            // Stop accepting/preparing manual-SFTP work before the Sidecar broker is shut down.
            let shutdown = tauri::async_runtime::block_on(coordinator.shutdown());
            if !shutdown.drained() {
                log::warn!(
                    target: "harness_shell::sftp",
                    "manual SFTP shutdown reached its bounded drain timeout; durable non-terminal journals remain for recovery"
                );
            }
            let state = app_handle.state::<RuntimeStateHandle>();
            if matches!(
                state.status().state,
                RuntimeState::Starting | RuntimeState::Handshaking | RuntimeState::Ready
            ) {
                state.request_shutdown();
                let _ = state.wait_until_stopped(Duration::from_millis(3_500));
            }
        }
    });
}
