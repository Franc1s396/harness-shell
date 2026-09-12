from __future__ import annotations

from ..storage_support import RepositoryClient, sql

import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, StorageSelfCheckFailed


def test_open_plaintext_bootstraps_alembic_baseline(tmp_path: Path) -> None:
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        assert sql(database,
            "SELECT version_num FROM alembic_version"
        ).fetchall() == [("0003_context_summary_history",)]
    finally:
        database.close()


@pytest.mark.parametrize("version", [4, 6, 7])
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

    with pytest.raises(StorageSelfCheckFailed, match="incompatible database identity"):
        RuntimeDatabase.open(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("removed", [" ON DELETE CASCADE", " CHECK (revision > 0)",
                                    " DEFAULT 128000"])
def test_context_schema_drift_is_rejected_before_writes(tmp_path: Path, removed: str) -> None:
    from contextlib import closing
    from harness_shell_sidecar.storage.migration_runner import upgrade_database

    source = tmp_path / "source.sqlite3"
    upgrade_database(source)
    with closing(sqlite3.connect(source)) as original:
        ddl = "\n".join(original.iterdump())
    assert removed in ddl
    path = tmp_path / "runtime.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(ddl.replace(removed, ""))
    before = path.read_bytes()
    with pytest.raises(StorageSelfCheckFailed):
        RuntimeDatabase.open(path)
    assert path.read_bytes() == before
