"""Fail-closed SQLite bootstrap and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1
REQUIRED_TABLES = frozenset(
    {"schema_migrations", "encrypted_records", "audit_entries", "trace_spans"}
)


class StorageSelfCheckFailed(RuntimeError):
    """Raised when persisted state cannot be trusted."""


class RuntimeDatabase:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> RuntimeDatabase:
        if not path.is_absolute():
            raise StorageSelfCheckFailed("runtime database path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
        database = cls(path, connection)
        try:
            database._configure()
            database._migrate()
            database._self_check()
        except Exception:
            connection.close()
            raise
        return database

    def execute(
        self, statement: str, parameters: tuple | dict = ()
    ) -> sqlite3.Cursor:
        if self._closed:
            raise StorageSelfCheckFailed("runtime database is closed")
        return self.connection.execute(statement, parameters)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.connection.close()
            self._closed = True

    def _configure(self) -> None:
        journal_mode = self.connection.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0]
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")

        if str(journal_mode).lower() != "wal":
            raise StorageSelfCheckFailed("SQLite WAL mode is unavailable")
        if self.connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise StorageSelfCheckFailed("SQLite synchronous FULL is unavailable")
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StorageSelfCheckFailed("SQLite foreign keys are unavailable")
        if self.connection.execute("PRAGMA busy_timeout").fetchone()[0] != 5_000:
            raise StorageSelfCheckFailed("SQLite busy timeout is unavailable")

    def _migrate(self) -> None:
        tables = self._table_names()
        if "schema_migrations" not in tables:
            if tables:
                raise StorageSelfCheckFailed(
                    "runtime database contains an unversioned schema"
                )
            migration = (
                Path(__file__).parent / "migrations" / "001_m1.sql"
            ).read_text(encoding="utf-8")
            try:
                self.connection.executescript(migration)
            except sqlite3.DatabaseError as exc:
                raise StorageSelfCheckFailed("schema v1 migration failed") from exc

        versions = [
            row[0]
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        if versions != [SCHEMA_VERSION]:
            raise StorageSelfCheckFailed(
                f"unsupported schema versions: {versions!r}"
            )

    def _self_check(self) -> None:
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise StorageSelfCheckFailed("SQLite integrity check failed")
        missing = REQUIRED_TABLES - self._table_names()
        if missing:
            raise StorageSelfCheckFailed(
                f"schema v1 is missing required tables: {sorted(missing)!r}"
            )
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(encrypted_records)"
            ).fetchall()
        }
        required_columns = {
            "record_type",
            "record_id",
            "schema_version",
            "nonce",
            "ciphertext",
            "created_at",
            "updated_at",
        }
        if columns != required_columns:
            raise StorageSelfCheckFailed(
                "encrypted_records schema does not match version 1"
            )

    def _table_names(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

