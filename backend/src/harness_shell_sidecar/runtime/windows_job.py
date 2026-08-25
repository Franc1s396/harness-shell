"""Attach the packaged Python runtime to the Rust-owned Windows Job Object."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


JOB_OBJECT_ASSIGN_PROCESS = 0x0001


def attach_required_job() -> None:
    job_name = os.environ.pop("HARNESS_SIDECAR_JOB", None)
    if not job_name:
        raise RuntimeError("HARNESS_SIDECAR_JOB is required")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.OpenJobObjectW(JOB_OBJECT_ASSIGN_PROCESS, False, job_name)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(job)
