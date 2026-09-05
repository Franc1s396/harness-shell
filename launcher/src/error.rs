use thiserror::Error;

/// Bounded Launcher failures safe to show in a native startup dialog.
#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum LauncherError {
    #[error("LAUNCHER_CONFIG_INVALID: The Harness Shell installation paths are invalid.")]
    ConfigInvalid,
    #[error("LAUNCHER_COMPONENT_MISSING: A required Harness Shell component is missing.")]
    ComponentMissing,
    #[error("LAUNCHER_DATA_DIRECTORY_FAILED: The application data directory could not be prepared.")]
    DataDirectoryFailed,
    #[error("LAUNCHER_BACKEND_LOG_FAILED: The Backend log file could not be written.")]
    BackendLogFailed,
    #[error("LAUNCHER_CONTROL_PIPE_FAILED: The Backend control channel could not be created.")]
    ControlPipeFailed,
    #[error("LAUNCHER_JOB_FAILED: The desktop process owner could not be created.")]
    JobFailed,
    #[error("LAUNCHER_BACKEND_START_FAILED: The Harness Shell Backend could not be started.")]
    BackendStartFailed,
    #[error("LAUNCHER_BACKEND_READY_FAILED: The Harness Shell Backend did not publish valid readiness.")]
    BackendReadyFailed,
    #[error("LAUNCHER_BACKEND_EXITED_EARLY: The Harness Shell Backend exited during startup.")]
    BackendExitedEarly,
    #[error("LAUNCHER_UI_START_FAILED: The Harness Shell window could not be started.")]
    UiStartFailed,
    #[error("LAUNCHER_PROCESS_WAIT_FAILED: Desktop process supervision failed.")]
    ProcessWaitFailed,
    #[error("LAUNCHER_SHUTDOWN_SIGNAL_FAILED: The Backend shutdown signal could not be sent.")]
    ShutdownSignalFailed,
}
