"""Validate the frozen HTTP and runtime WebSocket contract artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


HTTP_ROOT = Path("docs/protocol/http")

EXPECTED_OPERATIONS = {
    ("GET", "/v1/health/live"),
    ("GET", "/v1/health/ready"),
    ("POST", "/v1/runtime/initialize"),
    ("GET", "/v1/runtime/state"),
    ("POST", "/v1/runtime/shutdown"),
    ("POST", "/v1/requests/{request_id}/cancel"),
    ("GET", "/v1/connections"),
    ("GET", "/v1/connections/{connection_id}"),
    ("POST", "/v1/connections"),
    ("PATCH", "/v1/connections/{connection_id}"),
    ("DELETE", "/v1/connections/{connection_id}"),
    ("POST", "/v1/host-key-inspections"),
    ("POST", "/v1/host-key-confirmations"),
    ("POST", "/v1/host-key-replacements"),
    ("POST", "/v1/ssh/sessions"),
    ("GET", "/v1/ssh/sessions"),
    ("DELETE", "/v1/ssh/sessions/{ssh_session_id}"),
    ("POST", "/v1/pty/sessions"),
    ("POST", "/v1/pty/sessions/{pty_session_id}/resize"),
    ("DELETE", "/v1/pty/sessions/{pty_session_id}"),
    ("POST", "/v1/sftp/contexts"),
    ("POST", "/v1/sftp/listings"),
    ("GET", "/v1/sftp/listings/{listing_id}/batches/{sequence}"),
    ("DELETE", "/v1/sftp/listings/{listing_id}"),
    ("POST", "/v1/sftp/metadata/lstat"),
    ("POST", "/v1/sftp/metadata/readlink"),
    ("POST", "/v1/sftp/metadata/realpath"),
    ("POST", "/v1/sftp/hashes/sha256"),
    ("POST", "/v1/sftp/uploads/preflight"),
    ("POST", "/v1/sftp/uploads"),
    ("PUT", "/v1/sftp/uploads/{operation_id}/chunks/{sequence}"),
    ("POST", "/v1/sftp/uploads/{operation_id}/finish"),
    ("POST", "/v1/sftp/uploads/{operation_id}/abort"),
    ("POST", "/v1/sftp/downloads"),
    ("GET", "/v1/sftp/downloads/{operation_id}/chunks/{sequence}"),
    ("POST", "/v1/sftp/downloads/{operation_id}/finish"),
    ("POST", "/v1/sftp/downloads/{operation_id}/abort"),
    ("POST", "/v1/sftp/directories"),
    ("POST", "/v1/sftp/renames"),
    ("POST", "/v1/sftp/removals"),
    ("POST", "/v1/sftp/deletions/preflight"),
    ("POST", "/v1/sftp/deletions/{operation_id}/execute"),
    ("GET", "/v1/sftp/recoveries"),
    ("POST", "/v1/sftp/recoveries/{recovery_id}/actions"),
    ("GET", "/v1/agent/api-configs"),
    ("GET", "/v1/agent/api-configs/{api_config_id}"),
    ("POST", "/v1/agent/api-configs"),
    ("PATCH", "/v1/agent/api-configs/{api_config_id}"),
    ("DELETE", "/v1/agent/api-configs/{api_config_id}"),
    ("POST", "/v1/agent/turns"),
}

EXPECTED_WEBSOCKET_TYPES = {
    "pty.input",
    "pty.input_result",
    "pty.output",
    "pty.closed",
    "ssh.connection_state",
    "sftp.operation_progress",
    "runtime.ping",
    "runtime.pong",
    "runtime.error",
}

EXPECTED_LIMITS = {
    "json_request_bytes": 1_048_576,
    "json_response_bytes": 1_048_576,
    "websocket_text_bytes": 65_536,
    "pty_decoded_bytes": 32_768,
    "sftp_chunk_bytes": 262_144,
    "active_application_requests": 16,
    "websocket_queue_messages": 64,
    "heartbeat_interval_ms": 5_000,
    "heartbeat_timeout_ms": 15_000,
    "startup_timeout_ms": 5_000,
    "shutdown_timeout_ms": 3_000,
}


def load_json(path: Path) -> dict[str, Any]:
    """Load one UTF-8 contract artifact as a JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_frozen_openapi_has_exact_http_operations() -> None:
    """Reject missing, extra, or generic RPC HTTP operations."""

    document = load_json(HTTP_ROOT / "openapi-v1.json")
    operations = {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert operations == EXPECTED_OPERATIONS
    assert all(path != "/v1/rpc" for _, path in operations)


def test_each_operation_has_request_correlation_and_problem_details() -> None:
    """Require the shared request ID and typed error boundary on every operation."""

    document = load_json(HTTP_ROOT / "openapi-v1.json")
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            parameters = operation.get("parameters", [])
            assert any(
                parameter.get("$ref") == "#/components/parameters/RequestId"
                for parameter in parameters
            ), (method, path)
            responses = operation["responses"]
            assert any(status.startswith("2") for status in responses), (method, path)
            assert responses["default"]["$ref"] == "#/components/responses/Problem"


def test_websocket_union_and_limits_are_exact() -> None:
    """Freeze the realtime union and all cross-language byte/time limits."""

    websocket = load_json(HTTP_ROOT / "runtime-websocket-v1.schema.json")
    assert set(websocket["$defs"]["message_type"]["enum"]) == EXPECTED_WEBSOCKET_TYPES
    assert websocket["x-max-text-message-bytes"] == 65_536
    assert websocket["x-queue-capacity"] == 64
    assert load_json(HTTP_ROOT / "fixtures/limits-v1.json") == EXPECTED_LIMITS


def test_invalid_fixtures_name_the_expected_stable_error() -> None:
    """Require every invalid cross-language case to state its expected error code."""

    paths = [
        HTTP_ROOT / "fixtures/runtime-invalid-v1.json",
        HTTP_ROOT / "fixtures/agent/invalid-http-v1.json",
        HTTP_ROOT / "fixtures/manual-sftp/invalid-http-v1.json",
    ]
    for path in paths:
        cases = load_json(path)["cases"]
        assert cases
        assert all(case.get("expected_error_code") for case in cases)


def test_http_artifacts_do_not_reintroduce_the_old_transport() -> None:
    """Keep old framing and generic method wrappers out of the frozen artifacts."""

    forbidden = ("FrameEnvelope", "Content-Length", '"/v1/rpc"', '"method":')
    paths = list(HTTP_ROOT.rglob("*.json"))
    assert len(paths) == 10
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path
