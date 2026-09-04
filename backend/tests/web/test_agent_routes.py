from __future__ import annotations

import base64
from collections.abc import Mapping
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from harness_shell_sidecar.agent.contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.agent.handlers import AgentTurnRequest
from harness_shell_sidecar.runtime.request_context import RequestContext


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def agent_stream_headers(request_id: str | None = None) -> dict[str, str]:
    """Create strict Agent SSE negotiation and correlation headers."""

    return {
        "Accept": "text/event-stream",
        "X-Request-ID": request_id or str(uuid4()),
    }


def parse_sse_events(wire: str) -> list[dict[str, object]]:
    """Decode Backend-owned fixed three-line frames for route assertions."""

    events: list[dict[str, object]] = []
    for frame in wire.split("\n\n"):
        if not frame:
            continue
        event_line, id_line, data_line = frame.split("\n")
        value = json.loads(data_line.removeprefix("data: "))
        assert event_line == f"event: {value['type']}"
        assert id_line == f"id: {value['sequence']}"
        events.append(value)
    return events


class SuccessfulTurnApplication:
    """Publish one deterministic successful lifecycle for route integration."""

    def __init__(self) -> None:
        """Freeze one Run identity reused across all emitted events."""

        now = datetime.now(UTC)
        self.run_snapshot = AgentRun(
            agent_run_id=uuid4(),
            conversation_id=uuid4(),
            ssh_session_id=uuid4(),
            api_config_id=uuid4(),
            status=AgentRunStatus.RUNNING,
            react_iteration=0,
            error_code=None,
            started_at=now,
            ended_at=None,
        )

    async def run(
        self,
        _context: RequestContext,
        _params: Mapping[str, object],
        sink,
    ) -> None:
        """Emit started, one exact visible delta, and durable completion."""

        await sink.started(self.run_snapshot)
        await sink.text_delta("hello")
        await sink.completed(
            self.run_snapshot.model_copy(
                update={
                    "status": AgentRunStatus.COMPLETED,
                    "ended_at": datetime.now(UTC),
                }
            )
        )


class FailedTurnApplication(SuccessfulTurnApplication):
    """Publish one deterministic durable post-start failure."""

    async def run(
        self,
        _context: RequestContext,
        _params: Mapping[str, object],
        sink,
    ) -> None:
        """Emit started then the matching failed terminal event."""

        await sink.started(self.run_snapshot)
        await sink.text_delta("partial")
        await sink.failed(
            self.run_snapshot.model_copy(
                update={
                    "status": AgentRunStatus.FAILED,
                    "error_code": "MODEL_RESPONSE_INVALID",
                    "ended_at": datetime.now(UTC),
                }
            )
        )


def config_input(**overrides: object) -> dict[str, object]:
    """Build one Provider configuration mutation input."""

    value: dict[str, object] = {
        "display_name": "Test Provider",
        "api_type": "RESPONSES",
        "base_url": "https://example.invalid/v1/",
        "model": "test-model",
        "enabled": True,
    }
    value.update(overrides)
    return value


def encrypted_secret(client: TestClient, secret: str) -> dict[str, object]:
    """Encrypt one API key for the current Runtime public key."""

    public_key = client.get(
        "/v1/runtime/credential-encryption-key",
        headers=request_headers(),
    ).json()
    aes_key = bytes(range(32))
    iv = bytes(range(12))
    aad = f"harness-shell-credential-v1\0{public_key['key_id']}".encode()
    ciphertext = AESGCM(aes_key).encrypt(iv, secret.encode(), aad)
    rsa_public_key = serialization.load_der_public_key(
        base64.b64decode(public_key["public_key_spki_b64"], validate=True)
    )
    wrapped_key = rsa_public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "version": 1,
        "key_id": public_key["key_id"],
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "iv_b64": base64.b64encode(iv).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def credential_record(database_path: Path, credential_id: str) -> dict[str, str] | None:
    """Read one credential through a test-thread-owned SQLite connection."""

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            """
            SELECT payload
            FROM runtime_records
            WHERE record_type = 'credential' AND record_id = ?
            """,
            (credential_id,),
        ).fetchone()
    return None if row is None else json.loads(bytes(row[0]).decode("utf-8"))


