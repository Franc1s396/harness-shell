from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import (
    ForbiddenTraceAttribute,
    LocalTraceStore,
    RuntimeDatabase,
    SpanRecord,
)


def span(**attributes: str | int | float | bool) -> SpanRecord:
    return SpanRecord(
        trace_id="0" * 31 + "1",
        span_id="0" * 15 + "1",
        parent_span_id=None,
        name="sidecar.initialize",
        started_at="2026-08-25T00:00:00.000Z",
        ended_at="2026-08-25T00:00:00.100Z",
        status="OK",
        attributes=attributes,
    )


def test_trace_store_persists_only_allowlisted_metadata(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    try:
        LocalTraceStore(database).write(
            span(**{"runtime.state": "READY", "db.duration_ms": 3.5})
        )
        row = database.execute(
            "SELECT name, status, attributes_json FROM trace_spans"
        ).fetchone()

        assert row[:2] == ("sidecar.initialize", "OK")
        assert json.loads(row[2]) == {
            "db.duration_ms": 3.5,
            "runtime.state": "READY",
        }
    finally:
        database.close()


@pytest.mark.parametrize("attribute", ("prompt", "custom.attribute"))
def test_trace_rejects_forbidden_or_unknown_attributes(
    tmp_path: Path, attribute: str
) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    try:
        with pytest.raises(ForbiddenTraceAttribute, match=attribute):
            LocalTraceStore(database).write(span(**{attribute: "secret"}))
    finally:
        database.close()


def test_trace_rejects_oversized_string_values(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    try:
        with pytest.raises(ForbiddenTraceAttribute, match="512"):
            LocalTraceStore(database).write(
                span(**{"error.code": "X" * 513})
            )
    finally:
        database.close()

