pub mod app_state;
pub mod commands;
pub mod protocol;
pub mod sidecar;
pub mod vault;

use std::{fs, sync::mpsc, time::Duration};

use app_state::RuntimeStateHandle;
use commands::{
    close_pty, confirm_host_key, connect_ssh, create_connection, delete_connection,
    delete_ssh_credential, disconnect_ssh, get_approval_context, get_runtime_status,
    import_private_key, inspect_host_key, list_connections, open_approval_window, open_pty,
    replace_host_key, resize_pty, store_private_key_passphrase, store_ssh_password,
    submit_approval_decision, update_connection, write_pty,
};
use sidecar::{
    broker::runtime_broker_channel, process::supervise_runtime, RuntimeState, RuntimeStatus,
};
use tauri::{Manager, RunEvent};
use vault::{SecretVault, VaultState};

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
            close_pty
        ])
        .setup(|app| {
            let app_data = app.path().app_local_data_dir()?;
            let extraction_directory = app_data.join("sidecar-tmp");
            fs::create_dir_all(&extraction_directory)?;
            let runtime_db_path = app_data.join("runtime.sqlite3");
            let vault = SecretVault::open(app_data.join("vault.sqlite3"))?;
            let runtime_keys = vault.get_or_create_runtime_keys()?;
            app.manage(VaultState::new(vault));

            let state = RuntimeStateHandle::new(RuntimeStatus::starting("desktop"));
            let (control_sender, control_receiver) = mpsc::channel();
            state.attach_control(control_sender);
            app.manage(state.clone());
            let (runtime_broker, broker_commands) = runtime_broker_channel();
            app.manage(runtime_broker);
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
        if matches!(event, RunEvent::ExitRequested { .. }) {
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
