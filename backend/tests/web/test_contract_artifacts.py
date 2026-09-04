"""Validate the frozen HTTP and runtime WebSocket contract artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness_shell_sidecar.agent.streaming import (
    AgentTurnCompletedEvent,
    AgentTurnStartedEvent,
    AgentTurnTextDeltaEvent,
)
from harness_shell_sidecar.web.contracts import build_openapi_document


HTTP_ROOT = Path("docs/protocol/http")

EXPECTED_OPERATIONS = {
    ("GET", "/v1/health/live"),
    ("GET", "/v1/health/ready"),
    ("GET", "/v1/runtime/state"),
    ("GET", "/v1/runtime/credential-encryption-key"),
    ("GET", "/v1/connections"),
    ("POST", "/v1/connections"),
    ("PATCH", "/v1/connections/{connection_id}"),
    ("DELETE", "/v1/connections/{connection_id}"),
    ("POST", "/v1/host-key-inspections"),
    ("POST", "/v1/host-key-confirmations"),
    ("POST", "/v1/host-key-replacements"),
    ("POST", "/v1/ssh/sessions"),
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
    ("GET", "/v1/sftp/recoveries/{recovery_id}"),
    ("POST", "/v1/sftp/recoveries/{recovery_id}/actions"),
    ("GET", "/v1/agent/api-configs"),
    ("POST", "/v1/agent/api-configs"),
    ("PATCH", "/v1/agent/api-configs/{api_config_id}"),
    ("DELETE", "/v1/agent/api-configs/{api_config_id}"),
    ("POST", "/v1/agent/turns"),
    ("GET", "/v1/diagnostics/log-directory"),
    ("POST", "/v1/diagnostics/log-directory/open"),
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
    "agent_sse_frame_bytes": 65_536,
    "agent_sse_body_bytes": 4_194_304,
    "agent_sse_terminal_reserve_bytes": 65_536,
    "agent_sse_queue_events": 64,
    "agent_result_json_bytes": 1_048_576,
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


def _parse_fixture_frames(
    wire: str,
) -> list[tuple[str, int, dict[str, object]]]:
    """Parse well-framed fixture events before testing one semantic violation."""

    frames: list[tuple[str, int, dict[str, object]]] = []
    assert wire.endswith("\n\n")
    for raw in wire.removesuffix("\n\n").split("\n\n"):
        lines = raw.split("\n")
        assert len(lines) == 3
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("id: ")
        assert lines[2].startswith("data: ")
        data = json.loads(lines[2].removeprefix("data: "))
        assert isinstance(data, dict)
        frames.append(
            (
                lines[0].removeprefix("event: "),
                int(lines[1].removeprefix("id: ")),
                data,
            )
        )
    return frames


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
    removed_collection = "/" + "preparations"
    assert not any(removed_collection in path for _, path in operations)


def test_credential_encryption_fixture_freezes_browser_wire_parameters() -> None:
    """Keep React and Python aligned on the exact hybrid envelope parameters."""

    fixture = load_json(HTTP_ROOT / "fixtures/credential-encryption-v1.json")

    assert fixture == {
        "version": 1,
        "scheme": "RSA-OAEP-256+A256GCM",
        "rsa": {
            "name": "RSA-OAEP",
            "hash": "SHA-256",
            "modulus_length_bits": 3072,
            "public_exponent": 65537,
        },
        "aes": {
            "name": "AES-GCM",
            "key_length_bits": 256,
            "iv_length_bytes": 12,
        },
        "aad_utf8_template": "harness-shell-credential-v1\0{key_id}",
        "base64": "canonical-rfc4648",
    }


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


def test_agent_turn_contract_has_only_sse_success() -> None:
    """Expose the strict event union without an obsolete JSON success model."""

    document = build_openapi_document()
    operation = document["paths"]["/v1/agent/turns"]["post"]
    success = operation["responses"]["200"]
    accept = next(
        parameter
        for parameter in operation["parameters"]
        if parameter.get("name", "").lower() == "accept"
    )

    assert set(success["content"]) == {"text/event-stream"}
    assert accept["required"] is True
    assert accept["schema"] == {"type": "string", "const": "text/event-stream"}
    assert success["headers"]["X-Request-ID"]["description"] == (
        "Matches the request and every Agent SSE event request_id."
    )
    assert "AgentTurnResponse" not in document["components"]["schemas"]
    for name in (
        "AgentTurnStartedEvent",
        "AgentTurnTextDeltaEvent",
        "AgentTurnCompletedEvent",
        "AgentTurnFailedEvent",
    ):
        assert name in document["components"]["schemas"]


def test_agent_sse_fixtures_freeze_valid_and_invalid_sequences() -> None:
    """Keep cross-language framing and state-machine cases deterministic."""

    valid = load_json(HTTP_ROOT / "fixtures/agent/valid-http-v1.json")
    stream = next(case for case in valid["cases"] if case["name"] == "agent-turn-sse")
    assert stream["event_types"] == [
        "agent.turn.started",
        "agent.turn.text_delta",
        "agent.turn.completed",
    ]
    assert stream["wire_utf8"].endswith("\n\n")

    invalid = load_json(HTTP_ROOT / "fixtures/agent/invalid-http-v1.json")
    names = {case["name"] for case in invalid["cases"]}
    assert {
        "missing-started",
        "sequence-gap",
        "identity-change",
        "unknown-event",
        "duplicate-terminal",
        "frame-too-large",
        "body-too-large",
        "comment-field",
        "retry-field",
        "multiline-data",
        "bare-cr",
    } <= names

    cases = {case["name"]: case for case in invalid["cases"]}
    disconnected = cases["start-turn-with-disconnected-session"]
    assert disconnected["request"]["headers"]["Accept"] == "text/event-stream"
    assert "user_message" in disconnected["request"]["body"]
    assert "user_text" not in disconnected["request"]["body"]
    assert disconnected["expected_http_status"] == 404
    assert disconnected["expected_error_code"] == "SSH_SESSION_UNAVAILABLE"
    assert cases["frame-too-large"]["generated_wire"]["encoded_bytes"] == 65_537
    assert cases["body-too-large"]["generated_wire"]["encoded_bytes"] == 4_194_305

    missing = _parse_fixture_frames(cases["missing-started"]["wire_utf8"])
    AgentTurnTextDeltaEvent.model_validate_json(json.dumps(missing[0][2]))
    assert missing[0][0:2] == ("agent.turn.text_delta", 0)

    gap = _parse_fixture_frames(cases["sequence-gap"]["wire_utf8"])
    AgentTurnStartedEvent.model_validate_json(json.dumps(gap[0][2]))
    AgentTurnCompletedEvent.model_validate_json(json.dumps(gap[1][2]))
    assert [frame[1] for frame in gap] == [0, 2]

    changed = _parse_fixture_frames(cases["identity-change"]["wire_utf8"])
    AgentTurnStartedEvent.model_validate_json(json.dumps(changed[0][2]))
    AgentTurnCompletedEvent.model_validate_json(json.dumps(changed[1][2]))
    assert changed[0][2]["conversation_id"] != changed[1][2]["conversation_id"]

    duplicate = _parse_fixture_frames(cases["duplicate-terminal"]["wire_utf8"])
    AgentTurnStartedEvent.model_validate_json(json.dumps(duplicate[0][2]))
    AgentTurnCompletedEvent.model_validate_json(json.dumps(duplicate[1][2]))
    AgentTurnCompletedEvent.model_validate_json(json.dumps(duplicate[2][2]))
    assert [frame[0] for frame in duplicate] == [
        "agent.turn.started",
        "agent.turn.completed",
        "agent.turn.completed",
    ]


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
    assert len(paths) == 11
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path
