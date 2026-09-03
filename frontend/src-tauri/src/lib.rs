pub mod commands;
mod logging;

use commands::{get_backend_bootstrap, BackendBootstrap, BackendBootstrapState};

const STARTUP_TITLE: &str = "Harness Shell startup error";
const BOOTSTRAP_INVALID_MESSAGE: &str =
    "BACKEND_BOOTSTRAP_INVALID: The Backend address supplied to Harness Shell is invalid.";
#[cfg(not(debug_assertions))]
const BOOTSTRAP_MISSING_MESSAGE: &str =
    "BACKEND_BOOTSTRAP_MISSING: Harness Shell must be started by its desktop Launcher.";
const SHELL_START_FAILED_MESSAGE: &str =
    "DESKTOP_SHELL_START_FAILED: The Harness Shell window could not be started.";

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if let Err(message) = run_application() {
        show_native_startup_error(message);
        std::process::exit(1);
    }
}

fn run_application() -> Result<(), &'static str> {
    let bootstrap =
        BackendBootstrap::from_args(std::env::args_os()).map_err(|_| BOOTSTRAP_INVALID_MESSAGE)?;
    #[cfg(not(debug_assertions))]
    if bootstrap.is_none() {
        return Err(BOOTSTRAP_MISSING_MESSAGE);
    }

    let app = tauri::Builder::default()
        .plugin(logging::plugin())
        .manage(BackendBootstrapState::new(bootstrap))
        .invoke_handler(tauri::generate_handler![get_backend_bootstrap])
        .setup(|_| {
            log::info!(target: "harness_shell::core", "application startup initialized");
            Ok(())
        })
        .build(tauri::generate_context!())
        .map_err(|_| SHELL_START_FAILED_MESSAGE)?;
    app.run(|_, _| {});
    Ok(())
}

#[cfg(windows)]
fn show_native_startup_error(message: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};

    let title = wide(STARTUP_TITLE);
    let message = wide(message);
    // The UTF-16 buffers remain alive throughout this synchronous native call.
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            message.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

#[cfg(not(windows))]
fn show_native_startup_error(message: &str) {
    eprintln!("{STARTUP_TITLE}: {message}");
}

#[cfg(windows)]
fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
