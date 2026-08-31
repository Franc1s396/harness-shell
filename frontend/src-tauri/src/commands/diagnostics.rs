//! Fixed-scope diagnostics commands exposed only to the main window.

use std::{io, path::Path, process::Command};

use tauri::{AppHandle, Manager};

use super::CommandError;

const LOG_DIRECTORY_RESOLVE_FAILED: &str = "LOG_DIRECTORY_RESOLVE_FAILED";
const LOG_DIRECTORY_UNAVAILABLE: &str = "LOG_DIRECTORY_UNAVAILABLE";
const LOG_DIRECTORY_ENCODING_INVALID: &str = "LOG_DIRECTORY_ENCODING_INVALID";
const LOG_DIRECTORY_OPEN_FAILED: &str = "LOG_DIRECTORY_OPEN_FAILED";

fn resolve_log_directory(app: &AppHandle) -> Result<std::path::PathBuf, CommandError> {
    app.path().app_log_dir().map_err(|_| {
        CommandError::new(
            LOG_DIRECTORY_RESOLVE_FAILED,
            "The application log directory could not be resolved.",
        )
    })
}

#[tauri::command]
pub fn get_log_directory(app: AppHandle) -> Result<String, CommandError> {
    let directory = resolve_log_directory(&app)?;
    if !directory.is_dir() {
        return Err(CommandError::new(
            LOG_DIRECTORY_UNAVAILABLE,
            "The application log directory is not available.",
        ));
    }
    directory.to_str().map(str::to_owned).ok_or_else(|| {
        CommandError::new(
            LOG_DIRECTORY_ENCODING_INVALID,
            "The application log directory cannot be represented as text.",
        )
    })
}

#[tauri::command]
pub fn open_log_directory(app: AppHandle) -> Result<(), CommandError> {
    let directory = resolve_log_directory(&app)?;
    open_log_directory_path(&directory, |path| {
        Command::new("explorer.exe").arg(path).spawn().map(|_| ())
    })
}

fn open_log_directory_path(
    directory: &Path,
    opener: impl FnOnce(&Path) -> io::Result<()>,
) -> Result<(), CommandError> {
    if !directory.is_dir() {
        return Err(CommandError::new(
            LOG_DIRECTORY_UNAVAILABLE,
            "The application log directory is not available.",
        ));
    }
    opener(directory).map_err(|_| {
        CommandError::new(
            LOG_DIRECTORY_OPEN_FAILED,
            "The application log directory could not be opened.",
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_log_directory_rejects_a_missing_fixed_directory() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("missing");
        let error = open_log_directory_path(&missing, |_| Ok(())).unwrap_err();
        assert_eq!(error.code(), "LOG_DIRECTORY_UNAVAILABLE");
    }

    #[test]
    fn open_log_directory_reports_explorer_start_failure() {
        let directory = tempfile::tempdir().unwrap();
        let error = open_log_directory_path(directory.path(), |_| {
            Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "explorer",
            ))
        })
        .unwrap_err();
        assert_eq!(error.code(), "LOG_DIRECTORY_OPEN_FAILED");
    }
}
