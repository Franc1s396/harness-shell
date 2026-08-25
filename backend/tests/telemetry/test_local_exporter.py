from pathlib import Path

from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from harness_shell_sidecar.storage import LocalTraceStore, RuntimeDatabase
from harness_shell_sidecar.telemetry import (
    LocalSpanExporter,
    build_local_tracer_provider,
)


def test_local_exporter_writes_real_sdk_spans(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    provider = build_local_tracer_provider(LocalTraceStore(database))
    try:
        tracer = provider.get_tracer("harness-shell-test")
        with tracer.start_as_current_span("runtime.ready") as span:
            span.set_attribute("runtime.state", "READY")
        provider.force_flush()

        row = database.execute(
            "SELECT name, status, attributes_json FROM trace_spans"
        ).fetchone()
        assert row[0] == "runtime.ready"
        assert '"runtime.state":"READY"' in row[2]
    finally:
        provider.shutdown()
        database.close()


def test_provider_contains_only_the_local_exporter(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    provider = build_local_tracer_provider(LocalTraceStore(database))
    try:
        processors = provider._active_span_processor._span_processors

        assert len(processors) == 1
        assert isinstance(processors[0], SimpleSpanProcessor)
        assert isinstance(processors[0].span_exporter, LocalSpanExporter)
    finally:
        provider.shutdown()
        database.close()

