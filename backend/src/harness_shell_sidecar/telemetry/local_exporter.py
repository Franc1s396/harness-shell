"""OpenTelemetry exporter that writes only to local runtime SQLite."""

from __future__ import annotations

from datetime import datetime, timezone

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from harness_shell_sidecar.storage import LocalTraceStore, SpanRecord


class LocalSpanExporter(SpanExporter):
    def __init__(self, store: LocalTraceStore) -> None:
        self._store = store

    def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
        try:
            for span in spans:
                self._store.write(_to_record(span))
        except Exception:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def build_local_tracer_provider(store: LocalTraceStore) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(LocalSpanExporter(store)))
    return provider


def _to_record(span: ReadableSpan) -> SpanRecord:
    context = span.get_span_context()
    parent_span_id = None
    if span.parent is not None:
        parent_span_id = f"{span.parent.span_id:016x}"
    status = span.status.status_code.name
    return SpanRecord(
        trace_id=f"{context.trace_id:032x}",
        span_id=f"{context.span_id:016x}",
        parent_span_id=parent_span_id,
        name=span.name,
        started_at=_ns_timestamp(span.start_time),
        ended_at=_ns_timestamp(span.end_time) if span.end_time is not None else None,
        status=status,
        attributes=dict(span.attributes or {}),
    )


def _ns_timestamp(value: int | None) -> str:
    if value is None:
        raise ValueError("span timestamp is missing")
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")

