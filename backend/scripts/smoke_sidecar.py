"""Validate that the packaged executable emits one protocol v1 ready frame."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType


MAX_HEADER_BYTES = 8_192
READY_TIMEOUT_SECONDS = 5
STDERR_CAPTURE_LIMIT = 16_384
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class IoCounters(ctypes.Structure):
    """映射 Windows JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION 的 I/O 计数。"""

    #: ctypes 按 Windows API 声明顺序布局的字段列表。
    _fields_ = [
        # 已完成的读取操作次数。
        ("read_operations", ctypes.c_uint64),
        # 已完成的写入操作次数。
        ("write_operations", ctypes.c_uint64),
        # 既非读取也非写入的操作次数。
        ("other_operations", ctypes.c_uint64),
        # 已传输的读取字节数。
        ("read_bytes", ctypes.c_uint64),
        # 已传输的写入字节数。
        ("write_bytes", ctypes.c_uint64),
        # 其他 I/O 操作传输的字节数。
        ("other_bytes", ctypes.c_uint64),
    ]


class BasicLimitInformation(ctypes.Structure):
    """映射 Windows JOBOBJECT_BASIC_LIMIT_INFORMATION 结构。"""

    #: ctypes 按 Windows API ABI 顺序布局的基本限制字段。
    _fields_ = [
        # 单进程用户态 CPU 时间上限。
        ("per_process_user_time_limit", ctypes.c_int64),
        # 整个 Job 的用户态 CPU 时间上限。
        ("per_job_user_time_limit", ctypes.c_int64),
        # 指示哪些 Job 限制生效的位标志。
        ("limit_flags", ctypes.c_uint32),
        # 进程最小工作集大小。
        ("minimum_working_set_size", ctypes.c_size_t),
        # 进程最大工作集大小。
        ("maximum_working_set_size", ctypes.c_size_t),
        # Job 允许的活动进程数。
        ("active_process_limit", ctypes.c_uint32),
        # 允许进程运行的 CPU 亲和性掩码。
        ("affinity", ctypes.c_size_t),
        # 分配给进程的优先级类别。
        ("priority_class", ctypes.c_uint32),
        # Job 中进程的调度等级。
        ("scheduling_class", ctypes.c_uint32),
    ]


class ExtendedLimitInformation(ctypes.Structure):
    """映射 Windows JOBOBJECT_EXTENDED_LIMIT_INFORMATION 结构。"""

    #: ctypes 按 Windows API ABI 顺序布局的扩展限制字段。
    _fields_ = [
        # 基础 Job 限制。
        ("basic_limit_information", BasicLimitInformation),
        # Job 累计 I/O 计数。
        ("io_info", IoCounters),
        # 单进程内存上限。
        ("process_memory_limit", ctypes.c_size_t),
        # 整个 Job 的内存上限。
        ("job_memory_limit", ctypes.c_size_t),
        # Job 中单进程峰值内存用量。
        ("peak_process_memory_used", ctypes.c_size_t),
        # Job 全部进程峰值内存用量。
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


def create_job() -> tuple[int, str]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    name = f"HarnessShellSmoke-{os.getpid()}"
    handle = kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimitInformation()
    limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    return handle, name


def close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(handle)


def read_ready_frame(process: subprocess.Popen[bytes]) -> FrameEnvelope:
    assert process.stdout is not None
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        byte = process.stdout.read(1)
        if byte == b"":
            raise RuntimeError("packaged Sidecar exited before sidecar.ready")
        header.extend(byte)
        if len(header) > MAX_HEADER_BYTES:
            raise RuntimeError("packaged Sidecar emitted an oversized frame header")

    prefix = b"Content-Length: "
    if not header.startswith(prefix):
        raise RuntimeError("packaged Sidecar emitted an invalid frame header")
    length = int(header[len(prefix) : -4])
    body = process.stdout.read(length)
    if len(body) != length:
        raise RuntimeError("packaged Sidecar emitted a truncated ready frame")
    return FrameEnvelope.model_validate_json(body)


def main() -> int:
    executable = Path(sys.argv[1]).resolve(strict=True)
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise RuntimeError("SystemRoot is required for the Windows Sidecar")
    system32 = str(Path(system_root, "System32").resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="harness-shell-smoke-") as extraction_dir:
        job_handle, job_name = create_job()
        process = subprocess.Popen(
            [executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "SystemRoot": system_root,
                "WINDIR": system_root,
                "TEMP": extraction_dir,
                "TMP": extraction_dir,
                "PATH": system32,
                "USERNAME": "harness-shell",
                "USERPROFILE": extraction_dir,
                "HARNESS_SIDECAR_JOB": job_name,
            },
        )
        try:
            result: queue.Queue[FrameEnvelope | BaseException] = queue.Queue(maxsize=1)
            stderr_capture = bytearray()
            stderr_lock = threading.Lock()

            def drain_stderr() -> None:
                assert process.stderr is not None
                while chunk := process.stderr.read(4_096):
                    with stderr_lock:
                        remaining = STDERR_CAPTURE_LIMIT - len(stderr_capture)
                        if remaining > 0:
                            stderr_capture.extend(chunk[:remaining])

            def read_frame() -> None:
                try:
                    result.put(read_ready_frame(process))
                except BaseException as exc:
                    result.put(exc)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            threading.Thread(target=read_frame, daemon=True).start()
            try:
                frame_or_error = result.get(timeout=READY_TIMEOUT_SECONDS)
            except queue.Empty as exc:
                raise RuntimeError("packaged Sidecar ready frame timed out") from exc
            if isinstance(frame_or_error, BaseException):
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                stderr_thread.join(timeout=1)
                with stderr_lock:
                    stderr_text = stderr_capture.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"{frame_or_error}; packaged stderr:\n{stderr_text}"
                ) from frame_or_error
            frame = frame_or_error
            if frame.message_type is not MessageType.EVENT:
                raise RuntimeError("packaged Sidecar ready frame has the wrong message type")
            if frame.payload.get("event") != "sidecar.ready":
                raise RuntimeError("packaged Sidecar did not emit sidecar.ready")
            json.dumps(frame.payload, separators=(",", ":"))
            return 0
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            close_handle(job_handle)


if __name__ == "__main__":
    raise SystemExit(main())
