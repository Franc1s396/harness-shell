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
            "connection_profiles",
            "host_keys",
            "artifact_metadata",
            "model_api_configs",
            "agent_conversations",
            "agent_runs",
            "agent_messages",
        } <= tables
        assert database.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
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


def test_database_upgrades_existing_v1_without_losing_records(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path, isolation_level=None)
    migration = (
        Path(__file__).parents[2]
        / "src"
        / "harness_shell_sidecar"
        / "storage"
        / "migrations"
        / "001_m1.sql"
    ).read_text(encoding="utf-8")
    connection.executescript(migration)
    connection.execute(
        """
        INSERT INTO encrypted_records(
            record_type, record_id, schema_version, nonce, ciphertext,
            created_at, updated_at
        ) VALUES ('checkpoint', 'existing', 1, zeroblob(12), X'01', 'now', 'now')
        """
    )
    connection.close()

    database = RuntimeDatabase.open(path)
    try:
        assert database.execute(
            "SELECT record_id FROM encrypted_records"
        ).fetchall() == [("existing",)]
        assert database.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
    finally:
        database.close()


def test_database_upgrades_existing_v2_with_profile_version(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path, isolation_level=None)
    migrations = (
        Path(__file__).parents[2]
        / "src"
        / "harness_shell_sidecar"
        / "storage"
        / "migrations"
    )
    connection.executescript(
        (migrations / "001_m1.sql").read_text(encoding="utf-8")
    )
    connection.executescript(
        (migrations / "002_m2.sql").read_text(encoding="utf-8")
    )
    connection.execute(
        """
        INSERT INTO connection_profiles(
            connection_id, display_name, group_name, host, port, username,
            auth_kind, credential_id, passphrase_credential_id,
            proxy_jump_id, favorite, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, 22, ?, 'password', ?, NULL, NULL, 0, ?, ?)
        """,
        (
            "00000000-0000-4000-8000-000000000001",
            "existing",
            "existing.example",
            "deploy",
            "00000000-0000-4000-8000-000000000002",
            "2026-08-29T00:00:00.000000Z",
            "2026-08-29T00:00:00.000000Z",
        ),
    )
    connection.close()

    database = RuntimeDatabase.open(path)
    try:
        assert database.execute(
            "SELECT version, display_name FROM connection_profiles"
        ).fetchone() == (1, "existing")
        assert database.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
    finally:
        database.close()


def test_database_upgrades_existing_v3_without_losing_profiles(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    connection = sqlite3.connect(path, isolation_level=None)
    migrations = (
        Path(__file__).parents[2]
        / "src"
        / "harness_shell_sidecar"
        / "storage"
        / "migrations"
    )
    for migration_name in (
        "001_m1.sql",
        "002_m2.sql",
        "003_connection_profile_version.sql",
    ):
        connection.executescript(
            (migrations / migration_name).read_text(encoding="utf-8")
        )
    connection.execute(
        """
        INSERT INTO connection_profiles(
            connection_id, display_name, group_name, host, port, username,
            auth_kind, credential_id, passphrase_credential_id,
            proxy_jump_id, favorite, created_at, updated_at, version
        ) VALUES (?, ?, NULL, ?, 22, ?, 'password', ?, NULL, NULL, 0, ?, ?, 7)
        """,
        (
            "00000000-0000-4000-8000-000000000001",
            "existing-v3",
            "existing.example",
            "deploy",
            "00000000-0000-4000-8000-000000000002",
            "2026-08-30T00:00:00.000000Z",
            "2026-08-30T00:00:00.000000Z",
        ),
    )
    connection.close()

    database = RuntimeDatabase.open(path)
    try:
        assert database.execute(
            "SELECT version, display_name FROM connection_profiles"
        ).fetchone() == (7, "existing-v3")
        assert database.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
    finally:
        database.close()


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        ("model_api_configs", ") STRICT", ")"),
        (
            "model_api_configs",
            "CHECK(length(display_name) BETWEEN 1 AND 80)",
            "",
        ),
        (
            "model_api_configs",
            "CHECK(length(display_name) BETWEEN 1 AND 80)",
            "CHECK(length(display_name) BETWEEN 1 AND 81)",
        ),
        (
            "model_api_configs",
            "CHECK(api_type IN ('CHAT_COMPLETIONS', 'RESPONSES'))",
            "",
        ),
        (
            "model_api_configs",
            "CHECK(length(base_url) BETWEEN 1 AND 2048)",
            "",
        ),
        (
            "model_api_configs",
            "CHECK(length(base_url) BETWEEN 1 AND 2048)",
            "CHECK(length(base_url) BETWEEN 1 AND 2049)",
        ),
        (
            "model_api_configs",
            "CHECK(length(model) BETWEEN 1 AND 255)",
            "",
        ),
        (
            "model_api_configs",
            "CHECK(length(model) BETWEEN 1 AND 255)",
            "CHECK(length(model) BETWEEN 1 AND 256)",
        ),
        (
            "model_api_configs",
            "CHECK(enabled IN (0, 1))",
            "",
        ),
        (
            "agent_runs",
            "CHECK(status IN ('RUNNING','COMPLETED','FAILED','LIMIT_REACHED','CANCELLED'))",
            "",
        ),
        (
            "agent_runs",
            "CHECK(react_iteration BETWEEN 0 AND 128)",
            "",
        ),
        (
            "agent_runs",
            "CHECK(react_iteration BETWEEN 0 AND 128)",
            "CHECK(react_iteration BETWEEN 0 AND 129)",
        ),
        (
            "agent_runs",
            "CHECK(react_iteration BETWEEN 0 AND 128)",
            "CHECK(react_iteration BETWEEN -1 AND 128)",
        ),
        (
            "agent_messages",
            "CHECK(sequence > 0)",
            "",
        ),
        (
            "agent_messages",
            "CHECK(message_type IN ('SYSTEM','HUMAN','AI','TOOL'))",
            "",
        ),
    ],
    ids=[
        "strict-table",
        "config-display-name-length",
        "config-display-name-max-length",
        "config-api-type",
        "config-base-url-length",
        "config-base-url-max-length",
        "config-model-length",
        "config-model-max-length",
        "config-enabled",
        "run-status-check",
        "run-iteration-check",
        "run-iteration-max",
        "run-iteration-min",
        "message-sequence-check",
        "message-type-check",
    ],
)
def test_database_rejects_weakened_agent_schema(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    """Reject a schema-v4 database whose declared constraints were weakened."""

    path = (tmp_path / "runtime.sqlite3").resolve()
    database = RuntimeDatabase.open(path)
    database.close()
    connection = sqlite3.connect(path)
    sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()[0]
    assert old in sql
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_schema SET sql = ? WHERE type = 'table' AND name = ?",
        (sql.replace(old, new, 1), table),
    )
    version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute(f"PRAGMA schema_version = {version + 1}")
    connection.commit()
    connection.close()

    with pytest.raises(StorageSelfCheckFailed, match="schema.*version 4"):
        RuntimeDatabase.open(path)
