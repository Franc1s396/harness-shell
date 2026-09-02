"""Emit complete Python logging records as JSON on Sidecar stderr."""

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
StackFrame: TypeAlias = dict[str, str | int]
LogValue: TypeAlias = object
_PROVIDER_REQUEST_ID_HEADERS = ("x-request-id", "x-client-request-id")


class JsonLogFormatter(logging.Formatter):
    """Serialize complete messages, fields, and exceptions into one JSON record."""

    def format(self, record: logging.LogRecord) -> str:
        """Return one compact JSON record without filtering caller-provided content."""

        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        )
        event = getattr(record, "harness_event", None)
        fields = getattr(record, "harness_fields", None)
        if not isinstance(event, str):
            event = "python_log"
        if not isinstance(fields, Mapping):
            fields = {}

        payload: dict[str, object] = {
            "timestamp": timestamp.replace("+00:00", "Z"),
            "level": _normalized_level(record.levelno),
            "component": "python_sidecar",
            "event": event,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(fields)
        if record.exc_info is not None and record.exc_info[1] is not None:
            payload.update(extract_exception_fields(record.exc_info[1]))
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )


def _normalized_level(level: int) -> str:
    """Map standard logging levels to the three-level Sidecar wire contract."""

    if level >= logging.ERROR:
        return "ERROR"
    if level >= logging.WARNING:
        return "WARNING"
    return "INFO"


def configure_stderr_logging(stream: TextIO | None = None) -> None:
    """Install the process-wide INFO logger on stderr or an injected test stream."""

    target = stream if stream is not None else sys.stderr
    if stream is None and hasattr(target, "reconfigure"):
        target.reconfigure(encoding="utf-8", errors="strict")
    handler = logging.StreamHandler(target)
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: LogValue,
) -> None:
    """Log one named event with every caller-provided field unchanged."""

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


def _stack_frames(error: BaseException) -> list[StackFrame]:
    """Extract file basename, line, and function for structured navigation."""

    frames: list[StackFrame] = []
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


def extract_exception_fields(
    error: BaseException,
) -> dict[str, LogValue]:
    """Extract complete exception text, HTTP body, and useful typed metadata."""

    fields: dict[str, LogValue] = {
        "exception_type": _qualified_exception_type(error),
        "exception_text": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }
    body: object | None = None
    request_id: str | None = None
    response_body: str | None = None

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
        response_status = getattr(response, "status_code", None)
        if "http_status" not in fields and isinstance(response_status, int):
            fields["http_status"] = response_status
        if request_id is None and response is not None:
            request_id = _response_request_id(response)
        if response_body is None and response is not None:
            current_response_body = getattr(response, "text", None)
            if isinstance(current_response_body, str):
                response_body = current_response_body

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
    if not response_body and body is not None:
        response_body = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
    if response_body is not None:
        fields["http_response_body"] = response_body

    stack_frames = _stack_frames(error)
    if stack_frames:
        fields["stack_frames"] = stack_frames
    return fields


def log_exception_event(
    logger: logging.Logger,
    event: str,
    error: BaseException,
    *,
    error_code: str,
    **fields: LogValue,
) -> None:
    """Log a failed event with its complete exception and a stable error code."""

    event_fields = dict(fields)
    event_fields["error_code"] = error_code
    logger.log(
        logging.ERROR,
        event,
        exc_info=(type(error), error, error.__traceback__),
        extra={"harness_event": event, "harness_fields": event_fields},
    )
