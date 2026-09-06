//! 将打包 Backend stderr 捕获到每用户的单个有界轮转日志。

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

/// 拥有 Backend stderr 管道及其专用轮转文件写入线程。
pub struct BackendLogCapture {
    /// 可继承写入端的父进程副本，子进程启动后立即关闭。
    backend_write: Option<OwnedHandle>,
    /// 唯一读取任务；Backend 关闭继承写入端后等待其结束。
    worker: Option<JoinHandle<io::Result<()>>>,
}

impl BackendLogCapture {
    /// 在 Backend 能输出 stderr 前准备日志文件并开始排空。
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

    /// 返回 Backend 进程唯一允许继承的 stderr 句柄。
    pub fn backend_handle(&self) -> HANDLE {
        raw(self
            .backend_write
            .as_ref()
            .expect("Backend stderr handle is available before spawn"))
    }

    /// CreateProcess 完成继承后关闭父进程写入端副本。
    pub fn close_backend_end(&mut self) {
        self.backend_write.take();
    }

    /// 子进程关闭后等待读取线程，并暴露持久化写入失败。
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

/// 追加原始 Backend stderr 字节，并在固定大小边界轮转。
struct RotatingBackendLog {
    active_path: PathBuf,
    file: Option<File>,
    size: u64,
}

impl RotatingBackendLog {
    /// 打开追加目标前，先轮转此前已满的活动文件。
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

    /// 写入排空的分块；跨越大小限制前先轮转。
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

    /// 关闭活动文件、移动归档并重新打开空的追加目标。
    fn rotate(&mut self) -> io::Result<()> {
        self.file.take();
        rotate_archives(&self.active_path)?;
        self.file = Some(open_active_file(&self.active_path)?);
        self.size = 0;
        Ok(())
    }
}

/// 持续排空匿名管道，直到所有 Backend 写入端关闭。
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

/// 创建读取端绝不会泄露到子进程的匿名管道。
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

/// 以追加模式打开活动文件，保留之前未满文件中的启动日志。
fn open_active_file(path: &Path) -> io::Result<File> {
    OpenOptions::new().create(true).append(true).open(path)
}

/// 移动 .1 到 .4 归档，只删除最旧的有界归档。
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

/// 在活动 Backend 日志旁构建稳定数字归档路径。
fn archive_path(active_path: &Path, index: usize) -> PathBuf {
    active_path.with_extension(format!("log.{index}"))
}

fn raw(handle: &OwnedHandle) -> HANDLE {
    handle.as_raw_handle() as HANDLE
}
