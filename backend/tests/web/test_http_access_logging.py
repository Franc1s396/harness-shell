import io
import logging
import re
from uuid import uuid4

from fastapi import Response
from fastapi.testclient import TestClient

from harness_shell_sidecar.telemetry import configure_stderr_logging


def _access_record(stderr: io.StringIO) -> list[str]:
    """Return the single HTTP access record emitted by the private API."""

    matches = [
        line
        for line in stderr.getvalue().splitlines()
        if "harness_shell_sidecar.web.access" in line
    ]
    assert len(matches) == 1
    return matches[0].split(" | ", maxsplit=5)


def test_successful_http_request_logs_route_status_duration_and_request_id(
    client: TestClient,
) -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)
    request_id = uuid4()
    secret_path_marker = "must-not-appear-in-access-log"

    async def probe(_opaque_value: str) -> Response:
        """Return a test-only success through the production middleware stack."""

        return Response(status_code=204)

    client.app.add_api_route(
        "/__test__/access/{_opaque_value}",
        probe,
        methods=["GET"],
    )
    response = client.get(
        f"/__test__/access/{secret_path_marker}",
        headers={"X-Request-ID": str(request_id)},
    )

    assert response.status_code == 204
    access_fields = _access_record(stderr)
    _timestamp, level, logged_request_id, _thread, _logger, message = access_fields
    assert level == "INFO "
    assert logged_request_id == str(request_id)
    assert re.fullmatch(
        r"http_request_completed method=GET "
        r"route=/__test__/access/\{_opaque_value\} status=204 duration_ms=\d+",
        message,
    )
    assert secret_path_marker not in " | ".join(access_fields)


def test_rejected_http_request_logs_warning_with_returned_status(
    client: TestClient,
) -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)
    request_id = uuid4()

    response = client.get(
        "/__test__/missing",
        headers={"X-Request-ID": str(request_id)},
    )

    assert response.status_code == 404
    _timestamp, level, logged_request_id, _thread, _logger, message = (
        _access_record(stderr)
    )
    assert level == "WARNING"
    assert logged_request_id == str(request_id)
    assert re.fullmatch(
        r"http_request_completed method=GET route=<unmatched> "
        r"status=404 duration_ms=\d+",
        message,
    )


def test_internal_http_failure_logs_error_with_returned_status(
    client: TestClient,
) -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)
    request_id = uuid4()

    async def fail() -> Response:
        """Return a test-only server failure through the production middleware."""

        return Response(status_code=500)

    client.app.add_api_route("/__test__/failure", fail, methods=["GET"])
    response = client.get(
        "/__test__/failure",
        headers={"X-Request-ID": str(request_id)},
    )

    assert response.status_code == 500
    _timestamp, level, logged_request_id, _thread, _logger, message = (
        _access_record(stderr)
    )
    assert level == "ERROR"
    assert logged_request_id == str(request_id)
    assert re.fullmatch(
        r"http_request_completed method=GET route=/__test__/failure "
        r"status=500 duration_ms=\d+",
        message,
    )


def test_runtime_state_request_is_excluded_from_http_access_logging(
    client: TestClient,
) -> None:
    stderr = io.StringIO()
    configure_stderr_logging(stderr)

    response = client.get(
        "/v1/runtime/state",
        headers={"X-Request-ID": str(uuid4())},
    )

    assert response.status_code == 200
    assert "harness_shell_sidecar.web.access" not in stderr.getvalue()
