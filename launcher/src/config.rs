use std::{ffi::OsString, path::{Path, PathBuf}};

use crate::error::LauncherError;

/// 固定安装组件路径和唯一的每用户 Backend 数据目录。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LauncherConfig {
    /// 位于 Launcher 旁的独立 Tauri UI 可执行文件。
    pub ui_exe: PathBuf,
    /// 位于 Launcher 旁的打包 Python Backend 可执行文件。
    pub backend_exe: PathBuf,
    /// 仅传给 Backend 的 `%LOCALAPPDATA%\com.harnessshell.app` 目录。
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
