use std::{
    mem::size_of,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
};

use windows_sys::Win32::{
    Foundation::HANDLE,
    System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicAccountingInformation,
        JobObjectExtendedLimitInformation, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
    },
};

use crate::error::LauncherError;

/// 关闭时终止进程的 Job，是两棵桌面子进程树的唯一所有者。
pub struct WindowsJob {
    handle: OwnedHandle,
}

impl WindowsJob {
    pub fn create() -> Result<Self, LauncherError> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(LauncherError::JobFailed);
        }
        let handle = unsafe { OwnedHandle::from_raw_handle(handle as _) };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if unsafe {
            SetInformationJobObject(
                raw(&handle),
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        } == 0
        {
            return Err(LauncherError::JobFailed);
        }
        Ok(Self { handle })
    }

    pub fn assign(&self, process: HANDLE) -> Result<(), LauncherError> {
        if unsafe { AssignProcessToJobObject(self.raw(), process) } == 0 {
            return Err(LauncherError::JobFailed);
        }
        Ok(())
    }

    pub fn terminate(&self) -> Result<(), LauncherError> {
        if unsafe { TerminateJobObject(self.raw(), 1) } == 0 {
            return Err(LauncherError::JobFailed);
        }
        Ok(())
    }

    pub fn active_processes(&self) -> Result<u32, LauncherError> {
        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        if unsafe {
            QueryInformationJobObject(
                self.raw(),
                JobObjectBasicAccountingInformation,
                &mut accounting as *mut _ as *mut _,
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(LauncherError::JobFailed);
        }
        Ok(accounting.ActiveProcesses)
    }

    pub(crate) fn raw(&self) -> HANDLE {
        raw(&self.handle)
    }
}

fn raw(handle: &OwnedHandle) -> HANDLE {
    handle.as_raw_handle() as HANDLE
}