def credential_count(database_path: Path) -> int:
    """Count credential rows without borrowing the ASGI thread connection."""

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            "SELECT COUNT(*) FROM runtime_records WHERE record_type = 'credential'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_agent_api_config_crud_routes_are_typed(autonomous_client) -> None:
    created = autonomous_client.post(
        "/v1/agent/api-configs",
        headers=request_headers(),
        json=config_input(
            api_key_envelope=encrypted_secret(autonomous_client, "first-api-key")
        ),
    )
    assert created.status_code == 201
    created_config = created.json()["config"]
    config_id = created_config["api_config_id"]
    first_credential_id = created_config["api_key_credential_id"]

    listed = autonomous_client.get("/v1/agent/api-configs", headers=request_headers())
    unchanged_secret = autonomous_client.patch(
        f"/v1/agent/api-configs/{config_id}",
        headers=request_headers(),
        json=config_input(display_name="Updated Provider"),
    )
    replacement = autonomous_client.patch(
        f"/v1/agent/api-configs/{config_id}",
        headers=request_headers(),
        json=config_input(
            display_name="Rekeyed Provider",
            api_key_envelope=encrypted_secret(autonomous_client, "second-api-key"),
        ),
    )
    second_credential_id = replacement.json()["config"]["api_key_credential_id"]
    database_path = (
        autonomous_client.app.state.runtime_owner.require_resources().database.path
    )

    assert listed.status_code == 200
    assert unchanged_secret.status_code == 200
    assert unchanged_secret.json()["config"]["api_key_credential_id"] == first_credential_id
    assert replacement.status_code == 200
    assert second_credential_id != first_credential_id
    assert credential_record(database_path, first_credential_id) is None
    assert credential_record(database_path, second_credential_id) == {
        "credential_id": second_credential_id,
        "kind": "api_key",
        "secret": "second-api-key",
    }

    deleted = autonomous_client.delete(
        f"/v1/agent/api-configs/{config_id}", headers=request_headers()
    )

    assert deleted.json()["deleted"] is True
    assert credential_record(database_path, second_credential_id) is None


def test_api_config_create_rolls_back_credential_when_metadata_write_fails(
    autonomous_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = autonomous_client.app.state.runtime_owner.require_resources()

    def fail_create(_value: object) -> None:
        """Simulate a metadata failure after the handler creates the credential."""

        raise RuntimeError("simulated metadata failure")

    monkeypatch.setattr(resources.agent_api_configs, "create", fail_create)

    with pytest.raises(RuntimeError, match="simulated metadata failure"):
        autonomous_client.post(
            "/v1/agent/api-configs",
            headers=request_headers(),
            json=config_input(
                api_key_envelope=encrypted_secret(
                    autonomous_client, "must-roll-back"
                )
            ),
        )

    assert credential_count(resources.database.path) == 0


def test_agent_turn_accepts_only_identities_and_missing_config_is_typed(
    autonomous_client,
) -> None:
    payload = {
        "conversation_id": None,
        "ssh_session_id": str(uuid4()),
        "api_config_id": str(uuid4()),
        "user_message": "do not run",
    }
    assert AgentTurnRequest.model_validate_json(json.dumps(payload))

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "MODEL_API_CONFIG_NOT_FOUND"

    forbidden = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json={**payload, "api_key_b64": "forbidden"},
    )
    assert forbidden.status_code == 422


def test_agent_turn_requires_sse_accept(autonomous_client) -> None:
    """Reject a turn before application work when SSE was not negotiated."""

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=request_headers(),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )

    assert response.status_code == 406
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["error_code"] == "AGENT_STREAM_ACCEPT_REQUIRED"


def test_agent_turn_success_is_started_first_sse(autonomous_client) -> None:
    """Expose only the fixed correlated event sequence on HTTP success."""

    request_id = str(uuid4())
    resources = autonomous_client.app.state.runtime_owner.require_resources()
    application = SuccessfulTurnApplication()
    resources.agent_turn_application = application

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(request_id),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )
    events = parse_sse_events(response.text)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == request_id
    assert [event["type"] for event in events] == [
        "agent.turn.started",
        "agent.turn.text_delta",
        "agent.turn.completed",
    ]
    assert [event["sequence"] for event in events] == [0, 1, 2]
    assert {event["request_id"] for event in events} == {request_id}
    assert {event["conversation_id"] for event in events} == {
        str(application.run_snapshot.conversation_id)
    }
    assert {event["agent_run_id"] for event in events} == {
        str(application.run_snapshot.agent_run_id)
    }


def test_agent_turn_post_start_failure_remains_sse(autonomous_client) -> None:
    """Keep HTTP 200 and emit one safe terminal failure after started."""

    resources = autonomous_client.app.state.runtime_owner.require_resources()
    application = FailedTurnApplication()
    resources.agent_turn_application = application
    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )
    events = parse_sse_events(response.text)

    assert response.status_code == 200
    assert [event["type"] for event in events] == [
        "agent.turn.started",
        "agent.turn.text_delta",
        "agent.turn.failed",
    ]
    assert events[-1]["status"] == "FAILED"
    assert events[-1]["error_code"] == "MODEL_RESPONSE_INVALID"
    assert events[-1]["message"] == "Model response was invalid"
    assert "partial" not in str(events[-1])
