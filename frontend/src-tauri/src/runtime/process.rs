use std::{
    env,
    net::TcpListener,
    path::{Path, PathBuf},
};

#[cfg(target_os = "windows")]
use serde::Deserialize;
#[cfg(target_os = "windows")]
use tauri::{async_runtime::Receiver, AppHandle, Runtime};
#[cfg(target_os = "windows")]
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

#[cfg(target_os = "windows")]
use super::job::WindowsJob;

pub fn reserve_loopback_port() -> Result<u16, BackendProcessError> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

pub fn sidecar_args(port: u16) -> Vec<String> {
    vec!["serve".into(), "--port".into(), port.to_string()]
}

#[cfg(target_os = "windows")]
pub struct BackendProcessOwner {
    events: Receiver<CommandEvent>,
    child: Option<CommandChild>,
    stderr_buffer: Vec<u8>,
    job: WindowsJob,
}

#[cfg(target_os = "windows")]
impl BackendProcessOwner {
    pub fn spawn<R: Runtime>(
        app: &AppHandle<R>,
        extraction_directory: &Path,
        port: u16,
    ) -> Result<Self, BackendProcessError> {
        if port == 0 {
            return Err(BackendProcessError::InvalidEnvironment);
        }
        let extraction_directory = extraction_directory
            .to_str()
            .filter(|_| extraction_directory.is_absolute())
            .ok_or(BackendProcessError::InvalidEnvironment)?;
        let system_root =
            env::var_os("SystemRoot").ok_or(BackendProcessError::InvalidEnvironment)?;
        let system32 = PathBuf::from(&system_root).join("System32");
        if !system32.is_dir() {
            return Err(BackendProcessError::InvalidEnvironment);
        }
        let job = WindowsJob::create()?;
        let (events, child) = app
            .shell()
            .sidecar("harness-shell-sidecar")?
            .args(sidecar_args(port))
            .set_raw_out(true)
            .env_clear()
            .env("SystemRoot", &system_root)
            .env("WINDIR", &system_root)
            .env("TEMP", extraction_directory)
            .env("TMP", extraction_directory)
            .env("PATH", &system32)
            .env("USERNAME", "harness-shell")
            .env("USERPROFILE", extraction_directory)
            .env("HARNESS_SIDECAR_JOB", job.name())
            .spawn()?;
        Ok(Self {
            events,
            child: Some(child),
            stderr_buffer: Vec::new(),
            job,
        })
    }

    pub fn pid(&self) -> Option<u32> {
        self.child.as_ref().map(CommandChild::pid)
    }

    pub async fn next_event(&mut self) -> Result<BackendProcessEvent, BackendProcessError> {
        loop {
            let event = self
                .events
                .recv()
                .await
                .ok_or(BackendProcessError::EventChannelClosed)?;
            match event {
                CommandEvent::Stdout(bytes) if bytes.is_empty() => continue,
                CommandEvent::Stdout(_) => return Err(BackendProcessError::UnexpectedStdout),
                CommandEvent::Stderr(bytes) => self.consume_stderr(&bytes),
                CommandEvent::Terminated(payload) => {
                    self.flush_stderr();
                    self.child = None;
                    return Ok(BackendProcessEvent::Terminated { code: payload.code });
                }
                CommandEvent::Error(_) => return Err(BackendProcessError::EventChannelClosed),
                _ => return Err(BackendProcessError::EventChannelClosed),
            }
        }
    }

    pub fn kill(&mut self) -> Result<(), BackendProcessError> {
        self.job.terminate()?;
        self.child.take();
        Ok(())
    }

    fn consume_stderr(&mut self, bytes: &[u8]) {
        self.stderr_buffer.extend_from_slice(bytes);
        while let Some(newline) = self.stderr_buffer.iter().position(|byte| *byte == b'\n') {
            let mut line = self.stderr_buffer.drain(..=newline).collect::<Vec<_>>();
            if line.last() == Some(&b'\n') {
                line.pop();
            }
            if line.last() == Some(&b'\r') {
                line.pop();
            }
            log_sidecar_line(&line);
        }
    }

    fn flush_stderr(&mut self) {
        if !self.stderr_buffer.is_empty() {
            let line = std::mem::take(&mut self.stderr_buffer);
            log_sidecar_line(&line);
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BackendProcessEvent {
    Terminated { code: Option<i32> },
}

#[cfg(target_os = "windows")]
#[derive(Deserialize)]
struct SidecarLogEnvelope {
    component: String,
    level: String,
}

#[cfg(target_os = "windows")]
fn log_sidecar_line(line: &[u8]) {
    if line.is_empty() {
        return;
    }
    let level = serde_json::from_slice::<SidecarLogEnvelope>(line)
        .ok()
        .filter(|value| value.component == "python_sidecar")
        .and_then(|value| match value.level.as_str() {
            "INFO" => Some(log::Level::Info),
            "WARNING" => Some(log::Level::Warn),
            "ERROR" => Some(log::Level::Error),
            _ => None,
        })
        .unwrap_or(log::Level::Warn);
    log::log!(target: "harness_shell::sidecar", level, "{}", String::from_utf8_lossy(line));
}

#[derive(Debug, thiserror::Error)]
pub enum BackendProcessError {
    #[error("loopback port reservation failed")]
    Io(#[from] std::io::Error),
    #[cfg(target_os = "windows")]
    #[error("packaged backend operation failed")]
    Shell(#[from] tauri_plugin_shell::Error),
    #[cfg(target_os = "windows")]
    #[error("packaged backend process tree ownership failed")]
    Job(#[from] super::job::JobError),
    #[error("packaged backend event channel closed")]
    EventChannelClosed,
    #[error("packaged backend emitted unexpected stdout")]
    UnexpectedStdout,
    #[error("required Windows backend environment is unavailable")]
    InvalidEnvironment,
}
