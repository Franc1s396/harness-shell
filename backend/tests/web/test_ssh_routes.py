from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

from harness_shell_sidecar.ssh.models import SshConnectRequest

from .conftest import valid_initialize_json


class FakeConnection:
    """Provide the exact close surface owned by a registered test SSH session."""

    def __init__(self) -> None:
        """Create an open connection marker."""

        self.closed = False

    def is_closed(self) -> bool:
        """Report the current deterministic connection state."""

        return self.closed

    def close(self) -> None:
        """Record registry-owned closure."""

        self.closed = True

    async def wait_closed(self) -> None:
        """Model immediate deterministic transport convergence."""


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


def test_ssh_session_list_is_a_safe_empty_snapshot(client, tmp_path: Path) -> None:
    initialize(client, tmp_path)

    response = client.get("/v1/ssh/sessions", headers=request_headers())

    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_ssh_session_list_requires_ready_runtime(client) -> None:
    response = client.get("/v1/ssh/sessions", headers=request_headers())

    assert response.status_code == 503
    assert response.json()["error_code"] == "RUNTIME_NOT_READY"


def test_ssh_session_list_exposes_only_safe_snapshot_fields(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    resources = client.app.state.runtime_owner.require_resources()
    connection_id = uuid4()
    session = resources.ssh_runtime.sessions.register(
        connection_id,
        FakeConnection(),
        connection_profile_version=3,
        host_label="Safe host",
        target_host_key_fingerprint="SHA256:safe",
    )

    response = client.get("/v1/ssh/sessions", headers=request_headers())

    assert response.status_code == 200
    snapshots = response.json()["sessions"]
    assert snapshots == [
        {
            "ssh_session_id": str(session.ssh_session_id),
            "connection_id": str(connection_id),
            "connection_profile_version": 3,
            "host_label": "Safe host",
            "target_host_key_fingerprint": "SHA256:safe",
            "jump_connection_id": None,
            "jump_profile_version": None,
            "jump_host_key_fingerprint": None,
        }
    ]
    assert "connection" not in snapshots[0]
    assert "jump_connection" not in snapshots[0]


def test_ssh_connect_secret_is_redacted_and_missing_profile_is_typed(
    client,
    tmp_path: Path,
) -> None:
    initialize(client, tmp_path)
    marker = base64.b64encode(b"ssh-secret-marker").decode("ascii")
    payload = {
        "connection_id": str(uuid4()),
        "profile_version": 1,
        "password_b64": marker,
        "private_key_b64": None,
        "passphrase_b64": None,
        "jump": None,
    }
    assert marker not in repr(SshConnectRequest.model_validate_json(__import__("json").dumps(payload)))

    response = client.post(
        "/v1/ssh/sessions",
        headers=request_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "CONNECTION_NOT_FOUND"
    assert marker not in response.text


def test_ssh_disconnect_missing_session_is_not_found(client, tmp_path: Path) -> None:
    initialize(client, tmp_path)

    response = client.delete(
        f"/v1/ssh/sessions/{uuid4()}", headers=request_headers()
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "SSH_SESSION_NOT_FOUND"
