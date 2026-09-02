from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from .conftest import valid_initialize_json


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def initialize(client, tmp_path: Path) -> None:
    """Initialize the disposable HTTP runtime used by a domain-route test."""

    response = client.post(
        "/v1/runtime/initialize",
        headers=request_headers(),
        json=valid_initialize_json(tmp_path),
    )
    assert response.status_code == 200


def connection_input(**overrides: object) -> dict[str, object]:
    """Build one valid password-auth connection profile input."""

    value: dict[str, object] = {
        "display_name": "Test host",
        "group_name": None,
        "host": "127.0.0.1",
        "port": 22,
        "username": "tester",
        "auth_kind": "password",
        "credential_id": str(uuid4()),
        "passphrase_credential_id": None,
        "proxy_jump_id": None,
        "favorite": False,
    }
    value.update(overrides)
    return value


def test_connection_crud_routes_dispatch_to_the_shared_application_handlers(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    create = client.post(
        "/v1/connections",
        headers=request_headers(),
        json=connection_input(),
    )
    assert create.status_code == 201
    connection = create.json()["connection"]
    connection_id = connection["connection_id"]

    listed = client.get("/v1/connections", headers=request_headers())
    fetched = client.get(
        f"/v1/connections/{connection_id}", headers=request_headers()
    )
    updated = client.patch(
        f"/v1/connections/{connection_id}",
        headers=request_headers(),
        json=connection_input(display_name="Updated host"),
    )
    deleted = client.delete(
        f"/v1/connections/{connection_id}", headers=request_headers()
    )

    assert listed.status_code == 200
    assert len(listed.json()["connections"]) == 1
    assert fetched.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["connection"]["display_name"] == "Updated host"
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_connection_unknown_field_and_missing_identity_use_typed_problems(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    invalid = client.post(
        "/v1/connections",
        headers=request_headers(),
        json=connection_input(unexpected=True),
    )
    missing = client.get(
        f"/v1/connections/{uuid4()}", headers=request_headers()
    )

    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "REQUEST_VALIDATION_FAILED"
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "CONNECTION_NOT_FOUND"


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
