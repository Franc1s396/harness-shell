from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def test_public_key_route_is_no_store(tmp_path: Path) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/v1/runtime/credential-encryption-key",
            headers=request_headers(),
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "request_id",
        "version",
        "scheme",
        "key_id",
        "public_key_spki_b64",
    }


def test_ssh_connect_accepts_only_connection_identity(tmp_path: Path) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(
            "/v1/ssh/sessions",
            headers=request_headers(),
            json={
                "connection_id": str(uuid4()),
                "password_b64": "forbidden",
            },
        )

    assert response.status_code == 422


def test_standalone_credential_mutation_routes_are_removed(
    tmp_path: Path,
) -> None:
    """Keep credential persistence behind owning business mutations only."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    paths = create_app(settings=settings).openapi()["paths"]

    assert "/v1/credentials" not in paths
    assert "/v1/credentials/private-key-imports" not in paths
    assert "/v1/credentials/{credential_id}" not in paths
