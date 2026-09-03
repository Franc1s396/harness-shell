use std::{
    mem::size_of,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    time::{Duration, Instant},
};

use serde::Deserialize;
use uuid::Uuid;
use windows_sys::Win32::{
    Foundation::{
        HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0,
        SetHandleInformation,
    },
    Security::SECURITY_ATTRIBUTES,
    Storage::FileSystem::{ReadFile, WriteFile},
    System::{
        Pipes::{CreatePipe, PeekNamedPipe},
        Threading::WaitForSingleObject,
    },
};

use crate::error::LauncherError;

pub const READY_FRAME_MAX_JSON_BYTES: usize = 4_096;

/// Strict readiness payload emitted once by the Backend after it is listening.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ReadyFrame {
    pub version: u8,
    pub instance_id: Uuid,
    pub port: u16,
}

impl ReadyFrame {
    pub fn decode(payload: &[u8]) -> Result<Self, LauncherError> {
        if payload.is_empty() || payload.len() > READY_FRAME_MAX_JSON_BYTES {
            return Err(LauncherError::BackendReadyFailed);
        }
        let ready: Self = serde_json::from_slice(payload)
            .map_err(|_| LauncherError::BackendReadyFailed)?;
        if ready.version != 1 || ready.port == 0 {
            return Err(LauncherError::BackendReadyFailed);
        }
        Ok(ready)
    }
}

/// Two one-way anonymous pipes with only the two Backend ends inheritable.
pub struct ControlPipes {
    control_write: Option<OwnedHandle>,
    ready_read: Option<OwnedHandle>,
    backend_control_read: Option<OwnedHandle>,
    backend_ready_write: Option<OwnedHandle>,
}

impl ControlPipes {
    pub fn create() -> Result<Self, LauncherError> {
        let mut attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: std::ptr::null_mut(),
            bInheritHandle: 1,
        };
        let (mut control_read, mut control_write) = (std::ptr::null_mut(), std::ptr::null_mut());
        let (mut ready_read, mut ready_write) = (std::ptr::null_mut(), std::ptr::null_mut());
        unsafe {
            if CreatePipe(
                &mut control_read,
                &mut control_write,
                &mut attributes,
                0,
            ) == 0
            {
                return Err(LauncherError::ControlPipeFailed);
            }
        }
        let control_read = unsafe { owned(control_read) };
        let control_write = unsafe { owned(control_write) };
        unsafe {
            if CreatePipe(&mut ready_read, &mut ready_write, &mut attributes, 0) == 0 {
                return Err(LauncherError::ControlPipeFailed);
            }
        }
        let ready_read = unsafe { owned(ready_read) };
        let ready_write = unsafe { owned(ready_write) };
        if unsafe {
            SetHandleInformation(raw(&control_write), HANDLE_FLAG_INHERIT, 0) == 0
                || SetHandleInformation(raw(&ready_read), HANDLE_FLAG_INHERIT, 0) == 0
        } {
            return Err(LauncherError::ControlPipeFailed);
        }
        Ok(Self {
            control_write: Some(control_write),
            ready_read: Some(ready_read),
            backend_control_read: Some(control_read),
            backend_ready_write: Some(ready_write),
        })
    }

    pub fn backend_handles(&self) -> [HANDLE; 2] {
        [
            raw(self.backend_control_read.as_ref().expect("Backend control handle")),
            raw(self.backend_ready_write.as_ref().expect("Backend ready handle")),
        ]
    }

    pub fn backend_handle_values(&self) -> (usize, usize) {
        let [control, ready] = self.backend_handles();
        (control as usize, ready as usize)
    }

    /// Close the parent copies immediately after the Backend process is created.
    pub fn close_backend_ends(&mut self) {
        self.backend_control_read.take();
        self.backend_ready_write.take();
    }

    pub fn read_ready(
        &mut self,
        backend_process: HANDLE,
        timeout: Duration,
    ) -> Result<ReadyFrame, LauncherError> {
        let handle = raw(self.ready_read.as_ref().ok_or(LauncherError::BackendReadyFailed)?);
        let prefix = read_exact_bounded(handle, backend_process, 4, timeout)?;
        let length = u32::from_be_bytes(prefix.try_into().expect("four-byte prefix")) as usize;
        if !(1..=READY_FRAME_MAX_JSON_BYTES).contains(&length) {
            return Err(LauncherError::BackendReadyFailed);
        }
        let payload = read_exact_bounded(handle, backend_process, length, timeout)?;
        self.ready_read.take();
        ReadyFrame::decode(&payload)
    }

    pub fn signal_shutdown(&mut self) -> Result<(), LauncherError> {
        let handle = self.control_write.take().ok_or(LauncherError::ShutdownSignalFailed)?;
        let mut written = 0u32;
        let byte = [0x01u8];
        let result = unsafe {
            WriteFile(
                raw(&handle),
                byte.as_ptr(),
                byte.len() as u32,
                &mut written,
                std::ptr::null_mut(),
            )
        };
        drop(handle);
        if result == 0 || written != 1 {
            return Err(LauncherError::ShutdownSignalFailed);
        }
        Ok(())
    }

    pub fn close_control(&mut self) {
        self.control_write.take();
    }
}

fn read_exact_bounded(
    pipe: HANDLE,
    backend_process: HANDLE,
    length: usize,
    timeout: Duration,
) -> Result<Vec<u8>, LauncherError> {
    let deadline = Instant::now() + timeout;
    let mut result = vec![0u8; length];
    let mut offset = 0;
    while offset < length {
        if unsafe { WaitForSingleObject(backend_process, 0) } == WAIT_OBJECT_0 {
            return Err(LauncherError::BackendExitedEarly);
        }
        let mut available = 0u32;
        if unsafe {
            PeekNamedPipe(
                pipe,
                std::ptr::null_mut(),
                0,
                std::ptr::null_mut(),
                &mut available,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(LauncherError::BackendReadyFailed);
        }
        if available == 0 {
            if Instant::now() >= deadline {
                return Err(LauncherError::BackendReadyFailed);
            }
            std::thread::sleep(Duration::from_millis(10));
            continue;
        }
        let requested = (length - offset).min(available as usize);
        let mut read = 0u32;
        if unsafe {
            ReadFile(
                pipe,
                result[offset..offset + requested].as_mut_ptr(),
                requested as u32,
                &mut read,
                std::ptr::null_mut(),
            )
        } == 0
            || read == 0
        {
            return Err(LauncherError::BackendReadyFailed);
        }
        offset += read as usize;
    }
    Ok(result)
}

fn raw(handle: &OwnedHandle) -> HANDLE {
    handle.as_raw_handle() as HANDLE
}

unsafe fn owned(handle: HANDLE) -> OwnedHandle {
    OwnedHandle::from_raw_handle(handle as _)
}
