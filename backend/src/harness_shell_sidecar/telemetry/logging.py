"""Emit allowlisted Sidecar diagnostics without serializing business payloads."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import traceback
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
SafeStackFrame: TypeAlias = dict[str, str | int]
SafeLogValue: TypeAlias = JsonScalar | list[SafeStackFrame]

_ALLOWED_FIELDS = frozenset(
    {
        "agent_run_id",
        "conversation_id",
        "ssh_session_id",
        "api_config_id",
        "api_type",
        "model",
        "node",
        "react_iteration",
        "duration_ms",
        "route_source",
        "route_target",
        "error_code",
        "exception_type",
        "http_status",
        "provider_error_type",
        "provider_error_code",
        "provider_request_id",
        "response_body_length",
        "response_body_sha256",
        "stack_frames",
    }
)
_PROVIDER_REQUEST_ID_HEADERS = ("x-request-id", "x-client-request-id")


class JsonLogFormatter(logging.Formatter):
    """Serialize only the structured event envelope and validated safe fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Return one compact JSON record without arbitrary message or exc_info text."""

        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        )
        event = getattr(record, "harness_event", None)
        fields = getattr(record, "harness_fields", None)
        try:
            if not isinstance(event, str) or not isinstance(fields, dict):
                raise TypeError
            _validate_fields(fields)
        except (TypeError, ValueError):
            # A third-party LogRecord must not gain structured-log trust by
            # spoofing our extra attribute names with arbitrary business data.
            event = "unstructured_python_log"
            fields = {}

        payload: dict[str, SafeLogValue] = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": _normalized_level(record.levelno),
            "component": "python_sidecar",
            "event": event,
            "logger": record.name,
        }
        payload.update(fields)
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )


def _normalized_level(level: int) -> str:
    """Map standard logging levels to the three-level Sidecar wire contract."""

    if level >= logging.ERROR:
        return "ERROR"
    if level >= logging.WARNING:
        return "WARNING"
    return "INFO"


def _is_json_scalar(value: object) -> bool:
    """Return whether a value is an approved scalar log-field value."""

    return value is None or isinstance(value, (str, int, float, bool))


def _is_safe_stack_frames(value: object) -> bool:
    """Validate the exact metadata-only shape allowed for stack frames."""

    if not isinstance(value, list):
        return False
    for frame in value:
        if not isinstance(frame, dict) or set(frame) != {"file", "line", "function"}:
            return False
        if not isinstance(frame["file"], str):
            return False
        if not isinstance(frame["line"], int) or isinstance(frame["line"], bool):
            return False
        if not isinstance(frame["function"], str):
            return False
    return True


def _validate_fields(fields: Mapping[str, object]) -> None:
    """Reject unknown names or values that cannot enter the safe JSON envelope."""

    unsupported = set(fields) - _ALLOWED_FIELDS
    if unsupported:
        name = sorted(unsupported)[0]
        raise ValueError(f"unsupported log field: {name}")
    for name, value in fields.items():
        valid = (
            _is_safe_stack_frames(value)
            if name == "stack_frames"
            else _is_json_scalar(value)
        )
        if not valid:
            raise TypeError(f"unsupported log field value: {name}")
    json.dumps(
        fields,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def configure_stderr_logging(stream: TextIO | None = None) -> None:
    """Install the process-wide INFO logger on stderr or an injected test stream."""

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: SafeLogValue,
) -> None:
    """Log one named event after validating its explicit safe-field allowlist."""

    _validate_fields(fields)
    logger.log(
        level,
        event,
        extra={"harness_event": event, "harness_fields": fields},
    )


def _walk_exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield each exception once, preferring explicit causes over contexts."""

    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        yield current

        # Push context first so the explicit cause is inspected first by the stack.
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def _qualified_exception_type(error: BaseException) -> str:
    """Return a type identifier without evaluating exception message data."""

    error_type = type(error)
    return f"{error_type.__module__}.{error_type.__qualname__}"


def _mapping_provider_fields(body: object) -> dict[str, JsonScalar]:
    """Extract only scalar Provider type/code fields from supported body mappings."""

    if not isinstance(body, Mapping):
        return {}
    candidate: Mapping[object, object] = body
    nested = body.get("error")
    if isinstance(nested, Mapping):
        candidate = nested

    fields: dict[str, JsonScalar] = {}
    provider_type = candidate.get("type")
    if isinstance(provider_type, (str, int, float, bool)) or provider_type is None:
        if "type" in candidate:
            fields["provider_error_type"] = provider_type
    provider_code = candidate.get("code")
    if isinstance(provider_code, (str, int, float, bool)) or provider_code is None:
        if "code" in candidate:
            fields["provider_error_code"] = provider_code
    return fields


def _canonical_body_digest(body: object) -> tuple[int, str] | None:
    """Return only canonical JSON byte length and SHA-256 for a serializable body."""

    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _response_request_id(response: object) -> str | None:
    """Read only the two approved request-ID headers from a response object."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    for name in _PROVIDER_REQUEST_ID_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _safe_stack_frames(error: BaseException) -> list[SafeStackFrame]:
    """Extract file basename, line, and function without locals or messages."""

    frames: list[SafeStackFrame] = []
    for current in _walk_exception_chain(error):
        if current.__traceback__ is None:
            continue
        for frame in traceback.extract_tb(current.__traceback__):
            frames.append(
                {
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
            )
    return frames


def extract_safe_exception_fields(
    error: BaseException,
) -> dict[str, SafeLogValue]:
    """Extract allowlisted exception metadata without message, URL, headers, or body."""

    fields: dict[str, SafeLogValue] = {
        "exception_type": _qualified_exception_type(error)
    }
    body: object | None = None
    request_id: str | None = None

    for current in _walk_exception_chain(error):
        status_code = getattr(current, "status_code", None)
        if "http_status" not in fields and isinstance(status_code, int):
            fields["http_status"] = status_code

        current_request_id = getattr(current, "request_id", None)
        if (
            request_id is None
            and isinstance(current_request_id, str)
            and current_request_id
        ):
            request_id = current_request_id

        response = getattr(current, "response", None)
        if request_id is None and response is not None:
            request_id = _response_request_id(response)

        current_body = getattr(current, "body", None)
        if body is None and current_body is not None:
            body = current_body

    if request_id is not None:
        fields["provider_request_id"] = request_id
    if body is not None:
        fields.update(_mapping_provider_fields(body))
        digest = _canonical_body_digest(body)
        if digest is not None:
            fields["response_body_length"] = digest[0]
            fields["response_body_sha256"] = digest[1]

    stack_frames = _safe_stack_frames(error)
    if stack_frames:
        fields["stack_frames"] = stack_frames
    return fields


def log_exception_event(
    logger: logging.Logger,
    event: str,
    error: BaseException,
    *,
    error_code: str,
    **fields: SafeLogValue,
) -> None:
    """Log a failed event with safe cause metadata and a stable public error code."""

    event_fields = dict(fields)
    event_fields["error_code"] = error_code
    event_fields.update(extract_safe_exception_fields(error))
    log_event(logger, logging.ERROR, event, **event_fields)
