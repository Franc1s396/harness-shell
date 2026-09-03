from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, StorageSelfCheckFailed


def test_open_plaintext_bootstraps_only_schema_v6(tmp_path: Path) -> None:
    database = RuntimeDatabase.open_plaintext(tmp_path / "runtime.sqlite3")
    try:
        assert database.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(6,)]
    finally:
        database.close()


def test_open_plaintext_rejects_schema_v4_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO schema_migrations VALUES (4, 'old')")
    old.commit()
    old.close()
    before = path.read_bytes()

    with pytest.raises(StorageSelfCheckFailed, match="incompatible schema"):
        RuntimeDatabase.open_plaintext(path)

    assert path.read_bytes() == before
