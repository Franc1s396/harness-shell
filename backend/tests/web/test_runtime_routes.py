from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.runtime.dispatcher import DispatchError
from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app
from harness_shell_sidecar.web.dependencies import dispatch_application
from harness_shell_sidecar.web.errors import HttpProblem


def headers(request_id: UUID | None = None) -> dict[str, str]:
    """Create the required HTTP request-correlation header."""

    return {"X-Request-ID": str(request_id or uuid4())}


def assert_correlated(response, request_id: UUID) -> dict[str, object]:
    """Assert the header/body correlation invariant and return decoded JSON."""

    body = response.json()
    assert response.headers["X-Request-ID"] == str(request_id)
    assert body["request_id"] == str(request_id)
    return body


def test_app_disables_runtime_documentation_and_requires_request_id(client) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404

    missing = client.get("/v1/health/live")
    invalid = client.get(
        "/v1/health/live", headers={"X-Request-ID": "not-a-uuid"}
    )
    assert missing.status_code == 400
    assert missing.json()["error_code"] == "INVALID_REQUEST_ID"
    assert invalid.status_code == 400
    assert invalid.json()["error_code"] == "INVALID_REQUEST_ID"


def test_liveness_readiness_and_state_are_ready_after_lifespan_start(client) -> None:
    live_id = uuid4()
    ready_id = uuid4()
    state_id = uuid4()

    live = client.get("/v1/health/live", headers=headers(live_id))
    ready = client.get("/v1/health/ready", headers=headers(ready_id))
    state = client.get("/v1/runtime/state", headers=headers(state_id))

    assert assert_correlated(live, live_id)["live"] is True
    assert assert_correlated(ready, ready_id) == {
        "request_id": str(ready_id),
        "ready": True,
        "state": "READY",
    }
    assert assert_correlated(state, state_id)["state"] == "READY"


def test_legacy_runtime_mutation_routes_do_not_exist(client) -> None:
    assert client.post("/v1/runtime/" + "initialize", headers=headers(), json={}).status_code == 404
    assert client.post("/v1/runtime/" + "shutdown", headers=headers()).status_code == 404


def test_json_request_larger_than_one_mebibyte_is_rejected(client) -> None:
    request_id = uuid4()
    response = client.post(
        "/v1/connections",
        headers=headers(request_id),
        json={"padding": "x" * 1_048_576},
    )
    assert response.status_code == 413
    assert assert_correlated(response, request_id)["error_code"] == "REQUEST_TOO_LARGE"


def test_request_cancel_route_is_not_exposed(tmp_path: Path) -> None:
    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime-data").resolve())
    paths = create_app(settings=settings).openapi()["paths"]
    assert not any(path.startswith("/v1/requests/") for path in paths)


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("DUPLICATE_REQUEST_ID", 409),
        ("REQUEST_CAPACITY_EXCEEDED", 429),
        ("HOST_KEY_CONFLICT", 409),
        ("CONNECTION_VERSION_EXHAUSTED", 409),
        ("AGENT_RESPONSE_TOO_LARGE", 413),
    ],
)
def test_dispatch_failures_preserve_fixed_http_status(
    error_code: str,
    expected_status: int,
) -> None:
    """Keep stable dispatcher and domain failures in their fixed categories."""

    class RejectingDispatcher:
        async def dispatch(self, *_args, **_kwargs):
            raise DispatchError(error_code, "request was rejected")

    class ReadyOwner:
        def require_resources(self):
            return SimpleNamespace(dispatcher=RejectingDispatcher())

    async def scenario() -> None:
        with pytest.raises(HttpProblem) as captured:
            await dispatch_application(
                ReadyOwner(),  # type: ignore[arg-type]
                uuid4(),
                "test.operation",
                {},
            )
        assert captured.value.problem.status == expected_status
        assert captured.value.problem.error_code == error_code

    __import__("asyncio").run(scenario())
