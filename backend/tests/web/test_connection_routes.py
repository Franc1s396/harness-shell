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

from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def connection_input(**overrides: object) -> dict[str, object]:
    """Build one valid password-auth connection mutation input."""

    value: dict[str, object] = {
        "display_name": "Test host",
        "group_name": None,
        "host": "127.0.0.1",
        "port": 22,
        "username": "tester",
        "auth_kind": "password",
        "proxy_jump_id": None,
        "favorite": False,
    }
    value.update(overrides)
    return value


def encrypted_secret(client: TestClient, secret: str) -> dict[str, object]:
    """Encrypt one test secret for the current Runtime public key."""

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
    """Read one persisted credential through an independent SQLite connection."""

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
    """Count persisted credential records from the test thread safely."""

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            "SELECT COUNT(*) FROM runtime_records WHERE record_type = 'credential'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_connection_crud_routes_dispatch_to_the_shared_application_handlers(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    app = create_app(settings=settings)
    with TestClient(app) as client:
        create = client.post(
            "/v1/connections",
            headers=request_headers(),
            json=connection_input(
                credential_envelope=encrypted_secret(client, "first-password")
            ),
        )
        assert create.status_code == 201
        connection = create.json()["connection"]
        connection_id = connection["connection_id"]
        first_credential_id = connection["credential_id"]

        listed = client.get("/v1/connections", headers=request_headers())
        unchanged_secret = client.patch(
            f"/v1/connections/{connection_id}",
            headers=request_headers(),
            json=connection_input(display_name="Updated host"),
        )
        replacement = client.patch(
            f"/v1/connections/{connection_id}",
            headers=request_headers(),
            json=connection_input(
                display_name="Rekeyed host",
                credential_envelope=encrypted_secret(client, "second-password"),
            ),
        )
        second_credential_id = replacement.json()["connection"]["credential_id"]
        assert listed.status_code == 200
        assert len(listed.json()["connections"]) == 1
        assert unchanged_secret.status_code == 200
        assert unchanged_secret.json()["connection"]["credential_id"] == first_credential_id
        assert replacement.status_code == 200
        assert second_credential_id != first_credential_id
        assert credential_record(settings.database_path, first_credential_id) is None
        assert credential_record(settings.database_path, second_credential_id) == {
            "credential_id": second_credential_id,
            "kind": "ssh_password",
            "secret": "second-password",
        }

        deleted = client.delete(
            f"/v1/connections/{connection_id}", headers=request_headers()
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert credential_record(settings.database_path, second_credential_id) is None


def test_private_key_and_passphrase_are_owned_by_the_connection_transaction(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    app = create_app(settings=settings)
    with TestClient(app) as client:
        created = client.post(
            "/v1/connections",
            headers=request_headers(),
            json=connection_input(
                auth_kind="private_key",
                credential_envelope=encrypted_secret(client, "private-key-text"),
                passphrase_envelope=encrypted_secret(client, "key-passphrase"),
            ),
        )
        assert created.status_code == 201
        connection = created.json()["connection"]
        credential_id = connection["credential_id"]
        passphrase_id = connection["passphrase_credential_id"]

        assert credential_record(settings.database_path, credential_id) == {
            "credential_id": credential_id,
            "kind": "imported_private_key",
            "secret": "private-key-text",
        }
        assert credential_record(settings.database_path, passphrase_id) == {
            "credential_id": passphrase_id,
            "kind": "private_key_passphrase",
            "secret": "key-passphrase",
        }

        deleted = client.delete(
            f"/v1/connections/{connection['connection_id']}",
            headers=request_headers(),
        )

        assert deleted.status_code == 200
        assert credential_record(settings.database_path, credential_id) is None
        assert credential_record(settings.database_path, passphrase_id) is None


def test_connection_create_rolls_back_credential_when_profile_write_fails(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/connections",
            headers=request_headers(),
            json=connection_input(
                credential_envelope=encrypted_secret(client, "must-roll-back"),
                proxy_jump_id=str(uuid4()),
            ),
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "PROXY_JUMP_NOT_FOUND"
        assert credential_count(settings.database_path) == 0


def test_connection_unknown_field_uses_typed_problem(
    client,
    tmp_path: Path,
) -> None:
    invalid = client.post(
        "/v1/connections",
        headers=request_headers(),
        json=connection_input(unexpected=True),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "REQUEST_VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/host-key-inspections"),
        ("POST", "/v1/host-key-confirmations"),
        ("POST", "/v1/host-key-replacements"),
    ],
)
def test_host_key_routes_exist_as_typed_endpoints(
    client,
    method: str,
    path: str,
) -> None:
    response = client.request(
        method,
        path,
        headers=request_headers(),
        json={},
    )

    assert response.status_code not in (404, 405)
    assert response.json()["error_code"] in {
        "RUNTIME_NOT_READY",
        "REQUEST_VALIDATION_FAILED",
    }


def test_generic_rpc_routes_do_not_exist(client) -> None:
    response = client.post(
        "/v1/rpc",
        headers=request_headers(),
        json={"method": "connections.list", "params": {}},
    )

    assert response.status_code == 404
