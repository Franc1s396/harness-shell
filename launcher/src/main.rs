// Prevent an additional console window for the installed Launcher.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::os::windows::ffi::OsStrExt;

use harness_shell_launcher::{config::LauncherConfig, error::LauncherError, run};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MessageBoxW, MB_ICONERROR, MB_OK,
};

fn main() {
    let result = std::env::current_exe()
        .map_err(|_| LauncherError::ConfigInvalid)
        .and_then(|path| LauncherConfig::from_executable(&path))
        .and_then(run);
    if let Err(error) = result {
        show_error(&error.to_string());
        std::process::exit(1);
    }
}

fn show_error(message: &str) {
    let title = wide("Harness Shell startup error");
    let message = wide(message);
    // Only the bounded `LauncherError` display text reaches this native dialog.
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            message.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

fn wide(value: &str) -> Vec<u16> {
    std::ffi::OsStr::new(value)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
