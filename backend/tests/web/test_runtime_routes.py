from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from harness_shell_sidecar.runtime import RuntimeInitializationFailure
from harness_shell_sidecar.runtime.dispatcher import DispatchError
from harness_shell_sidecar.web import create_app
from harness_shell_sidecar.web.dependencies import dispatch_application
from harness_shell_sidecar.web.errors import HttpProblem

from .conftest import valid_initialize_json


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


def test_live_and_not_ready_responses_are_typed_and_correlated(client) -> None:
    live_id = uuid4()
    ready_id = uuid4()

    live = client.get("/v1/health/live", headers=headers(live_id))
    ready = client.get("/v1/health/ready", headers=headers(ready_id))

    assert live.status_code == 200
    assert assert_correlated(live, live_id)["live"] is True
    assert ready.status_code == 503
    assert assert_correlated(ready, ready_id)["error_code"] == "RUNTIME_NOT_READY"


def test_initialize_is_atomic_and_ready_only_after_success(
    client,
    tmp_path: Path,
) -> None:
    initialize_id = uuid4()
    response = client.post(
        "/v1/runtime/initialize",
        headers=headers(initialize_id),
        json=valid_initialize_json(tmp_path),
    )

    assert response.status_code == 200
    assert assert_correlated(response, initialize_id)["state"] == "READY"

    ready_id = uuid4()
    ready = client.get("/v1/health/ready", headers=headers(ready_id))
    assert ready.status_code == 200
    assert assert_correlated(ready, ready_id) == {
        "request_id": str(ready_id),
        "ready": True,
        "state": "READY",
    }


def test_shutdown_requests_server_exit_after_returning_stopped(
    tmp_path: Path,
) -> None:
    exit_requests: list[str] = []

    with TestClient(
        create_app(shutdown_callback=lambda: exit_requests.append("requested"))
    ) as client:
        initialized = client.post(
            "/v1/runtime/initialize",
            headers=headers(),
            json=valid_initialize_json(tmp_path),
        )
        shutdown_id = uuid4()
        shutdown = client.post(
            "/v1/runtime/shutdown",
            headers=headers(shutdown_id),
        )

    assert initialized.status_code == 200
    assert shutdown.status_code == 202
    assert assert_correlated(shutdown, shutdown_id)["state"] == "STOPPED"
    assert exit_requests == ["requested"]


def test_initialize_rejects_unknown_fields_without_opening_storage(
    tmp_path: Path,
) -> None:
    open_count = 0

    def resource_factory(*_args, **_kwargs):
        nonlocal open_count
        open_count += 1
        raise AssertionError("validation must run before resource initialization")

    with TestClient(create_app(resource_factory=resource_factory)) as client:
        response = client.post(
            "/v1/runtime/initialize",
            headers=headers(),
            json={**valid_initialize_json(tmp_path), "unexpected": True},
        )

    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_VALIDATION_FAILED"
    assert open_count == 0


def test_duplicate_initialize_returns_stable_conflict(
    client,
    tmp_path: Path,
) -> None:
    payload = valid_initialize_json(tmp_path)
    assert client.post(
        "/v1/runtime/initialize", headers=headers(), json=payload
    ).status_code == 200

    duplicate_id = uuid4()
    duplicate = client.post(
        "/v1/runtime/initialize",
        headers=headers(duplicate_id),
        json=payload,
    )

    assert duplicate.status_code == 409
    assert (
        assert_correlated(duplicate, duplicate_id)["error_code"]
        == "RUNTIME_ALREADY_INITIALIZED"
    )


def test_partial_initialization_failure_never_enters_ready(tmp_path: Path) -> None:
    def fail_initialize(*_args, **_kwargs):
        raise RuntimeInitializationFailure(
            "AUDIT_CHAIN_INVALID", "audit chain verification failed"
        )

    with TestClient(create_app(resource_factory=fail_initialize)) as client:
        response = client.post(
            "/v1/runtime/initialize",
            headers=headers(),
            json=valid_initialize_json(tmp_path),
        )
        ready = client.get("/v1/health/ready", headers=headers())
        state = client.get("/v1/runtime/state", headers=headers())

    assert response.status_code == 500
    assert response.json()["error_code"] == "AUDIT_CHAIN_INVALID"
    assert ready.status_code == 503
    assert state.json()["state"] == "FAILED"


def test_json_request_larger_than_one_mebibyte_is_rejected(tmp_path: Path) -> None:
    request_id = uuid4()
    oversized = {
        **valid_initialize_json(tmp_path),
        "padding": "x" * 1_048_576,
    }

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/runtime/initialize",
            headers=headers(request_id),
            json=oversized,
        )

    assert response.status_code == 413
    assert assert_correlated(response, request_id)["error_code"] == "REQUEST_TOO_LARGE"


def test_cancel_requires_ready_runtime(client) -> None:
    request_id = uuid4()
    response = client.post(
        f"/v1/requests/{uuid4()}/cancel",
        headers=headers(request_id),
    )

    assert response.status_code == 503
    assert assert_correlated(response, request_id)["error_code"] == "RUNTIME_NOT_READY"


def test_cancel_openapi_path_uses_the_frozen_request_id_parameter() -> None:
    """Keep the generated path compatible with the frozen Rust contract name."""

    paths = create_app().openapi()["paths"]

    assert "/v1/requests/{request_id}/cancel" in paths
    assert "/v1/requests/{target_request_id}/cancel" not in paths


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("DUPLICATE_REQUEST_ID", 409),
        ("REQUEST_CAPACITY_EXCEEDED", 429),
    ],
)
def test_dispatch_capacity_failures_have_fixed_http_status(
    error_code: str,
    expected_status: int,
) -> None:
    """Keep dispatcher concurrency failures distinct at the HTTP boundary."""

    class RejectingDispatcher:
        """Raise one selected real dispatcher boundary error."""

        async def dispatch(self, *_args, **_kwargs):
            raise DispatchError(error_code, "request was rejected")

    class ReadyOwner:
        """Expose the minimal ready resource surface used by HTTP dispatch."""

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


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("HOST_KEY_CONFLICT", 409),
        ("CONNECTION_VERSION_EXHAUSTED", 409),
        ("AGENT_RESPONSE_TOO_LARGE", 413),
    ],
)
def test_domain_error_registry_preserves_fixed_status_categories(
    error_code: str,
    expected_status: int,
) -> None:
    """Prevent stable domain failures from falling through to generic 500."""

    class RejectingDispatcher:
        async def dispatch(self, *_args, **_kwargs):
            raise DispatchError(error_code, "domain request failed")

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
