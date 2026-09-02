"""Allowlisted local representation of OpenTelemetry spans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Mapping

from .database import RuntimeDatabase


ALLOWED_TRACE_ATTRIBUTES = frozenset(
    {
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
    """经过 allowlist 约束后可写入本地数据库的 Span 表示。"""

    #: 32 位十六进制 Trace 标识符。
    trace_id: str
    #: 16 位十六进制 Span 标识符。
    span_id: str
    #: 可选的父 Span 标识符。
    parent_span_id: str | None
    #: Span 操作名称。
    name: str
    #: Span 开始时刻的 UTC 文本。
    started_at: str
    #: Span 结束时刻；尚未结束时为空。
    ended_at: str | None
    #: OpenTelemetry 标准状态的本地枚举值。
    status: Literal["UNSET", "OK", "ERROR"]
    #: 仅允许预定义键和标量值的脱敏属性集合。
    attributes: Mapping[str, str | int | float | bool]


class LocalTraceStore:
    """把 allowlist 校验后的 Span 持久化到本地运行时数据库。"""

    def __init__(self, database: RuntimeDatabase) -> None:
        """绑定唯一允许写入 Span 的本地数据库。"""

        self._database = database  # trace_spans 表所属的运行时数据库。

    def write(self, span: SpanRecord) -> None:
        """验证属性并以规范 JSON 写入一条 Span 记录。"""

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
