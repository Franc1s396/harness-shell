pub mod app_state;
pub mod protocol;
pub mod sidecar;
pub mod vault;

use std::{fs, sync::mpsc, time::Duration};

use app_state::RuntimeStateHandle;
use sidecar::{process::supervise_runtime, RuntimeState, RuntimeStatus};
use tauri::{Manager, RunEvent};
use vault::SecretVault;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let app_data = app.path().app_local_data_dir()?;
            let extraction_directory = app_data.join("sidecar-tmp");
            fs::create_dir_all(&extraction_directory)?;
            let runtime_db_path = app_data.join("runtime.sqlite3");
            let runtime_keys = {
                let vault = SecretVault::open(app_data.join("vault.sqlite3"))?;
                vault.get_or_create_runtime_keys()?
            };

            let state = RuntimeStateHandle::new(RuntimeStatus::starting("desktop"));
            let (control_sender, control_receiver) = mpsc::channel();
            state.attach_control(control_sender);
            app.manage(state.clone());
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(error) = supervise_runtime(
                    app_handle,
                    state,
                    control_receiver,
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
