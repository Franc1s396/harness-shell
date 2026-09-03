"""Fail-closed SQLite bootstrap and schema management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


PLAINTEXT_SCHEMA_VERSION = 6
PLAINTEXT_REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "runtime_records",
        "connection_profiles",
        "host_keys",
        "model_api_configs",
        "agent_conversations",
        "agent_runs",
        "agent_messages",
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
    def open_plaintext(cls, path: Path) -> RuntimeDatabase:
        """Open a fresh schema-v6 database or reject every older schema."""

        if not path.is_absolute():
            raise StorageSelfCheckFailed("runtime database path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        connection = sqlite3.connect(path.resolve(), isolation_level=None)
        database = cls(path.resolve(), connection)
        try:
            if existed:
                database._require_exact_schema_version(PLAINTEXT_SCHEMA_VERSION)
                database._configure()
            else:
                database._configure()
                migration = (
                    Path(__file__).parent
                    / "migrations"
                    / "006_plaintext_runtime.sql"
                ).read_text(encoding="utf-8")
                connection.executescript(migration)
            database._self_check_plaintext_v6()
        except BaseException:
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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Own one immediate transaction across cooperating domain repositories."""

        self.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.execute("COMMIT")
        except BaseException as error:
            try:
                self.execute("ROLLBACK")
            except BaseException as rollback_error:
                error.add_note(f"SQLite rollback failed: {rollback_error!r}")
            raise

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

    def _check_agent_schema(
        self,
        api_key_column: str,
        message_record_column: str,
        schema_version: int,
    ) -> None:
        """Verify Agent types, constraints, foreign keys, and indexes."""

        expected_columns = {
            "model_api_configs": {
                "api_config_id": ("TEXT", 1, 1),
                "display_name": ("TEXT", 1, 0),
                "api_type": ("TEXT", 1, 0),
                "base_url": ("TEXT", 1, 0),
                "model": ("TEXT", 1, 0),
                api_key_column: ("TEXT", 1, 0),
                "enabled": ("INTEGER", 1, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "agent_conversations": {
                "conversation_id": ("TEXT", 1, 1),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "agent_runs": {
                "agent_run_id": ("TEXT", 1, 1),
                "conversation_id": ("TEXT", 1, 0),
                "ssh_session_id": ("TEXT", 1, 0),
                "api_config_id": ("TEXT", 1, 0),
                "status": ("TEXT", 1, 0),
                "react_iteration": ("INTEGER", 1, 0),
                "error_code": ("TEXT", 0, 0),
                "started_at": ("TEXT", 1, 0),
                "ended_at": ("TEXT", 0, 0),
            },
            "agent_messages": {
                "message_id": ("TEXT", 1, 1),
                "conversation_id": ("TEXT", 1, 0),
                "sequence": ("INTEGER", 1, 0),
                "message_type": ("TEXT", 1, 0),
                message_record_column: ("TEXT", 1, 0),
                "tool_call_id": ("TEXT", 0, 0),
                "agent_run_id": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
            },
        }
        table_metadata = {
            row[1]: row
            for row in self.connection.execute("PRAGMA table_list").fetchall()
            if row[0] == "main"
        }
        for table_name, required in expected_columns.items():
            metadata = table_metadata.get(table_name)
            if metadata is None or metadata[5] != 1:
                raise StorageSelfCheckFailed(
                    f"{table_name} schema does not match version {schema_version}"
                )
            actual = {
                row[1]: (str(row[2]).upper(), row[3], row[5])
                for row in self.connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if actual != required:
                raise StorageSelfCheckFailed(
                    f"{table_name} schema does not match version {schema_version}"
                )
        unique_indexes = {
            tuple(
                column[2]
                for column in self.connection.execute(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            )
            for index in self.connection.execute(
                "PRAGMA index_list(agent_messages)"
            ).fetchall()
            if index[2] == 1
        }
        if not {
            (message_record_column,),
            ("conversation_id", "sequence"),
        } <= unique_indexes:
            raise StorageSelfCheckFailed(
                "agent_messages uniqueness schema does not match "
                f"version {schema_version}"
            )
        expected_foreign_keys = {
            "agent_runs": {
                ("agent_conversations", "conversation_id", "conversation_id"),
                ("model_api_configs", "api_config_id", "api_config_id"),
            },
            "agent_messages": {
                ("agent_conversations", "conversation_id", "conversation_id"),
                ("agent_runs", "agent_run_id", "agent_run_id"),
            },
        }
        for table_name, expected in expected_foreign_keys.items():
            actual = {
                (row[2], row[3], row[4])
                for row in self.connection.execute(
                    f"PRAGMA foreign_key_list({table_name})"
                ).fetchall()
            }
            if actual != expected:
                raise StorageSelfCheckFailed(
                    f"{table_name} schema does not match version {schema_version}"
                )

    def _require_exact_schema_version(self, expected_version: int) -> None:
        """Reject an existing database unless it contains only the target version."""

        tables = self._table_names()
        if "schema_migrations" not in tables:
            raise StorageSelfCheckFailed("incompatible schema")
        try:
            versions = self._schema_versions()
        except sqlite3.DatabaseError as exc:
            raise StorageSelfCheckFailed("incompatible schema") from exc
        if versions != [expected_version]:
            raise StorageSelfCheckFailed("incompatible schema")

    def _self_check_plaintext_v6(self) -> None:
        """Verify the complete fresh-only plaintext schema-v6 contract."""

        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise StorageSelfCheckFailed("SQLite integrity check failed")
        tables = self._table_names()
        if tables != PLAINTEXT_REQUIRED_TABLES:
            raise StorageSelfCheckFailed("plaintext schema v6 tables do not match")
        expected_columns = {
            "runtime_records": {
                "record_type",
                "record_id",
                "schema_version",
                "payload",
                "created_at",
                "updated_at",
            },
        }
        for table_name, expected in expected_columns.items():
            actual = {
                row[1]
                for row in self.connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if actual != expected:
                raise StorageSelfCheckFailed(
                    f"{table_name} schema does not match version 6"
                )
        profile_columns = {
            row[1]: row
            for row in self.connection.execute(
                "PRAGMA table_info(connection_profiles)"
            ).fetchall()
        }
        version_column = profile_columns.get("version")
        if (
            version_column is None
            or str(version_column[2]).upper() != "INTEGER"
            or version_column[3] != 1
            or str(version_column[4]) != "1"
        ):
            raise StorageSelfCheckFailed(
                "connection_profiles version schema does not match version 6"
            )
        indexes = {
            row[1]
            for row in self.connection.execute("PRAGMA index_list(host_keys)").fetchall()
        }
        if "one_active_host_key_per_connection" not in indexes:
            raise StorageSelfCheckFailed(
                "schema v6 is missing the active host-key index"
            )
        self._check_agent_schema(
            api_key_column="api_key_credential_id",
            message_record_column="record_id",
            schema_version=6,
        )

    def _schema_versions(self) -> list[int]:
        """按升序返回已经持久化的全部连续 schema 版本。"""

        return [
            row[0]
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    def _table_names(self) -> set[str]:
        """返回全部非 SQLite 内部表名。"""

        return {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
