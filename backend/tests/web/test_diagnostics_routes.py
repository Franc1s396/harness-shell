"""Verify the Python-owned diagnostics directory HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app


def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def test_log_directory_reports_only_availability(tmp_path: Path) -> None:
    """Do not expose the Python-owned absolute log path to React."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/v1/diagnostics/log-directory",
            headers=request_headers(),
        )

    assert response.status_code == 200
    assert set(response.json()) == {"request_id", "available"}
    assert response.json()["available"] is True
    assert str(settings.log_dir) not in response.text


def test_open_log_directory_uses_fixed_runtime_path(tmp_path: Path) -> None:
    """Only the settings-derived directory may reach the OS opener."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)
    opened: list[Path] = []
    with TestClient(
        create_app(settings=settings, log_directory_opener=opened.append)
    ) as client:
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 204
    assert opened == [settings.log_dir]


def test_open_log_directory_rejects_missing_fixed_path(tmp_path: Path) -> None:
    """A missing fixed directory is an explicit diagnostics failure."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        # Autonomous startup creates the fixed directory. Removing the empty
        # directory after startup models the real external-deletion failure.
        settings.log_dir.rmdir()
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "LOG_DIRECTORY_UNAVAILABLE"


def test_open_log_directory_reports_os_start_failure(tmp_path: Path) -> None:
    """Keep Explorer launch errors stable without exposing the absolute path."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)

    def fail_open(_path: Path) -> None:
        """Simulate one bounded OS process-start failure."""

        raise OSError("test marker must not cross the HTTP boundary")

    with TestClient(
        create_app(settings=settings, log_directory_opener=fail_open)
    ) as client:
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 500
    assert response.json()["error_code"] == "LOG_DIRECTORY_OPEN_FAILED"
    assert "test marker" not in response.text


def test_cors_allows_only_the_two_fixed_react_origins(tmp_path: Path) -> None:
    """Reject arbitrary origins while exposing the direct-client headers."""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        allowed = client.options(
            "/v1/diagnostics/log-directory",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Request-ID",
            },
        )
        rejected = client.options(
            "/v1/diagnostics/log-directory",
            headers={
                "Origin": "http://192.168.1.4:8765",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Request-ID",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
