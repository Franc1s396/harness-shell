from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, StorageSelfCheckFailed


def test_open_plaintext_bootstraps_only_schema_v7(tmp_path: Path) -> None:
    database = RuntimeDatabase.open_plaintext(tmp_path / "runtime.sqlite3")
    try:
        assert database.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(7,)]
    finally:
        database.close()


@pytest.mark.parametrize("version", [4, 6])
def test_open_plaintext_rejects_old_schema_without_modifying_it(
    tmp_path: Path, version: int,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO schema_migrations VALUES (?, 'old')", (version,))
    old.commit()
    old.close()
    before = path.read_bytes()

    with pytest.raises(StorageSelfCheckFailed, match="incompatible schema"):
        RuntimeDatabase.open_plaintext(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("removed", [" ON DELETE CASCADE", " CHECK (revision > 0)",
                                    " DEFAULT 128000"])
def test_context_schema_drift_is_rejected_before_writes(tmp_path: Path, removed: str) -> None:
    import harness_shell_sidecar.storage.database as database_module
    sql = (Path(database_module.__file__).parent / "migrations" / "007_context_runtime.sql").read_text(encoding="utf-8")
    assert removed in sql
    path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(sql.replace(removed, ""))
    connection.close()
    before = path.read_bytes()
    with pytest.raises(StorageSelfCheckFailed):
        RuntimeDatabase.open_plaintext(path)
    assert path.read_bytes() == before
