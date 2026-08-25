from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType, Sensitivity, encode_frame


DATA_MARKER = b"M1-DATA-SECRET-6f047bd2"
AUDIT_MARKER = b"M1-AUDIT-SECRET-a86ce181"


def _frame(stream: BinaryIO, timeout: float = 3.0) -> FrameEnvelope:
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
        raise AssertionError("timed out waiting for Sidecar frame") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _request(sequence: int, payload: dict, *, version: int = 1) -> bytes:
    frame = {
        "protocol_version": version,
        "message_type": "request",
        "request_id": str(uuid4()),
        "task_id": None,
        "workflow_run_id": None,
        "sequence": sequence,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sensitivity": "secret" if payload.get("method") == "initialize" else "normal",
        "payload": payload,
    }
    body = __import__("json").dumps(frame, separators=(",", ":")).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def _initialize_payload(path: Path) -> dict:
    data_key = DATA_MARKER.ljust(32, b"d")
    audit_key = AUDIT_MARKER.ljust(32, b"a")
    return {
        "method": "initialize",
        "app_version": "0.1.0",
        "runtime_db_path": str(path.resolve()),
        "runtime_data_key_b64": base64.b64encode(data_key).decode(),
        "audit_hmac_key_b64": base64.b64encode(audit_key).decode(),
        "heartbeat_interval_ms": 5_000,
        "heartbeat_timeout_ms": 15_000,
    }


def _start() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "harness_shell_sidecar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _initialize(process: subprocess.Popen[bytes], path: Path) -> FrameEnvelope:
    assert process.stdin and process.stdout
    assert _frame(process.stdout).payload["event"] == "sidecar.ready"
    process.stdin.write(_request(1, _initialize_payload(path)))
    process.stdin.flush()
    return _frame(process.stdout)


def _shutdown(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdin and process.stdout and process.stderr
    started = time.monotonic()
    process.stdin.write(_request(2, {"method": "shutdown"}))
    process.stdin.flush()
    assert _frame(process.stdout).payload == {"result": "stopping"}
    assert process.wait(timeout=3) == 0
    assert time.monotonic() - started < 3
    return process.stderr.read()


def test_valid_startup_shutdown_and_secret_evidence(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite3"
    process = _start()
    try:
        initialized = _initialize(process, runtime_db)
        assert initialized.payload["state"] == "READY"
        actual_secrets = (
            DATA_MARKER.ljust(32, b"d"),
            AUDIT_MARKER.ljust(32, b"a"),
        )
        live_persisted = b"".join(
            candidate.read_bytes()
            for candidate in (runtime_db, Path(f"{runtime_db}-wal"), Path(f"{runtime_db}-shm"))
            if candidate.exists()
        )
        assert Path(f"{runtime_db}-wal").exists()
        for secret in actual_secrets:
            assert secret not in live_persisted
            assert base64.b64encode(secret) not in live_persisted
        stderr = _shutdown(process)

        connection = sqlite3.connect(runtime_db)
        try:
            audits = connection.execute(
                "SELECT event_type, correlation_id FROM audit_entries ORDER BY sequence"
            ).fetchall()
            traces = {
                row[0] for row in connection.execute("SELECT name FROM trace_spans").fetchall()
            }
        finally:
            connection.close()

        assert [row[0] for row in audits] == [
            "runtime.started",
            "runtime.ready",
            "runtime.stopped",
        ]
        assert len({row[1] for row in audits}) == 1
        assert {"runtime.starting", "audit.verify", "storage.self_check", "runtime.ready"} <= traces

        persisted = stderr + b"".join(
            candidate.read_bytes()
            for candidate in (runtime_db, Path(f"{runtime_db}-wal"), Path(f"{runtime_db}-shm"))
            if candidate.exists()
        )
        for secret in actual_secrets:
            assert secret not in persisted
            assert base64.b64encode(secret) not in persisted
    finally:
        if process.poll() is None:
            process.kill()


def test_wrong_protocol_version_stops_before_ready(tmp_path: Path) -> None:
    process = _start()
    try:
        assert process.stdin and process.stdout
        assert _frame(process.stdout).payload["event"] == "sidecar.ready"
        process.stdin.write(_request(1, _initialize_payload(tmp_path / "runtime.sqlite3"), version=2))
        process.stdin.flush()
        rejected = _frame(process.stdout)
        assert rejected.message_type is MessageType.ERROR
        assert rejected.payload["error_code"] == "PROTOCOL_VIOLATION"
        assert process.wait(timeout=3) != 0
    finally:
        if process.poll() is None:
            process.kill()


def test_tampered_audit_stops_before_ready(tmp_path: Path) -> None:
    runtime_db = tmp_path / "runtime.sqlite3"
    first = _start()
    try:
        assert _initialize(first, runtime_db).payload["state"] == "READY"
        _shutdown(first)
    finally:
        if first.poll() is None:
            first.kill()

    with sqlite3.connect(runtime_db) as connection:
        connection.execute("UPDATE audit_entries SET body_json = ? WHERE sequence = 1", ('{"state":"forged"}',))

    second = _start()
    try:
        rejected = _initialize(second, runtime_db)
        assert rejected.message_type is MessageType.ERROR
        assert rejected.payload["error_code"] == "AUDIT_CHAIN_INVALID"
        assert second.wait(timeout=3) != 0
    finally:
        if second.poll() is None:
            second.kill()
