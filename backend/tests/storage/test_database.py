from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, StorageSelfCheckFailed


def test_plaintext_database_bootstraps_only_schema_v6(tmp_path: Path) -> None:
    database = RuntimeDatabase.open_plaintext((tmp_path / "runtime.sqlite3").resolve())
    try:
        assert database.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(6,)]
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "runtime_records" in tables
        assert "audit_entries" not in tables
        assert "trace_spans" not in tables
        assert "artifact_metadata" not in tables
    finally:
        database.close()


def test_plaintext_database_requires_absolute_path() -> None:
    with pytest.raises(StorageSelfCheckFailed, match="absolute"):
        RuntimeDatabase.open_plaintext(Path("runtime.sqlite3"))


def test_plaintext_database_rejects_old_schema_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES (4, 'old')")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(StorageSelfCheckFailed, match="incompatible schema"):
        RuntimeDatabase.open_plaintext(path)

    assert path.read_bytes() == before


def test_plaintext_database_rejects_tampered_v6_shape(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    database = RuntimeDatabase.open_plaintext(path)
    database.execute("DROP TABLE runtime_records")
    database.close()

    with pytest.raises(StorageSelfCheckFailed, match="tables do not match"):
        RuntimeDatabase.open_plaintext(path)


def test_plaintext_database_close_is_idempotent(tmp_path: Path) -> None:
    database = RuntimeDatabase.open_plaintext((tmp_path / "runtime.sqlite3").resolve())
    database.close()
    database.close()

    with pytest.raises(StorageSelfCheckFailed, match="closed"):
        database.execute("SELECT 1")
