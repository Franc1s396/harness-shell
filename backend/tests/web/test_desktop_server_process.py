from __future__ import annotations

import os
import queue
import struct
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from harness_shell_sidecar.__main__ import build_parser
from harness_shell_sidecar.runtime.desktop_control import decode_ready_payload


def test_desktop_requires_zero_port_and_absolute_data_dir(
    tmp_path: Path,
) -> None:
    absolute_data_dir = str((tmp_path / "runtime-data").resolve())
    args = build_parser().parse_args(
        [
            "desktop",
            "--port",
            "0",
            "--data-dir",
            absolute_data_dir,
            "--control-read-handle",
            "101",
            "--ready-write-handle",
            "202",
        ]
    )

    assert args.command == "desktop"
    assert args.port == 0
    assert args.data_dir == Path(absolute_data_dir)

    invalid_arguments = (
        [
            "desktop",
            "--port",
            "8765",
            "--data-dir",
            absolute_data_dir,
            "--control-read-handle",
            "101",
            "--ready-write-handle",
            "202",
        ],
        [
            "desktop",
            "--port",
            "0",
            "--data-dir",
            "relative-data",
            "--control-read-handle",
            "101",
            "--ready-write-handle",
            "202",
        ],
    )
    for argv in invalid_arguments:
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows HANDLE protocol")
def test_desktop_publishes_dynamic_port_and_control_byte_stops_cleanly(
    tmp_path: Path,
) -> None:
    """Exercise the real inherited HANDLE protocol without stdout signaling."""

    control_read_fd, control_write_fd = os.pipe()
    ready_read_fd, ready_write_fd = os.pipe()
    os.set_inheritable(control_read_fd, True)
    os.set_inheritable(control_write_fd, False)
    os.set_inheritable(ready_read_fd, False)
    os.set_inheritable(ready_write_fd, True)

    import msvcrt

    control_read_handle = msvcrt.get_osfhandle(control_read_fd)
    ready_write_handle = msvcrt.get_osfhandle(ready_write_fd)
    environment = os.environ.copy()
    source = str((Path(__file__).parents[2] / "src").resolve())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not existing else os.pathsep.join((source, existing))
    )
    data_dir = (tmp_path / "desktop-runtime").resolve()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harness_shell_sidecar",
            "desktop",
            "--port",
            "0",
            "--data-dir",
            str(data_dir),
            "--control-read-handle",
            str(control_read_handle),
            "--ready-write-handle",
            str(ready_write_handle),
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        close_fds=False,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    os.close(control_read_fd)
    os.close(ready_write_fd)

    ready_result: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

    def read_ready_frame() -> None:
        """Read exactly one bounded frame without blocking the test deadline."""

        try:
            length_prefix = _read_exact(ready_read_fd, 4)
            (payload_length,) = struct.unpack(">I", length_prefix)
            if not 1 <= payload_length <= 4_096:
                raise AssertionError(f"invalid ready payload length {payload_length}")
            ready_result.put(_read_exact(ready_read_fd, payload_length))
        except BaseException as error:
            ready_result.put(error)

    reader = threading.Thread(target=read_ready_frame, daemon=True)
    reader.start()
    try:
        result = ready_result.get(timeout=10)
        if isinstance(result, BaseException):
            raise result
        ready = decode_ready_payload(result)
        response = httpx.get(
            f"http://127.0.0.1:{ready.port}/v1/health/ready",
            headers={"X-Request-ID": str(uuid4())},
            timeout=2,
        )
        os.write(control_write_fd, b"\x01")
        os.close(control_write_fd)
        control_write_fd = -1
        stdout, stderr = process.communicate(timeout=10)

        assert response.status_code == 200
        assert response.json()["ready"] is True
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        assert stdout == b""
    finally:
        if control_write_fd >= 0:
            os.close(control_write_fd)
        os.close(ready_read_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)


def _read_exact(fd: int, length: int) -> bytes:
    """Read one exact byte count or fail on premature pipe EOF."""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EOFError("ready pipe closed before the complete frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
