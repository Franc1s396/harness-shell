from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    ProtocolViolation,
    Sensitivity,
)
from harness_shell_sidecar.runtime import Router, RuntimePhase


def frame(
    *,
    sequence: int,
    message_type: MessageType = MessageType.REQUEST,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    payload: dict,
    request_id: UUID | None = None,
) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=message_type,
        request_id=request_id or uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        sensitivity=sensitivity,
        payload=payload,
    )


def initialize_payload(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "method": "initialize",
        "app_version": "0.1.0",
        "runtime_db_path": r"C:\HarnessShell\runtime.sqlite3",
        "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
        "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
        "heartbeat_interval_ms": 5_000,
        "heartbeat_timeout_ms": 15_000,
    }
    payload.update(overrides)
    return payload


def initialize(router: Router, *, sequence: int = 1) -> FrameEnvelope:
    return router.handle(
        frame(
            sequence=sequence,
            sensitivity=Sensitivity.SECRET,
            payload=initialize_payload(),
        )
    )


def test_ready_event_starts_the_outbound_sequence() -> None:
    router = Router()

    ready = router.ready_event()

    assert ready.sequence == 1
    assert ready.message_type is MessageType.EVENT
    assert ready.payload["event"] == "sidecar.ready"
    assert ready.payload["capabilities"]["protocol_versions"] == [1]
    assert router.phase is RuntimePhase.HANDSHAKING


def test_initialize_requires_secret_frame() -> None:
    router = Router()
    router.ready_event()

    response = router.handle(frame(sequence=1, payload=initialize_payload()))

    assert response.message_type is MessageType.ERROR
    assert response.payload["error_code"] == "SENSITIVE_FRAME_REQUIRED"
    assert router.phase is RuntimePhase.HANDSHAKING


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("runtime_data_key_b64", base64.b64encode(b"short").decode("ascii")),
        ("audit_hmac_key_b64", "not-base64%%%"),
        ("runtime_db_path", "relative.sqlite3"),
        ("heartbeat_interval_ms", 4_999),
    ),
)
def test_invalid_initialize_payload_is_redacted(
    field: str, value: object
) -> None:
    router = Router()
    router.ready_event()
    payload = initialize_payload(**{field: value})

    response = router.handle(
        frame(
            sequence=1,
            sensitivity=Sensitivity.SECRET,
            payload=payload,
        )
    )

    assert response.payload == {
        "error_code": "INVALID_INITIALIZE_PAYLOAD",
        "message": "initialize payload is invalid",
    }
    assert str(value) not in str(response.payload)


def test_initialize_transitions_to_ready() -> None:
    router = Router()
    router.ready_event()

    response = initialize(router)

    assert response.message_type is MessageType.RESPONSE
    assert response.payload["result"] == "initialized"
    assert response.payload["state"] == "READY"
    assert router.phase is RuntimePhase.READY


def test_unknown_request_fails_without_dispatch() -> None:
    router = Router()
    router.ready_event()

    response = router.handle(
        frame(sequence=1, payload={"method": "unknown"})
    )

    assert response.payload["error_code"] == "UNKNOWN_METHOD"


def test_heartbeat_returns_pong_after_initialize() -> None:
    router = Router()
    router.ready_event()
    initialize(router)
    request = frame(
        sequence=2,
        message_type=MessageType.HEARTBEAT,
        payload={"kind": "ping"},
    )

    response = router.handle(request)

    assert response.request_id == request.request_id
    assert response.message_type is MessageType.HEARTBEAT
    assert response.payload == {"kind": "pong"}


def test_cancel_unknown_request_is_typed_error() -> None:
    router = Router()
    router.ready_event()
    target = uuid4()

    response = router.handle(
        frame(
            sequence=1,
            message_type=MessageType.CANCEL,
            payload={
                "target_request_id": str(target),
                "reason": "user_requested",
            },
        )
    )

    assert response.payload["error_code"] == "CANCEL_TARGET_NOT_FOUND"


def test_duplicate_inbound_sequence_is_terminal() -> None:
    router = Router()
    router.ready_event()
    initialize(router)

    with pytest.raises(ProtocolViolation, match="expected 2, got 1"):
        router.handle(
            frame(
                sequence=1,
                message_type=MessageType.HEARTBEAT,
                payload={"kind": "ping"},
            )
        )


def test_shutdown_stops_the_router() -> None:
    router = Router()
    router.ready_event()
    initialize(router)

    response = router.handle(
        frame(sequence=2, payload={"method": "shutdown"})
    )

    assert response.payload == {"result": "stopping"}
    assert router.phase is RuntimePhase.STOPPING

