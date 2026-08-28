"""Fail-closed SQLite bootstrap and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 2
REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "encrypted_records",
        "audit_entries",
        "trace_spans",
        "connection_profiles",
        "host_keys",
        "artifact_metadata",
    }
)


class StorageSelfCheckFailed(RuntimeError):
    """Raised when persisted state cannot be trusted."""


class RuntimeDatabase:
    """负责 SQLite 安全配置、顺序迁移、自检与关闭的运行时数据库。"""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        """封装已打开但尚需由 open 完成配置和验证的 SQLite 连接。"""

        self.path = path  # 当前运行实例数据库文件的绝对路径。
        self.connection = connection  # 供同一运行时各仓储共享的底层连接。
        self._closed = False  # 防止关闭后继续执行 SQL 或重复 checkpoint。

    @classmethod
    def open(cls, path: Path) -> RuntimeDatabase:
        """创建数据库、执行全部迁移和自检，任一步失败即关闭连接。"""

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
        """在未关闭的共享连接上执行一条参数化 SQL。"""

        if self._closed:
            raise StorageSelfCheckFailed("runtime database is closed")
        return self.connection.execute(statement, parameters)

    def close(self) -> None:
        """截断 WAL 后关闭连接；该操作可安全重复调用。"""

        if self._closed:
            return
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.connection.close()
            self._closed = True

    def _configure(self) -> None:
        """启用并验证 WAL、FULL 同步、外键和 busy timeout 安全配置。"""

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
        """仅从空库或受支持的连续版本执行内置顺序迁移。"""

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
        if versions == [1]:
            migration = (
                Path(__file__).parent / "migrations" / "002_m2.sql"
            ).read_text(encoding="utf-8")
            try:
                self.connection.executescript(migration)
            except sqlite3.DatabaseError as exc:
                raise StorageSelfCheckFailed("schema v2 migration failed") from exc
            versions = [
                row[0]
                for row in self.connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        if versions != list(range(1, SCHEMA_VERSION + 1)):
            raise StorageSelfCheckFailed(
                f"unsupported schema versions: {versions!r}"
            )

    def _self_check(self) -> None:
        """验证 SQLite 完整性、必需表、关键列和唯一索引。"""

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
                "encrypted_records schema does not match version 2"
            )
        indexes = {
            row[1]
            for row in self.connection.execute("PRAGMA index_list(host_keys)").fetchall()
        }
        if "one_active_host_key_per_connection" not in indexes:
            raise StorageSelfCheckFailed(
                "schema v2 is missing the active host-key index"
            )

    def _table_names(self) -> set[str]:
        """返回全部非 SQLite 内部表名。"""

        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
