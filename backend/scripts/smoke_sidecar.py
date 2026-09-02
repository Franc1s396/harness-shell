"""Validate the packaged loopback HTTP/WebSocket backend lifecycle."""

from __future__ import annotations

import base64
import ctypes
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from websockets.sync.client import connect


READY_TIMEOUT_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 1
CAPTURE_LIMIT = 65_536
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class IoCounters(ctypes.Structure):
    """Map the Windows Job Object I/O accounting structure."""

    _fields_ = [
        ("read_operations", ctypes.c_uint64),
        ("write_operations", ctypes.c_uint64),
        ("other_operations", ctypes.c_uint64),
        ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64),
        ("other_bytes", ctypes.c_uint64),
    ]


class BasicLimitInformation(ctypes.Structure):
    """Map the Windows Job Object basic limit structure."""

    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class ExtendedLimitInformation(ctypes.Structure):
    """Map the Windows Job Object extended limit structure."""

    _fields_ = [
        ("basic_limit_information", BasicLimitInformation),
        ("io_info", IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class BasicAccountingInformation(ctypes.Structure):
    """Map the Job Object process-count accounting fields."""

    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("this_period_total_user_time", ctypes.c_int64),
        ("this_period_total_kernel_time", ctypes.c_int64),
        ("total_page_fault_count", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


def _kernel32():
    """Configure the small allowlisted Windows Job Object API surface."""

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
    kernel32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def create_job() -> tuple[int, str]:
    """Create the named kill-on-close owner required by the packaged child."""

    kernel32 = _kernel32()
    name = f"HarnessShellSmoke-{os.getpid()}-{uuid4()}"
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


def active_job_processes(handle: int) -> int:
    """Return the exact remaining process count for cleanup verification."""

    kernel32 = _kernel32()
    accounting = BasicAccountingInformation()
    if not kernel32.QueryInformationJobObject(
        handle,
        JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(accounting.active_processes)


def close_handle(handle: int) -> None:
    """Close the owned Job Object handle exactly once."""

    _kernel32().CloseHandle(handle)


def reserve_loopback_port() -> int:
    """Reserve and release one dynamic loopback port before child spawn."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request_json(
    port: int,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    expected_status: int,
) -> tuple[dict[str, object], bytes]:
    """Issue one correlated private HTTP request and validate its typed response."""

    request_id = uuid4()
    encoded = None
    headers = {
        "X-Request-ID": str(request_id),
        "Accept": "application/json, application/problem+json",
    }
    if payload is not None:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    try:
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        body = response.read(1_048_577)
        if response.status != expected_status:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        if len(body) > 1_048_576:
            raise RuntimeError(f"{path} returned an oversized body")
        if response.getheader("X-Request-ID") != str(request_id):
            raise RuntimeError(f"{path} response correlation failed")
        decoded = json.loads(body)
        if decoded.get("request_id") != str(request_id):
            raise RuntimeError(f"{path} response body correlation failed")
        return decoded, body
    finally:
        connection.close()


def wait_live(port: int) -> bytes:
    """Poll liveness within the fixed startup deadline."""

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response, body = request_json(
                port,
                "GET",
                "/v1/health/live",
                expected_status=200,
            )
            if response == {"request_id": response["request_id"], "live": True}:
                return body
            raise RuntimeError("liveness response contract failed")
        except (ConnectionError, OSError):
            time.sleep(0.025)
    raise RuntimeError("packaged backend liveness timed out")


def drain_pipe(pipe, capture: bytearray, lock: threading.Lock) -> None:
    """Drain a child pipe so diagnostics cannot block process shutdown."""

    while chunk := pipe.read(4_096):
        with lock:
            remaining = CAPTURE_LIMIT - len(capture)
            if remaining > 0:
                capture.extend(chunk[:remaining])


def main() -> int:
    """Run live, initialize, ready, heartbeat, shutdown, and Job cleanup checks."""

    executable = Path(sys.argv[1]).resolve(strict=True)
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise RuntimeError("SystemRoot is required for the Windows backend")
    system32 = str(Path(system_root, "System32").resolve(strict=True))
    port = reserve_loopback_port()
    with tempfile.TemporaryDirectory(prefix="harness-shell-smoke-") as extraction_dir:
        job_handle, job_name = create_job()
        process = subprocess.Popen(
            [executable, "serve", "--port", str(port)],
            stdin=subprocess.DEVNULL,
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
        stdout_capture = bytearray()
        stderr_capture = bytearray()
        stdout_lock = threading.Lock()
        stderr_lock = threading.Lock()
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=drain_pipe,
            args=(process.stdout, stdout_capture, stdout_lock),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain_pipe,
            args=(process.stderr, stderr_capture, stderr_lock),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            observed_bodies = [wait_live(port)]
            runtime_data_key = bytes([0x31]) * 32
            audit_hmac_key = bytes([0x57]) * 32
            runtime_key_marker = base64.b64encode(runtime_data_key).decode("ascii")
            initialized, initialized_body = request_json(
                port,
                "POST",
                "/v1/runtime/initialize",
                payload={
                    "app_version": "packaged-smoke",
                    "runtime_db_path": str(
                        Path(extraction_dir, "runtime.sqlite3").resolve()
                    ),
                    "runtime_data_key_b64": runtime_key_marker,
                    "audit_hmac_key_b64": base64.b64encode(
                        audit_hmac_key
                    ).decode("ascii"),
                    "heartbeat_interval_ms": 5_000,
                    "heartbeat_timeout_ms": 15_000,
                },
                expected_status=200,
            )
            observed_bodies.append(initialized_body)
            if initialized["state"] != "READY":
                raise RuntimeError("initialize did not publish READY")
            ready, ready_body = request_json(
                port,
                "GET",
                "/v1/health/ready",
                expected_status=200,
            )
            observed_bodies.append(ready_body)
            if ready["ready"] is not True or ready["state"] != "READY":
                raise RuntimeError("ready response contract failed")

            with connect(
                f"ws://127.0.0.1:{port}/v1/runtime/events",
                open_timeout=HTTP_TIMEOUT_SECONDS,
                close_timeout=HTTP_TIMEOUT_SECONDS,
            ) as websocket:
                ping_id = uuid4()
                websocket.send(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "type": "runtime.ping",
                            "message_id": str(ping_id),
                            "causation_id": None,
                            "timestamp": "2026-09-02T00:00:00Z",
                            "payload": {
                                "client_timestamp": "2026-09-02T00:00:00Z"
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                pong = json.loads(websocket.recv(timeout=HTTP_TIMEOUT_SECONDS))
                if pong.get("type") != "runtime.pong":
                    raise RuntimeError("runtime WebSocket did not return pong")
                if UUID(pong["causation_id"]) != ping_id:
                    raise RuntimeError("runtime pong correlation failed")

            stopped, stopped_body = request_json(
                port,
                "POST",
                "/v1/runtime/shutdown",
                expected_status=202,
            )
            observed_bodies.append(stopped_body)
            if stopped["state"] != "STOPPED":
                raise RuntimeError("shutdown did not return STOPPED")
            if process.wait(timeout=READY_TIMEOUT_SECONDS) != 0:
                raise RuntimeError("packaged backend exited nonzero")
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            with stdout_lock:
                if stdout_capture:
                    raise RuntimeError("packaged backend emitted forbidden stdout")
            with stderr_lock:
                observed = b"".join(observed_bodies) + bytes(stderr_capture)
            if runtime_key_marker.encode("ascii") in observed:
                raise RuntimeError("runtime key material was exposed")
            if active_job_processes(job_handle) != 0:
                raise RuntimeError("packaged backend Job retained a process")
            return 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            close_handle(job_handle)


if __name__ == "__main__":
    raise SystemExit(main())
