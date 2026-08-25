use std::{ffi::c_void, mem::size_of, ptr};

use uuid::Uuid;
use windows_sys::Win32::{
    Foundation::{CloseHandle, GetLastError, HANDLE},
    System::JobObjects::{
        CreateJobObjectW, JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
        QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    },
};

#[derive(Debug, thiserror::Error)]
#[error("Windows Job Object operation {operation} failed with code {code}")]
pub struct JobError {
    operation: &'static str,
    code: u32,
}

pub struct WindowsJob {
    handle: HANDLE,
    name: String,
}

// SAFETY: Windows kernel handles may be used and closed from any process thread.
unsafe impl Send for WindowsJob {}
// SAFETY: all exposed operations are immutable kernel calls on a stable handle.
unsafe impl Sync for WindowsJob {}

impl WindowsJob {
    pub fn create() -> Result<Self, JobError> {
        let name = format!("HarnessShellSidecar-{}", Uuid::new_v4());
        let wide_name: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        // SAFETY: the name is a live, null-terminated UTF-16 buffer and security attrs are null.
        let handle = unsafe { CreateJobObjectW(ptr::null(), wide_name.as_ptr()) };
        if handle.is_null() {
            return Err(last_error("CreateJobObjectW"));
        }

        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: the information pointer and byte length match the selected information class.
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&raw const limits).cast::<c_void>(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            let error = last_error("SetInformationJobObject");
            // SAFETY: handle was returned by CreateJobObjectW and is owned here.
            unsafe { CloseHandle(handle) };
            return Err(error);
        }
        Ok(Self { handle, name })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn terminate(&self) -> Result<(), JobError> {
        // SAFETY: handle remains valid for the lifetime of self.
        if unsafe { TerminateJobObject(self.handle, 1) } == 0 {
            return Err(last_error("TerminateJobObject"));
        }
        Ok(())
    }

    pub fn active_processes(&self) -> Result<u32, JobError> {
        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        // SAFETY: the output pointer and byte length match the selected information class.
        let queried = unsafe {
            QueryInformationJobObject(
                self.handle,
                JobObjectBasicAccountingInformation,
                (&raw mut accounting).cast::<c_void>(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                ptr::null_mut(),
            )
        };
        if queried == 0 {
            return Err(last_error("QueryInformationJobObject"));
        }
        Ok(accounting.ActiveProcesses)
    }
}

impl Drop for WindowsJob {
    fn drop(&mut self) {
        // SAFETY: handle is uniquely owned and closed exactly once.
        unsafe { CloseHandle(self.handle) };
    }
}

fn last_error(operation: &'static str) -> JobError {
    // SAFETY: GetLastError has no preconditions and follows a failed Win32 call.
    JobError {
        operation,
        code: unsafe { GetLastError() },
    }
}
