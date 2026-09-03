from __future__ import annotations

from importlib import import_module
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from harness_shell_sidecar.web import create_app


def runtime_settings(data_dir: Path):
    """Create target settings while keeping a missing implementation RED."""

    try:
        settings_type = import_module(
            "harness_shell_sidecar.runtime.settings"
        ).RuntimeSettings
    except (ModuleNotFoundError, AttributeError) as exc:
        raise AssertionError("autonomous Runtime settings are not implemented") from exc
    return settings_type.from_data_dir(data_dir.resolve())


def test_lifespan_initializes_plaintext_resources_before_requests(
    tmp_path: Path,
) -> None:
    request_id = uuid4()
    app = create_app(settings=runtime_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/v1/health/ready",
            headers={"X-Request-ID": str(request_id)},
        )

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == str(request_id)
        assert response.json() == {
            "request_id": str(request_id),
            "ready": True,
            "state": "READY",
        }
