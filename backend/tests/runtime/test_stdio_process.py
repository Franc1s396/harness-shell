from __future__ import annotations

import base64
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    Sensitivity,
    encode_frame,
)


def read_one_frame(stream: BinaryIO, timeout: float = 3.0) -> FrameEnvelope:
    def read() -> FrameEnvelope:
        header = stream.readline()
        assert header.startswith(b"Content-Length: ")
        length = int(header.removeprefix(b"Content-Length: ").strip())
        assert stream.readline() == b"\r\n"
        return FrameEnvelope.model_validate_json(stream.read(length))

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(read)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        raise AssertionError("timed out waiting for protocol frame") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def request(
    sequence: int,
    payload: dict,
    *,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
) -> FrameEnvelope:
    return FrameEnvelope.model_validate(
        {
            "protocol_version": 1,
            "message_type": MessageType.REQUEST,
            "request_id": uuid4(),
            "task_id": None,
            "workflow_run_id": None,
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc),
            "sensitivity": sensitivity,
            "payload": payload,
        }
    )


def start_sidecar_module() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "harness_shell_sidecar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def initialize_payload(runtime_db: Path) -> dict:
    return {
        "method": "initialize",
        "app_version": "0.1.0",
        "runtime_db_path": str(runtime_db.resolve()),
        "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
        "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
        "heartbeat_interval_ms": 5_000,
        "heartbeat_timeout_ms": 15_000,
    }


def test_module_stdout_contains_only_protocol_frames(tmp_path: Path) -> None:
    process = start_sidecar_module()
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        ready = read_one_frame(process.stdout)
        assert ready.payload["event"] == "sidecar.ready"

        initialize_request = request(
            1,
            initialize_payload(tmp_path / "runtime.sqlite3"),
            sensitivity=Sensitivity.SECRET,
        )
        process.stdin.write(encode_frame(initialize_request))
        process.stdin.flush()
        initialized = read_one_frame(process.stdout)
        assert initialized.request_id == initialize_request.request_id
        assert initialized.payload["state"] == "READY"

        shutdown_request = request(2, {"method": "shutdown"})
        process.stdin.write(encode_frame(shutdown_request))
        process.stdin.flush()
        stopping = read_one_frame(process.stdout)
        assert stopping.request_id == shutdown_request.request_id
        assert stopping.payload == {"result": "stopping"}

        assert process.wait(timeout=3) == 0
        assert process.stdout.read() == b""
        stderr = process.stderr.read()
        assert b"ZGRkZGRk" not in stderr
        assert b"YWFhYWFh" not in stderr
    finally:
        if process.poll() is None:
            process.kill()


def test_eof_before_shutdown_is_not_reported_as_success() -> None:
    process = start_sidecar_module()
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        read_one_frame(process.stdout)
        process.stdin.close()

        assert process.wait(timeout=3) != 0
    finally:
        if process.poll() is None:
            process.kill()
