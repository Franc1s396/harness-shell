from __future__ import annotations

import base64
import json
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.sync.client import connect

from harness_shell_sidecar.web.server import build_config

from .conftest import reserve_then_release_loopback_port, valid_initialize_json


def headers() -> dict[str, str]:
    """Create one valid HTTP request identity."""

    return {"X-Request-ID": str(uuid4())}


def test_server_config_is_fixed_to_private_loopback_contract() -> None:
    config = build_config(port=43_123)

    assert config.host == "127.0.0.1"
    assert config.port == 43_123
    assert config.proxy_headers is False
    assert config.forwarded_allow_ips == ""
    assert config.access_log is False
    assert config.server_header is False
    assert config.date_header is False
    assert config.ws_max_size == 65_536
    assert config.ws_max_queue == 64
    assert config.ws_ping_interval is None
    assert config.ws_ping_timeout is None


def test_serve_binds_requested_loopback_port_and_logs_it(
    sidecar_process,
) -> None:
    port = reserve_then_release_loopback_port()
    process = sidecar_process("serve", "--port", str(port))
    record = process.wait_for_stderr_event("http_server_listening")
    process.wait_until_http_ready(port)
    response = httpx.get(
        f"http://127.0.0.1:{port}/v1/health/live",
        headers=headers(),
    )

    assert record["host"] == "127.0.0.1"
    assert record["port"] == port
    assert response.status_code == 200
    assert response.headers.get("server") is None
    assert response.headers.get("date") is None
    assert process.stdout_lines == []


def test_serve_port_conflict_exits_nonzero_without_selecting_another_port(
    sidecar_process,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = int(occupied.getsockname()[1])
        process = sidecar_process("serve", "--port", str(port))
        return_code = process.process.wait(timeout=10)

    assert return_code != 0
    assert not any(
        '"event":"http_server_listening"' in line
        for line in process.stderr_lines
    )


def test_real_process_initializes_websocket_and_binary_sftp_without_secret_leak(
    sidecar_process,
    tmp_path: Path,
) -> None:
    port = reserve_then_release_loopback_port()
    process = sidecar_process("serve", "--port", str(port))
    process.wait_for_stderr_event("http_server_listening")
    process.wait_until_http_ready(port)
    base_url = f"http://127.0.0.1:{port}"
    payload = valid_initialize_json(tmp_path)
    marker = base64.b64encode(b"secret-process-marker-32-bytes!!"[:32]).decode(
        "ascii"
    )
    payload["runtime_data_key_b64"] = marker
    initialized = httpx.post(
        f"{base_url}/v1/runtime/initialize",
        headers=headers(),
        json=payload,
    )
    ready = httpx.get(f"{base_url}/v1/health/ready", headers=headers())

    with connect(f"ws://127.0.0.1:{port}/v1/runtime/events") as websocket:
        ping_id = uuid4()
        websocket.send(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "runtime.ping",
                    "message_id": str(ping_id),
                    "causation_id": None,
                    "timestamp": "2026-09-02T00:00:00+00:00",
                    "payload": {
                        "client_timestamp": "2026-09-02T00:00:00+00:00"
                    },
                }
            )
        )
        pong = json.loads(websocket.recv())
        with connect(
            f"ws://127.0.0.1:{port}/v1/runtime/events"
        ) as conflicting:
            with pytest.raises(ConnectionClosedError) as closed:
                conflicting.recv()
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 4409

    missing_operation = httpx.put(
        f"{base_url}/v1/sftp/uploads/{uuid4()}/chunks/0",
        headers={
            **headers(),
            "Content-Type": "application/octet-stream",
            "X-Chunk-Offset": "0",
        },
        content=b"a",
    )

    assert initialized.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert pong["type"] == "runtime.pong"
    assert pong["causation_id"] == str(ping_id)
    assert missing_operation.status_code == 404
    assert missing_operation.json()["error_code"] == "SFTP_OPERATION_NOT_FOUND"
    observed = "\n".join(
        [initialized.text, ready.text, missing_operation.text]
        + process.stdout_lines
        + process.stderr_lines
    )
    assert marker not in observed


def test_real_process_http_shutdown_exits_cleanly(
    sidecar_process,
    tmp_path: Path,
) -> None:
    port = reserve_then_release_loopback_port()
    process = sidecar_process("serve", "--port", str(port))
    process.wait_for_stderr_event("http_server_listening")
    process.wait_until_http_ready(port)
    base_url = f"http://127.0.0.1:{port}"
    initialized = httpx.post(
        f"{base_url}/v1/runtime/initialize",
        headers=headers(),
        json=valid_initialize_json(tmp_path),
    )
    shutdown = httpx.post(
        f"{base_url}/v1/runtime/shutdown",
        headers=headers(),
    )
    return_code = process.process.wait(timeout=5)

    assert initialized.status_code == 200
    assert shutdown.status_code == 202
    assert shutdown.json()["state"] == "STOPPED"
    assert return_code == 0
    messages = [
        json.loads(line).get("message")
        for line in process.stderr_lines
        if line.startswith("{")
    ]
    assert "Application shutdown complete." in messages
    assert any(
        isinstance(message, str) and message.startswith("Finished server process")
        for message in messages
    )
