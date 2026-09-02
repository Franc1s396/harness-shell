from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from .conftest import valid_initialize_json


def request_headers() -> dict[str, str]:
    """Create one valid HTTP correlation header."""

    return {"X-Request-ID": str(uuid4())}


def initialize(client, tmp_path: Path):
    """Initialize and return the unique HTTP runtime owner."""

    response = client.post(
        "/v1/runtime/initialize",
        headers=request_headers(),
        json=valid_initialize_json(tmp_path),
    )
    assert response.status_code == 200
    return client.app.state.runtime_owner


def message(
    message_type: str,
    payload: dict[str, object],
    *,
    message_id: UUID | None = None,
    causation_id: UUID | None = None,
    timestamp: str | None = None,
) -> dict[str, object]:
    """Build one canonical Runtime WebSocket client envelope."""

    return {
        "schema_version": 1,
        "type": message_type,
        "message_id": str(message_id or uuid4()),
        "causation_id": None if causation_id is None else str(causation_id),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def test_runtime_ping_returns_correlated_pong(client, tmp_path: Path) -> None:
    initialize(client, tmp_path)
    ping_id = uuid4()
    with client.websocket_connect("/v1/runtime/events") as socket:
        socket.send_json(
            message(
                "runtime.ping",
                {"client_timestamp": datetime.now(timezone.utc).isoformat()},
                message_id=ping_id,
            )
        )
        pong = socket.receive_json()

    assert pong["schema_version"] == 1
    assert pong["type"] == "runtime.pong"
    assert pong["causation_id"] == str(ping_id)
    server_timestamp = datetime.fromisoformat(
        pong["payload"]["server_timestamp"].replace("Z", "+00:00")
    )
    assert server_timestamp.utcoffset() is not None


def test_second_runtime_websocket_cannot_replace_active_owner(
    client,
    tmp_path: Path,
) -> None:
    owner = initialize(client, tmp_path)
    gateway = owner.websocket_gateway
    assert client.portal.call(gateway.claim) is True
    try:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            with client.websocket_connect("/v1/runtime/events") as second:
                second.receive_json()
    finally:
        client.portal.call(gateway.release)

    assert disconnected.value.code == 4409


def test_pty_domain_failure_returns_correlated_result_and_keeps_connection(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    input_id = uuid4()
    pty_session_id = uuid4()
    with client.websocket_connect("/v1/runtime/events") as socket:
        socket.send_json(
            message(
                "pty.input",
                {
                    "pty_session_id": str(pty_session_id),
                    "data_b64": base64.b64encode(b"a").decode("ascii"),
                },
                message_id=input_id,
            )
        )
        result = socket.receive_json()
        socket.send_json(
            message(
                "runtime.ping",
                {"client_timestamp": datetime.now(timezone.utc).isoformat()},
            )
        )
        pong = socket.receive_json()

    assert result["type"] == "pty.input_result"
    assert result["causation_id"] == str(input_id)
    assert result["payload"] == {
        "pty_session_id": str(pty_session_id),
        "accepted_bytes": 0,
        "error_code": "PTY_SESSION_NOT_FOUND",
    }
    assert pong["type"] == "runtime.pong"


@pytest.mark.parametrize(
    "invalid",
    [
        message(
            "runtime.ping",
            {"client_timestamp": datetime.now(timezone.utc).isoformat()},
        )
        | {"unexpected": True},
        message(
            "runtime.ping",
            {"client_timestamp": datetime.now(timezone.utc).isoformat()},
            timestamp="2026-09-02T00:00:00",
        ),
        message(
            "runtime.ping",
            {"client_timestamp": datetime.now(timezone.utc).isoformat()},
            causation_id=uuid4(),
        ),
        message("unknown", {}),
        message(
            "pty.input",
            {"pty_session_id": str(uuid4()), "data_b64": "not-base64"},
        ),
        message(
            "pty.input",
            {
                "pty_session_id": str(uuid4()),
                "data_b64": base64.b64encode(b"x" * 32_769).decode("ascii"),
            },
        ),
    ],
    ids=[
        "unknown-field",
        "naive-timestamp",
        "client-causation",
        "unknown-type",
        "invalid-base64",
        "oversize-decoded-pty-input",
    ],
)
def test_invalid_runtime_message_closes_with_contract_code(
    client,
    tmp_path: Path,
    invalid: dict[str, object],
) -> None:
    initialize(client, tmp_path)
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect("/v1/runtime/events") as socket:
            socket.send_json(invalid)
            socket.receive_json()

    assert disconnected.value.code == 4400


def test_message_larger_than_65536_encoded_bytes_closes_with_1009(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect("/v1/runtime/events") as socket:
            socket.send_text("x" * 65_537)
            socket.receive_json()

    assert disconnected.value.code == 1009


def test_explicit_heartbeat_timeout_closes_with_4408(
    client,
    tmp_path: Path,
) -> None:
    from harness_shell_sidecar.web.websocket import RuntimeWebSocketGateway

    owner = initialize(client, tmp_path)
    owner.websocket_gateway = RuntimeWebSocketGateway(
        heartbeat_timeout_seconds=0.01
    )
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect("/v1/runtime/events") as socket:
            socket.receive_json()

    assert disconnected.value.code == 4408


def test_runtime_owner_converts_domain_event_before_websocket_delivery(
    client,
    tmp_path: Path,
) -> None:
    owner = initialize(client, tmp_path)
    pty_session_id = uuid4()
    with client.websocket_connect("/v1/runtime/events") as socket:
        client.portal.call(
            owner.event_sink,
            {
                "event": "ssh.pty.output",
                "pty_session_id": str(pty_session_id),
                "stream_sequence": 9,
                "data_b64": "YQ==",
            },
        )
        event = socket.receive_json()

    assert event["type"] == "pty.output"
    assert event["causation_id"] is None
    assert event["payload"] == {
        "pty_session_id": str(pty_session_id),
        "data_b64": "YQ==",
        "stream_sequence": 9,
    }


def test_gateway_outbound_queue_applies_backpressure_at_exact_capacity() -> None:
    from harness_shell_sidecar.web.websocket import RuntimeWebSocketGateway

    async def scenario() -> None:
        gateway = RuntimeWebSocketGateway()
        assert gateway._inbound.maxsize == 64
        assert gateway._outbound.maxsize == 64
        for index in range(64):
            await gateway.publish_domain_event(
                {
                    "event": "ssh.pty.output",
                    "pty_session_id": str(uuid4()),
                    "stream_sequence": index,
                    "data_b64": "YQ==",
                }
            )
        blocked = asyncio.create_task(
            gateway.publish_domain_event(
                {
                    "event": "ssh.pty.output",
                    "pty_session_id": str(uuid4()),
                    "stream_sequence": 64,
                    "data_b64": "YQ==",
                }
            )
        )
        await asyncio.sleep(0)
        assert blocked.done() is False
        await gateway.next_outbound()
        await blocked

    asyncio.run(scenario())


def test_domain_event_converter_preserves_current_safe_payload_shapes() -> None:
    from harness_shell_sidecar.web.websocket import convert_domain_event

    pty_session_id = uuid4()
    output = convert_domain_event(
        {
            "event": "ssh.pty.output",
            "pty_session_id": str(pty_session_id),
            "stream_sequence": 7,
            "data_b64": "YQ==",
        }
    )
    closed = convert_domain_event(
        {
            "event": "ssh.pty.closed",
            "pty_session_id": str(pty_session_id),
            "exit_status": 0,
            "exit_signal": None,
        }
    )
    connection_id = uuid4()
    correlation_id = uuid4()
    ssh_state = convert_domain_event(
        {
            "event": "ssh.connection.status",
            "status": {
                "connection_id": str(connection_id),
                "state": "CONNECTING",
                "session_id": None,
                "error_code": None,
                "recoverable": False,
                "correlation_id": str(correlation_id),
                "host_key_candidate": None,
                "trusted_fingerprint_sha256": None,
            },
        }
    )
    operation_id = uuid4()
    sftp_progress = convert_domain_event(
        {
            "event": "manual_sftp.operation.progress",
            "operation_id": str(operation_id),
            "kind": "recursive_delete",
            "phase": "deleting",
            "display_name": "cache",
            "remote_path": "/tmp/cache",
            "host_label": "test-host",
            "items_completed": 3,
            "items_total": 5,
            "cancellable": False,
        }
    )

    assert output.type == "pty.output"
    assert output.payload.stream_sequence == 7
    assert closed.type == "pty.closed"
    assert closed.payload.exit_status == 0
    assert ssh_state.type == "ssh.connection_state"
    assert ssh_state.payload.connection_id == connection_id
    assert ssh_state.payload.state == "CONNECTING"
    assert sftp_progress.type == "sftp.operation_progress"
    assert sftp_progress.payload.operation_id == operation_id
    assert sftp_progress.payload.items_completed == 3
