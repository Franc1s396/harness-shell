from __future__ import annotations

from pathlib import Path
from uuid import uuid4

def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def test_pty_control_routes_map_missing_sessions_without_network_io(
    client,
    tmp_path: Path,
) -> None:
    opened = client.post(
        "/v1/pty/sessions",
        headers=request_headers(),
        json={"ssh_session_id": str(uuid4()), "cols": 80, "rows": 24},
    )
    resized = client.post(
        f"/v1/pty/sessions/{uuid4()}/resize",
        headers=request_headers(),
        json={"cols": 100, "rows": 30},
    )
    closed = client.delete(
        f"/v1/pty/sessions/{uuid4()}", headers=request_headers()
    )

    assert opened.status_code == 404
    assert opened.json()["error_code"] == "SSH_SESSION_NOT_FOUND"
    assert resized.status_code == 404
    assert resized.json()["error_code"] == "PTY_SESSION_NOT_FOUND"
    assert closed.status_code == 404
    assert closed.json()["error_code"] == "PTY_SESSION_NOT_FOUND"


def test_pty_write_has_no_http_route(client) -> None:
    response = client.post(
        f"/v1/pty/sessions/{uuid4()}/write",
        headers=request_headers(),
        json={"data_b64": "YQ=="},
    )

    assert response.status_code == 404
