//! Capture packaged Backend stderr in one bounded per-user rotating log.

use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    mem::size_of,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    path::{Path, PathBuf},
    thread::{self, JoinHandle},
};

use windows_sys::Win32::{
    Foundation::{SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT},
    Security::SECURITY_ATTRIBUTES,
    System::Pipes::CreatePipe,
};

use crate::error::LauncherError;

pub const BACKEND_LOG_FILE_NAME: &str = "harness-shell-backend.log";
pub const MAX_BACKEND_LOG_FILE_SIZE_BYTES: u64 = 10 * 1024 * 1024;
pub const ARCHIVED_BACKEND_LOG_FILE_COUNT: usize = 4;

/// Own the Backend stderr pipe and its dedicated rotating file writer thread.
pub struct BackendLogCapture {
    /// Parent copy of the inheritable writer, closed immediately after child spawn.
    backend_write: Option<OwnedHandle>,
    /// Sole reader task; joined after the Backend closes its inherited writer.
    worker: Option<JoinHandle<io::Result<()>>>,
}

impl BackendLogCapture {
    /// Prepare the log file and start draining before the Backend can emit stderr.
    pub fn create(data_dir: &Path) -> Result<Self, LauncherError> {
        let log_dir = data_dir.join("logs");
        fs::create_dir_all(&log_dir).map_err(|_| LauncherError::BackendLogFailed)?;
        let writer = RotatingBackendLog::open(log_dir.join(BACKEND_LOG_FILE_NAME))
            .map_err(|_| LauncherError::BackendLogFailed)?;
        let (read, write) = stderr_pipe()?;
        let worker = thread::Builder::new()
            .name("harness-shell-backend-log".to_owned())
            .spawn(move || drain_stderr(read, writer))
            .map_err(|_| LauncherError::BackendLogFailed)?;
        Ok(Self {
            backend_write: Some(write),
            worker: Some(worker),
        })
    }

    /// Return the only stderr handle that the Backend process may inherit.
    pub fn backend_handle(&self) -> HANDLE {
        raw(self
            .backend_write
            .as_ref()
            .expect("Backend stderr handle is available before spawn"))
    }

    /// Close the parent writer copy once CreateProcess has inherited it.
    pub fn close_backend_end(&mut self) {
        self.backend_write.take();
    }

    /// Join the reader after child shutdown and expose any persistent write failure.
    pub fn finish(&mut self) -> Result<(), LauncherError> {
        self.close_backend_end();
        let worker = self.worker.take().ok_or(LauncherError::BackendLogFailed)?;
        worker
            .join()
            .map_err(|_| LauncherError::BackendLogFailed)?
            .map_err(|_| LauncherError::BackendLogFailed)
    }
}

impl Drop for BackendLogCapture {
    fn drop(&mut self) {
        self.close_backend_end();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

/// Append raw Backend stderr bytes while rotating at the fixed size boundary.
struct RotatingBackendLog {
    active_path: PathBuf,
    file: Option<File>,
    size: u64,
}

impl RotatingBackendLog {
    /// Rotate a previously full active file before opening the append target.
    fn open(active_path: PathBuf) -> io::Result<Self> {
        let existing_size = fs::metadata(&active_path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        if existing_size >= MAX_BACKEND_LOG_FILE_SIZE_BYTES {
            rotate_archives(&active_path)?;
        }
        let file = open_active_file(&active_path)?;
        let size = file.metadata()?.len();
        Ok(Self {
            active_path,
            file: Some(file),
            size,
        })
    }

    /// Write one drained chunk, rotating before it would cross the size limit.
    fn write_chunk(&mut self, bytes: &[u8]) -> io::Result<()> {
        if self.size > 0
            && self.size.saturating_add(bytes.len() as u64) > MAX_BACKEND_LOG_FILE_SIZE_BYTES
        {
            self.rotate()?;
        }
        self.file
            .as_mut()
            .expect("active Backend log file")
            .write_all(bytes)?;
        self.size = self.size.saturating_add(bytes.len() as u64);
        Ok(())
    }

    /// Close the active file, shift archives, and reopen an empty append target.
    fn rotate(&mut self) -> io::Result<()> {
        self.file.take();
        rotate_archives(&self.active_path)?;
        self.file = Some(open_active_file(&self.active_path)?);
        self.size = 0;
        Ok(())
    }
}

/// Drain the anonymous pipe until every Backend writer has closed.
fn drain_stderr(read: OwnedHandle, mut writer: RotatingBackendLog) -> io::Result<()> {
    let mut pipe = File::from(read);
    let mut buffer = [0u8; 8 * 1024];
    loop {
        let count = pipe.read(&mut buffer)?;
        if count == 0 {
            return Ok(());
        }
        writer.write_chunk(&buffer[..count])?;
    }
}

/// Create one anonymous pipe whose read end can never leak into a child.
fn stderr_pipe() -> Result<(OwnedHandle, OwnedHandle), LauncherError> {
    let mut attributes = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: 1,
    };
    let (mut read, mut write) = (std::ptr::null_mut(), std::ptr::null_mut());
    if unsafe { CreatePipe(&mut read, &mut write, &mut attributes, 0) } == 0 {
        return Err(LauncherError::BackendLogFailed);
    }
    let read = unsafe { OwnedHandle::from_raw_handle(read as _) };
    let write = unsafe { OwnedHandle::from_raw_handle(write as _) };
    if unsafe { SetHandleInformation(raw(&read), HANDLE_FLAG_INHERIT, 0) } == 0 {
        return Err(LauncherError::BackendLogFailed);
    }
    Ok((read, write))
}

/// Open the active file in append mode so prior non-full startup logs remain visible.
fn open_active_file(path: &Path) -> io::Result<File> {
    OpenOptions::new().create(true).append(true).open(path)
}

/// Shift `.1` through `.4`, deleting only the oldest bounded archive.
fn rotate_archives(active_path: &Path) -> io::Result<()> {
    for index in (1..=ARCHIVED_BACKEND_LOG_FILE_COUNT).rev() {
        let target = archive_path(active_path, index);
        if target.exists() {
            fs::remove_file(&target)?;
        }
        let source = if index == 1 {
            active_path.to_path_buf()
        } else {
            archive_path(active_path, index - 1)
        };
        if source.exists() {
            fs::rename(source, target)?;
        }
    }
    Ok(())
}

/// Build the stable numeric archive path beside the active Backend log.
fn archive_path(active_path: &Path, index: usize) -> PathBuf {
    active_path.with_extension(format!("log.{index}"))
}

fn raw(handle: &OwnedHandle) -> HANDLE {
    handle.as_raw_handle() as HANDLE
}
