use std::{ffi::OsString, path::{Path, PathBuf}};

use crate::error::LauncherError;

/// Fixed installed component paths and the single per-user Backend data directory.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LauncherConfig {
    /// Independent Tauri UI executable beside the Launcher.
    pub ui_exe: PathBuf,
    /// Packaged Python Backend executable beside the Launcher.
    pub backend_exe: PathBuf,
    /// `%LOCALAPPDATA%\com.harnessshell.app`, passed only to the Backend.
    pub data_dir: PathBuf,
}

impl LauncherConfig {
    pub fn from_executable(executable: &Path) -> Result<Self, LauncherError> {
        if !executable.is_absolute() {
            return Err(LauncherError::ConfigInvalid);
        }
        let install_dir = executable.parent().ok_or(LauncherError::ConfigInvalid)?;
        let local_app_data = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .ok_or(LauncherError::ConfigInvalid)?;
        Ok(Self {
            ui_exe: install_dir.join("harness-shell-ui.exe"),
            backend_exe: install_dir.join("harness-shell-sidecar.exe"),
            data_dir: local_app_data.join("com.harnessshell.app"),
        })
    }

    pub fn backend_arguments(
        &self,
        control_read_handle: usize,
        ready_write_handle: usize,
    ) -> Vec<OsString> {
        vec![
            "desktop".into(),
            "--port".into(),
            "0".into(),
            "--data-dir".into(),
            self.data_dir.as_os_str().to_owned(),
            "--control-read-handle".into(),
            control_read_handle.to_string().into(),
            "--ready-write-handle".into(),
            ready_write_handle.to_string().into(),
        ]
    }

    pub fn ui_arguments(port: u16) -> Vec<OsString> {
        vec![
            "--backend-url".into(),
            format!("http://127.0.0.1:{port}").into(),
        ]
    }

    pub fn validate_installed_components(&self) -> Result<(), LauncherError> {
        if !self.ui_exe.is_file() || !self.backend_exe.is_file() {
            return Err(LauncherError::ComponentMissing);
        }
        Ok(())
    }
}
