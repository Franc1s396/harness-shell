pub mod config;
pub mod control;
pub mod error;
pub mod job;
pub mod logging;
pub mod process;

use std::time::Duration;

use windows_sys::Win32::{
    Foundation::{WAIT_FAILED, WAIT_OBJECT_0},
    System::Threading::WaitForMultipleObjects,
};

use config::LauncherConfig;
use control::ControlPipes;
use error::LauncherError;
use job::WindowsJob;
use logging::BackendLogCapture;
use process::DesktopProcess;

const BACKEND_READY_TIMEOUT: Duration = Duration::from_secs(10);
const BACKEND_GRACEFUL_TIMEOUT: Duration = Duration::from_secs(3);

/// Start Backend, validate readiness, start UI, then supervise the fixed exit ordering.
pub fn run(config: LauncherConfig) -> Result<(), LauncherError> {
    config.validate_installed_components()?;
    std::fs::create_dir_all(&config.data_dir)
        .map_err(|_| LauncherError::DataDirectoryFailed)?;

    // Declared before the Job so error-path destruction kills children before
    // joining the stderr reader that waits for the inherited writer to close.
    let mut backend_log = BackendLogCapture::create(&config.data_dir)?;
    let job = WindowsJob::create()?;
    let mut pipes = ControlPipes::create()?;
    let mut inherited_handles = pipes.backend_handles().to_vec();
    inherited_handles.push(backend_log.backend_handle());
    let (control_read, ready_write) = pipes.backend_handle_values();
    let backend_arguments = config.backend_arguments(control_read, ready_write);
    let backend = DesktopProcess::spawn_suspended(
        &config.backend_exe,
        &backend_arguments,
        &inherited_handles,
        Some(backend_log.backend_handle()),
        &job,
    )
    .map_err(|_| LauncherError::BackendStartFailed)?;
    pipes.close_backend_ends();
    backend_log.close_backend_end();

    let ready = pipes.read_ready(backend.raw(), BACKEND_READY_TIMEOUT)?;
    if backend.wait_timeout(Duration::ZERO)? {
        return Err(LauncherError::BackendExitedEarly);
    }

    let ui = match DesktopProcess::spawn_suspended(
        &config.ui_exe,
        &LauncherConfig::ui_arguments(ready.port),
        &[],
        None,
        &job,
    ) {
        Ok(ui) => ui,
        Err(_) => {
            let _ = pipes.signal_shutdown();
            let _ = backend.wait_timeout(BACKEND_GRACEFUL_TIMEOUT);
            let _ = job.terminate();
            return Err(LauncherError::UiStartFailed);
        }
    };

    let children = [ui.raw(), backend.raw()];
    match unsafe { WaitForMultipleObjects(2, children.as_ptr(), 0, u32::MAX) } {
        WAIT_OBJECT_0 => {
            if !backend.wait_timeout(Duration::ZERO)? {
                if let Err(error) = pipes.signal_shutdown() {
                    let _ = job.terminate();
                    return Err(error);
                }
                if !backend.wait_timeout(BACKEND_GRACEFUL_TIMEOUT)? {
                    job.terminate()?;
                    let _ = backend.wait_timeout(BACKEND_GRACEFUL_TIMEOUT)?;
                }
            }
        }
        value if value == WAIT_OBJECT_0 + 1 => {
            // Never respawn. The UI owns the visible fatal-disconnect state and may exit later.
            pipes.close_control();
            ui.wait()?;
        }
        WAIT_FAILED => return Err(LauncherError::ProcessWaitFailed),
        _ => return Err(LauncherError::ProcessWaitFailed),
    }

    if job.active_processes()? != 0 {
        job.terminate()?;
    }
    backend_log.finish()?;
    Ok(())
}
