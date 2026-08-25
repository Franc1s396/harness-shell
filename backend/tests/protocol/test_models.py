import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    Sensitivity,
)


REQUEST_ID = UUID("018f3f83-7a53-7b5d-9c4e-1b2f68e27911")


def heartbeat(sequence: int = 1) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.HEARTBEAT,
        request_id=REQUEST_ID,
        task_id=None,
        workflow_run_id=None,
        sequence=sequence,
        timestamp=datetime(2026, 8, 25, tzinfo=timezone.utc),
        sensitivity=Sensitivity.NORMAL,
        payload={"kind": "ping"},
    )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (("protocol_version", 2), ("sequence", 0)),
)
def test_envelope_rejects_invalid_contract_fields(
    field: str, invalid_value: int
) -> None:
    payload = heartbeat().model_dump(mode="json")
    payload[field] = invalid_value

    with pytest.raises(ValidationError):
        FrameEnvelope.model_validate_json(json.dumps(payload))


def test_envelope_rejects_unknown_fields() -> None:
    payload = heartbeat().model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FrameEnvelope.model_validate_json(json.dumps(payload))


def test_envelope_requires_an_object_payload() -> None:
    payload = heartbeat().model_dump(mode="json")
    payload["payload"] = ["not", "an", "object"]

    with pytest.raises(ValidationError):
        FrameEnvelope.model_validate_json(json.dumps(payload))


def test_envelope_serializes_uuid_and_utc_timestamp() -> None:
    serialized = json.loads(heartbeat().model_dump_json())

    assert serialized["request_id"] == str(REQUEST_ID)
    assert serialized["timestamp"] == "2026-08-25T00:00:00Z"
    assert serialized["task_id"] is None
    assert serialized["workflow_run_id"] is None


def test_golden_fixture_matches_the_python_contract() -> None:
    fixture = (
        Path(__file__).parents[3]
        / "docs"
        / "protocol"
        / "fixtures"
        / "valid-heartbeat-v1.json"
    )

    assert FrameEnvelope.model_validate_json(fixture.read_bytes()) == heartbeat()
