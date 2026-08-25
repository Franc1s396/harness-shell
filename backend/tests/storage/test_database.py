from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, StorageSelfCheckFailed


def test_database_applies_schema_and_required_pragmas(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    try:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        assert database.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert database.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert database.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert {
            "schema_migrations",
            "encrypted_records",
            "audit_entries",
            "trace_spans",
        } <= tables
        assert database.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]
    finally:
        database.close()


def test_database_rejects_relative_path() -> None:
    with pytest.raises(StorageSelfCheckFailed, match="absolute"):
        RuntimeDatabase.open(Path("runtime.sqlite3"))


def test_database_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (99, '2026-08-25T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(StorageSelfCheckFailed, match="unsupported schema"):
        RuntimeDatabase.open(path)


def test_database_rejects_partial_unversioned_schema(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orphaned_table (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(StorageSelfCheckFailed, match="unversioned"):
        RuntimeDatabase.open(path)

