from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from harness_shell_sidecar.agent.handlers import AgentTurnRequest


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


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
        headers=request_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "MODEL_API_CONFIG_NOT_FOUND"

    forbidden = autonomous_client.post(
        "/v1/agent/turns",
        headers=request_headers(),
        json={**payload, "api_key_b64": "forbidden"},
    )
    assert forbidden.status_code == 422
