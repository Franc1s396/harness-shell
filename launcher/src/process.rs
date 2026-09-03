use std::{
    ffi::{OsStr, OsString},
    mem::size_of,
    os::windows::{ffi::OsStrExt, io::{AsRawHandle, FromRawHandle, OwnedHandle}},
    path::Path,
    time::Duration,
};

use windows_sys::Win32::{
    Foundation::{HANDLE, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT},
    System::Threading::{
        CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
        InitializeProcThreadAttributeList, ResumeThread, TerminateProcess,
        UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
        CREATE_UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
        PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTUPINFOEXW, STARTUPINFOW,
    },
};

use crate::{error::LauncherError, job::WindowsJob};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProcessStartError;

/// A resumed child whose process tree was assigned to the Launcher Job while suspended.
pub struct DesktopProcess {
    process: OwnedHandle,
    process_id: u32,
}

impl DesktopProcess {
    pub fn spawn_suspended(
        executable: &Path,
        arguments: &[OsString],
        inherited_handles: &[HANDLE],
        job: &WindowsJob,
    ) -> Result<Self, ProcessStartError> {
        let application = wide_null(executable.as_os_str());
        let mut command_line = command_line(executable.as_os_str(), arguments);
        let mut process_information = PROCESS_INFORMATION::default();

        let mut basic_startup = STARTUPINFOW::default();
        basic_startup.cb = size_of::<STARTUPINFOW>() as u32;
        let mut extended_startup = STARTUPINFOEXW::default();
        let mut attributes = None;
        let (startup, extended_flag) = if inherited_handles.is_empty() {
            (&basic_startup as *const STARTUPINFOW, 0)
        } else {
            let list = ProcessAttributeList::for_handles(inherited_handles)?;
            extended_startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
            extended_startup.lpAttributeList = list.pointer;
            attributes = Some(list);
            (
                &extended_startup.StartupInfo as *const STARTUPINFOW,
                EXTENDED_STARTUPINFO_PRESENT,
            )
        };

        let created = unsafe {
            CreateProcessW(
                application.as_ptr(),
                command_line.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                (!inherited_handles.is_empty()) as i32,
                CREATE_SUSPENDED
                    | CREATE_UNICODE_ENVIRONMENT
                    | CREATE_NO_WINDOW
                    | extended_flag,
                std::ptr::null(),
                std::ptr::null(),
                startup,
                &mut process_information,
            )
        };
        drop(attributes);
        if created == 0 {
            return Err(ProcessStartError);
        }

        let process = unsafe { OwnedHandle::from_raw_handle(process_information.hProcess as _) };
        let thread = unsafe { OwnedHandle::from_raw_handle(process_information.hThread as _) };
        if job.assign(raw(&process)).is_err() {
            terminate_suspended(raw(&process));
            return Err(ProcessStartError);
        }
        if unsafe { ResumeThread(raw(&thread)) } == u32::MAX {
            terminate_suspended(raw(&process));
            return Err(ProcessStartError);
        }
        drop(thread);
        Ok(Self {
            process,
            process_id: process_information.dwProcessId,
        })
    }

    pub fn raw(&self) -> HANDLE {
        raw(&self.process)
    }

    pub fn process_id(&self) -> u32 {
        self.process_id
    }

    pub fn wait_timeout(&self, timeout: Duration) -> Result<bool, LauncherError> {
        let milliseconds = timeout.as_millis().min(u32::MAX as u128) as u32;
        match unsafe { WaitForSingleObject(self.raw(), milliseconds) } {
            WAIT_OBJECT_0 => Ok(true),
            WAIT_TIMEOUT => Ok(false),
            WAIT_FAILED => Err(LauncherError::ProcessWaitFailed),
            _ => Err(LauncherError::ProcessWaitFailed),
        }
    }

    pub fn wait(&self) -> Result<(), LauncherError> {
        match unsafe { WaitForSingleObject(self.raw(), u32::MAX) } {
            WAIT_OBJECT_0 => Ok(()),
            _ => Err(LauncherError::ProcessWaitFailed),
        }
    }

    pub fn exit_code(&self) -> Result<u32, LauncherError> {
        let mut exit_code = 0u32;
        if unsafe { GetExitCodeProcess(self.raw(), &mut exit_code) } == 0 {
            return Err(LauncherError::ProcessWaitFailed);
        }
        Ok(exit_code)
    }
}

struct ProcessAttributeList {
    _storage: Vec<usize>,
    pointer: LPPROC_THREAD_ATTRIBUTE_LIST,
}

impl ProcessAttributeList {
    fn for_handles(handles: &[HANDLE]) -> Result<Self, ProcessStartError> {
        let mut byte_count = 0usize;
        unsafe {
            InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut byte_count);
        }
        if byte_count == 0 {
            return Err(ProcessStartError);
        }
        let mut storage = vec![0usize; byte_count.div_ceil(size_of::<usize>())];
        let pointer = storage.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
        if unsafe { InitializeProcThreadAttributeList(pointer, 1, 0, &mut byte_count) } == 0 {
            return Err(ProcessStartError);
        }
        if unsafe {
            UpdateProcThreadAttribute(
                pointer,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                handles.as_ptr() as *const _,
                std::mem::size_of_val(handles),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        } == 0
        {
            unsafe { DeleteProcThreadAttributeList(pointer) };
            return Err(ProcessStartError);
        }
        Ok(Self {
            _storage: storage,
            pointer,
        })
    }
}

impl Drop for ProcessAttributeList {
    fn drop(&mut self) {
        unsafe { DeleteProcThreadAttributeList(self.pointer) };
    }
}

fn terminate_suspended(process: HANDLE) {
    unsafe {
        TerminateProcess(process, 1);
        WaitForSingleObject(process, 5_000);
    }
}

fn raw(handle: &OwnedHandle) -> HANDLE {
    handle.as_raw_handle() as HANDLE
}

fn wide_null(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(std::iter::once(0)).collect()
}

fn command_line(executable: &OsStr, arguments: &[OsString]) -> Vec<u16> {
    let mut result = Vec::new();
    append_quoted(&mut result, executable);
    for argument in arguments {
        result.push(b' ' as u16);
        append_quoted(&mut result, argument);
    }
    result.push(0);
    result
}

/// Apply the Windows C runtime quoting rules so Python receives exact argument boundaries.
fn append_quoted(output: &mut Vec<u16>, value: &OsStr) {
    output.push(b'"' as u16);
    let mut backslashes = 0usize;
    for unit in value.encode_wide() {
        if unit == b'\\' as u16 {
            backslashes += 1;
            continue;
        }
        if unit == b'"' as u16 {
            output.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2 + 1));
            output.push(unit);
            backslashes = 0;
            continue;
        }
        output.extend(std::iter::repeat_n(b'\\' as u16, backslashes));
        backslashes = 0;
        output.push(unit);
    }
    output.extend(std::iter::repeat_n(b'\\' as u16, backslashes * 2));
    output.push(b'"' as u16);
}
