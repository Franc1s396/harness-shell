from __future__ import annotations

from uuid import uuid4

def request_headers() -> dict[str, str]:
    """Create one valid request correlation header."""

    return {"X-Request-ID": str(uuid4())}


def test_ssh_connect_accepts_only_identity_and_missing_profile_is_typed(
    autonomous_client,
) -> None:
    payload = {"connection_id": str(uuid4())}

    response = autonomous_client.post(
        "/v1/ssh/sessions",
        headers=request_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "CONNECTION_NOT_FOUND"

    forbidden = autonomous_client.post(
        "/v1/ssh/sessions",
        headers=request_headers(),
        json={**payload, "password_b64": "forbidden"},
    )
    assert forbidden.status_code == 422


def test_ssh_disconnect_missing_session_is_not_found(autonomous_client) -> None:
    response = autonomous_client.delete(
        f"/v1/ssh/sessions/{uuid4()}", headers=request_headers()
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "SSH_SESSION_NOT_FOUND"
