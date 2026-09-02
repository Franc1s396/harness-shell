from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

from harness_shell_sidecar.agent.handlers import AgentTurnRequest

from .conftest import valid_initialize_json


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def initialize(client, tmp_path: Path) -> None:
    """Initialize a disposable domain runtime."""

    assert client.post(
        "/v1/runtime/initialize",
        headers=request_headers(),
        json=valid_initialize_json(tmp_path),
    ).status_code == 200


def config_input(**overrides: object) -> dict[str, object]:
    """Build one non-secret Provider configuration input."""

    value: dict[str, object] = {
        "display_name": "Test Provider",
        "api_type": "RESPONSES",
        "base_url": "https://example.invalid/v1/",
        "model": "test-model",
        "api_key_secret_ref": str(uuid4()),
        "enabled": True,
    }
    value.update(overrides)
    return value


def test_agent_api_config_crud_routes_are_typed(client, tmp_path: Path) -> None:
    initialize(client, tmp_path)
    created = client.post(
        "/v1/agent/api-configs",
        headers=request_headers(),
        json=config_input(),
    )
    assert created.status_code == 201
    config_id = created.json()["config"]["api_config_id"]

    listed = client.get("/v1/agent/api-configs", headers=request_headers())
    fetched = client.get(
        f"/v1/agent/api-configs/{config_id}", headers=request_headers()
    )
    updated = client.patch(
        f"/v1/agent/api-configs/{config_id}",
        headers=request_headers(),
        json=config_input(display_name="Updated Provider"),
    )
    deleted = client.delete(
        f"/v1/agent/api-configs/{config_id}", headers=request_headers()
    )

    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert updated.json()["config"]["display_name"] == "Updated Provider"
    assert deleted.json()["deleted"] is True


def test_agent_turn_secret_is_redacted_and_missing_config_is_typed(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    marker = base64.b64encode(b"provider-secret-marker").decode("ascii")
    payload = {
        "conversation_id": None,
        "ssh_session_id": str(uuid4()),
        "api_config_id": str(uuid4()),
        "api_key_credential_id": str(uuid4()),
        "api_key_b64": marker,
        "user_message": "do not run",
    }
    assert marker not in repr(AgentTurnRequest.model_validate_json(json.dumps(payload)))

    response = client.post(
        "/v1/agent/turns",
        headers=request_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "MODEL_API_CONFIG_NOT_FOUND"
    assert marker not in response.text
