"""Allowlisted local representation of OpenTelemetry spans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Mapping

from .database import RuntimeDatabase


ALLOWED_TRACE_ATTRIBUTES = frozenset(
    {
        "protocol.version",
        "frame.message_type",
        "frame.payload_bytes",
        "frame.queue_wait_ms",
        "runtime.state",
        "error.code",
        "ssh.connection_id",
        "ssh.connect.attempt",
        "ssh.connect.outcome",
        "db.operation",
        "db.duration_ms",
    }
)


class ForbiddenTraceAttribute(ValueError):
    """Raised when a span attempts to persist non-allowlisted content."""


@dataclass(frozen=True, slots=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: str
    ended_at: str | None
    status: Literal["UNSET", "OK", "ERROR"]
    attributes: Mapping[str, str | int | float | bool]


class LocalTraceStore:
    def __init__(self, database: RuntimeDatabase) -> None:
        self._database = database

    def write(self, span: SpanRecord) -> None:
        attributes = _validate_attributes(span.attributes)
        attributes_json = json.dumps(
            attributes,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self._database.execute(
            """
            INSERT INTO trace_spans(
                span_id, trace_id, parent_span_id, name, started_at,
                ended_at, status, attributes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span.span_id,
                span.trace_id,
                span.parent_span_id,
                span.name,
                span.started_at,
                span.ended_at,
                span.status,
                attributes_json,
            ),
        )


def _validate_attributes(
    attributes: Mapping[str, str | int | float | bool],
) -> dict[str, str | int | float | bool]:
    validated: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_TRACE_ATTRIBUTES:
            raise ForbiddenTraceAttribute(f"trace attribute is forbidden: {key}")
        if not isinstance(value, (str, int, float, bool)):
            raise ForbiddenTraceAttribute(
                f"trace attribute has an unsupported value: {key}"
            )
        if isinstance(value, str) and len(value.encode("utf-8")) > 512:
            raise ForbiddenTraceAttribute(
                f"trace attribute exceeds 512 UTF-8 bytes: {key}"
            )
        validated[key] = value
    return validated
