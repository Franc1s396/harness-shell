"""Validate the autonomous desktop Sidecar pipe and HTTP/WebSocket lifecycle."""

from __future__ import annotations

import http.client
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from websockets.sync.client import connect

from harness_shell_sidecar.runtime.desktop_control import decode_ready_payload


READY_TIMEOUT_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 1
CAPTURE_LIMIT = 65_536


def request_json(
    port: int,
    method: str,
    path: str,
    *,
    expected_status: int,
) -> tuple[dict[str, object], bytes]:
    """Issue one correlated private HTTP request and validate its response."""

    request_id = uuid4()
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    try:
        connection.request(
            method,
            path,
            headers={
                "X-Request-ID": str(request_id),
                "Accept": "application/json, application/problem+json",
            },
        )
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


def wait_ready(port: int) -> bytes:
    """Poll autonomous readiness within the fixed startup deadline."""

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response, body = request_json(
                port,
                "GET",
                "/v1/health/ready",
                expected_status=200,
            )
            if response["ready"] is True and response["state"] == "READY":
                return body
            raise RuntimeError("readiness response contract failed")
        except (ConnectionError, OSError):
            time.sleep(0.025)
    raise RuntimeError("desktop backend readiness timed out")


def drain_pipe(pipe, capture: bytearray, lock: threading.Lock) -> None:
    """Drain a child pipe so bounded diagnostics cannot block shutdown."""

    while chunk := pipe.read(4_096):
        with lock:
            remaining = CAPTURE_LIMIT - len(capture)
            if remaining > 0:
                capture.extend(chunk[:remaining])


def read_exact(fd: int, length: int) -> bytes:
    """Read exactly one byte count or fail on premature pipe EOF."""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EOFError("ready pipe closed before the complete frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_ready_payload(fd: int) -> bytes:
    """Read one bounded length-prefixed ready payload."""

    (payload_length,) = struct.unpack(">I", read_exact(fd, 4))
    if not 1 <= payload_length <= 4_096:
        raise RuntimeError(f"invalid ready payload length {payload_length}")
    return read_exact(fd, payload_length)


def child_command(extraction_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Build a source or packaged desktop command without secret environment data."""

    environment = os.environ.copy()
    if len(sys.argv) > 1:
        executable = Path(sys.argv[1]).resolve(strict=True)
        command = [str(executable)]
    else:
        backend_root = Path(__file__).parents[1]
        source = str((backend_root / "src").resolve())
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source if not existing else os.pathsep.join((source, existing))
        )
        command = [sys.executable, "-m", "harness_shell_sidecar"]
    command.extend(
        [
            "desktop",
            "--port",
            "0",
            "--data-dir",
            str(extraction_dir),
        ]
    )
    return command, environment


def main() -> int:
    """Run ready-pipe, HTTP, WebSocket, and control-byte shutdown checks."""

    if sys.platform != "win32":
        raise RuntimeError("desktop Sidecar smoke requires Windows HANDLEs")

    import msvcrt

    with tempfile.TemporaryDirectory(prefix="harness-shell-smoke-") as temp_dir:
        data_dir = Path(temp_dir).resolve()
        command, environment = child_command(data_dir)
        control_read_fd, control_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        os.set_inheritable(control_read_fd, True)
        os.set_inheritable(control_write_fd, False)
        os.set_inheritable(ready_read_fd, False)
        os.set_inheritable(ready_write_fd, True)
        command.extend(
            [
                "--control-read-handle",
                str(msvcrt.get_osfhandle(control_read_fd)),
                "--ready-write-handle",
                str(msvcrt.get_osfhandle(ready_write_fd)),
            ]
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=False,
        )
        os.close(control_read_fd)
        os.close(ready_write_fd)
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
        ready_result: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

        def read_ready() -> None:
            """Move the blocking pipe read behind the smoke deadline."""

            try:
                ready_result.put(read_ready_payload(ready_read_fd))
            except BaseException as error:
                ready_result.put(error)

        ready_thread = threading.Thread(target=read_ready, daemon=True)
        ready_thread.start()
        control_open = True
        try:
            result = ready_result.get(timeout=READY_TIMEOUT_SECONDS)
            if isinstance(result, BaseException):
                raise result
            ready = decode_ready_payload(result)
            observed_bodies = [wait_ready(ready.port)]

            with connect(
                f"ws://127.0.0.1:{ready.port}/v1/runtime/events",
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

            os.write(control_write_fd, b"\x01")
            os.close(control_write_fd)
            control_open = False
            if process.wait(timeout=READY_TIMEOUT_SECONDS) != 0:
                raise RuntimeError("desktop backend exited nonzero")
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            with stdout_lock:
                if stdout_capture:
                    raise RuntimeError("desktop backend emitted forbidden stdout")
            with stderr_lock:
                observed = b"".join(observed_bodies) + bytes(stderr_capture)
            old_runtime_key = b"runtime_data" + b"_key_b64"
            old_audit_key = b"audit" + b"_hmac_key_b64"
            if old_runtime_key in observed or old_audit_key in observed:
                raise RuntimeError("removed Runtime key field was exposed")
            return 0
        finally:
            if control_open:
                os.close(control_write_fd)
            os.close(ready_read_fd)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            ready_thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
