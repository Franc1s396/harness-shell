"""Deterministic cross-language HTTP and Runtime WebSocket contract export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, TypeAdapter

from harness_shell_sidecar.agent.handlers import (
    AgentTurnRequest,
    ModelApiConfigCreateRequest,
    ModelApiConfigUpdateRequest,
)
from harness_shell_sidecar.connections.handlers import (
    ConnectionCreateRequest,
    ConnectionUpdateRequest,
)
from harness_shell_sidecar.connections.models import (
    HostKeyCandidate,
)
from harness_shell_sidecar.ssh.models import HostKeyInspectionRequest, SshConnectRequest
from harness_shell_sidecar.runtime.settings import RuntimeSettings

from .app import create_app
from .models import (
    ProblemDetails,
    PtyClosedMessage,
    PtyInputMessage,
    PtyInputResultMessage,
    PtyOutputMessage,
    RuntimeErrorMessage,
    RuntimePingMessage,
    RuntimePongMessage,
    SftpOperationProgressMessage,
    SshConnectionStateMessage,
)
from .routes.host_keys import HostKeyReplacementRequest
from .routes.manual_sftp import (
    SftpDeletePreflightRequest,
    SftpDownloadBeginRequest,
    SftpMkdirRequest,
    SftpPathRequest,
    SftpRecoveryActionRequest,
    SftpRemoveRequest,
    SftpRenameRequest,
    SftpSessionRequest,
    SftpUploadBeginRequest,
)
from .routes.terminal import PtyOpenRequest, PtyResizeRequest
from .websocket import MAX_WEBSOCKET_TEXT_BYTES, WEBSOCKET_QUEUE_CAPACITY


_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_JSON_BODY_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("post", "/v1/connections"): ConnectionCreateRequest,
    ("patch", "/v1/connections/{connection_id}"): ConnectionUpdateRequest,
    ("post", "/v1/host-key-inspections"): HostKeyInspectionRequest,
    ("post", "/v1/host-key-confirmations"): HostKeyCandidate,
    ("post", "/v1/host-key-replacements"): HostKeyReplacementRequest,
    ("post", "/v1/ssh/sessions"): SshConnectRequest,
    ("post", "/v1/pty/sessions"): PtyOpenRequest,
    ("post", "/v1/pty/sessions/{pty_session_id}/resize"): PtyResizeRequest,
    ("post", "/v1/sftp/contexts"): SftpSessionRequest,
    ("post", "/v1/sftp/listings"): SftpPathRequest,
    ("post", "/v1/sftp/metadata/lstat"): SftpPathRequest,
    ("post", "/v1/sftp/metadata/readlink"): SftpPathRequest,
    ("post", "/v1/sftp/hashes/sha256"): SftpPathRequest,
    ("post", "/v1/sftp/uploads/preflight"): SftpPathRequest,
    ("post", "/v1/sftp/uploads"): SftpUploadBeginRequest,
    ("post", "/v1/sftp/downloads"): SftpDownloadBeginRequest,
    ("post", "/v1/sftp/directories"): SftpMkdirRequest,
    ("post", "/v1/sftp/renames"): SftpRenameRequest,
    ("post", "/v1/sftp/removals"): SftpRemoveRequest,
    ("post", "/v1/sftp/deletions/preflight"): SftpDeletePreflightRequest,
    ("post", "/v1/sftp/recoveries/{recovery_id}/actions"): SftpRecoveryActionRequest,
    ("post", "/v1/agent/api-configs"): ModelApiConfigCreateRequest,
    ("patch", "/v1/agent/api-configs/{api_config_id}"): ModelApiConfigUpdateRequest,
    ("post", "/v1/agent/turns"): AgentTurnRequest,
}

_WEBSOCKET_TYPES = [
    "pty.input",
    "pty.input_result",
    "pty.output",
    "pty.closed",
    "ssh.connection_state",
    "sftp.operation_progress",
    "runtime.ping",
    "runtime.pong",
    "runtime.error",
]
_WebSocketMessage = Annotated[
    PtyInputMessage
    | PtyInputResultMessage
    | PtyOutputMessage
    | PtyClosedMessage
    | SshConnectionStateMessage
    | SftpOperationProgressMessage
    | RuntimePingMessage
    | RuntimePongMessage
    | RuntimeErrorMessage,
    Field(discriminator="type"),
]


def _add_model_schema(
    schemas: dict[str, object],
    model: type[BaseModel],
) -> str:
    """Merge one Pydantic model and its definitions into OpenAPI components."""

    schema = model.model_json_schema(
        ref_template="#/components/schemas/{model}",
        mode="validation",
    )
    definitions = schema.pop("$defs", {})
    for name, definition in definitions.items():
        schemas[name] = definition
    name = model.__name__
    schemas[name] = schema
    return name


def _success_headers() -> dict[str, object]:
    """Describe the correlation header present on every successful response."""

    return {
        "X-Request-ID": {
            "description": "Matches the request and JSON body request_id.",
            "schema": {"type": "string", "format": "uuid"},
        }
    }


def build_openapi_document() -> dict[str, object]:
    """Generate the strict HTTP document from the actual FastAPI application."""

    settings = RuntimeSettings.from_data_dir(Path("C:/harness-shell-contract"))
    document = create_app(settings=settings).openapi()
    components = document.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    problem_name = _add_model_schema(schemas, ProblemDetails)
    components["parameters"] = {
        "RequestId": {
            "name": "X-Request-ID",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        },
        "ChunkOffset": {
            "name": "X-Chunk-Offset",
            "in": "header",
            "required": True,
            "schema": {
                "type": "integer",
                "minimum": 0,
                "maximum": 2**53 - 1,
            },
        },
    }
    components["responses"] = {
        "Problem": {
            "description": "Stable typed failure.",
            "headers": _success_headers(),
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": f"#/components/schemas/{problem_name}"}
                }
            },
        }
    }

    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in _HTTP_METHODS:
                continue
            parameters = [
                parameter
                for parameter in operation.get("parameters", [])
                if not (
                    parameter.get("in") == "header"
                    and parameter.get("name", "").lower() == "x-request-id"
                )
            ]
            operation["parameters"] = [
                {"$ref": "#/components/parameters/RequestId"},
                *parameters,
            ]
            responses = operation["responses"]
            responses.pop("422", None)
            responses["default"] = {"$ref": "#/components/responses/Problem"}
            for status_code, response in responses.items():
                if status_code.startswith("2"):
                    response["headers"] = _success_headers()

            body_model = _JSON_BODY_MODELS.get((method, path))
            if body_model is not None:
                model_name = _add_model_schema(schemas, body_model)
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": f"#/components/schemas/{model_name}"
                            }
                        }
                    },
                }

    upload = document["paths"][
        "/v1/sftp/uploads/{operation_id}/chunks/{sequence}"
    ]["put"]
    upload["requestBody"] = {
        "required": True,
        "content": {
            "application/octet-stream": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "minLength": 1,
                    "maxLength": 262_144,
                }
            }
        },
    }
    upload["parameters"].append(
        {"$ref": "#/components/parameters/ChunkOffset"}
    )
    download = document["paths"][
        "/v1/sftp/downloads/{operation_id}/chunks/{sequence}"
    ]["get"]["responses"]["200"]
    download["content"] = {
        "application/octet-stream": {
            "schema": {
                "type": "string",
                "format": "binary",
                "maxLength": 262_144,
            }
        }
    }
    download["headers"] = {
        **_success_headers(),
        "X-Chunk-Sequence": {"schema": {"type": "integer", "minimum": 0}},
        "X-Chunk-Offset": {"schema": {"type": "integer", "minimum": 0}},
        "X-Chunk-Byte-Count": {
            "schema": {"type": "integer", "minimum": 0, "maximum": 262_144}
        },
        "X-Chunk-EOF": {"schema": {"type": "boolean"}},
    }

    document["paths"] = dict(sorted(document["paths"].items()))
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    return document


def build_websocket_schema() -> dict[str, object]:
    """Generate the strict nine-message WebSocket union from Pydantic models."""

    schema = TypeAdapter(_WebSocketMessage).json_schema(
        ref_template="#/$defs/{model}",
        mode="validation",
    )
    definitions = schema.setdefault("$defs", {})
    definitions["message_type"] = {
        "type": "string",
        "enum": _WEBSOCKET_TYPES,
    }
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://harness-shell.local/schemas/"
                "runtime-websocket-v1.schema.json"
            ),
            "title": "Harness Shell Runtime WebSocket v1",
            "description": (
                "Strict server and client messages carried by "
                "/v1/runtime/events after HTTP runtime initialization."
            ),
            "x-max-text-message-bytes": MAX_WEBSOCKET_TEXT_BYTES,
            "x-queue-capacity": WEBSOCKET_QUEUE_CAPACITY,
        }
    )
    return schema


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one artifact deterministically as UTF-8 with a final newline."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "build_openapi_document",
    "build_websocket_schema",
    "canonical_json_bytes",
]
