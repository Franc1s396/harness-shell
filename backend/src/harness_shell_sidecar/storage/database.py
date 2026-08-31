"""Fail-closed SQLite bootstrap and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 4
REQUIRED_TABLES = frozenset(
    {
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

        versions = self._schema_versions()
        if (
            not versions
            or versions != list(range(1, versions[-1] + 1))
            or versions[-1] > SCHEMA_VERSION
        ):
            raise StorageSelfCheckFailed(
                f"unsupported schema versions: {versions!r}"
            )

        migration_names = {
            2: "002_m2.sql",
            3: "003_connection_profile_version.sql",
            4: "004_react_shell_agent.sql",
        }
        for next_version in range(versions[-1] + 1, SCHEMA_VERSION + 1):
            migration = (
                Path(__file__).parent
                / "migrations"
                / migration_names[next_version]
            ).read_text(encoding="utf-8")
            try:
                self.connection.executescript(migration)
            except sqlite3.DatabaseError as exc:
                raise StorageSelfCheckFailed(
                    f"schema v{next_version} migration failed"
                ) from exc
            versions = self._schema_versions()
            if versions != list(range(1, next_version + 1)):
                raise StorageSelfCheckFailed(
                    f"unsupported schema versions: {versions!r}"
                )
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
                "connection_profiles version schema does not match version 3"
            )
        invalid_version = self.connection.execute(
            """
            SELECT 1 FROM connection_profiles
            WHERE version < 1 OR version > 9007199254740991
            LIMIT 1
            """
        ).fetchone()
        if invalid_version is not None:
            raise StorageSelfCheckFailed(
                "connection_profiles contains an invalid version"
            )
        self._check_agent_schema()

    def _check_agent_schema(self) -> None:
        """Verify schema-v4 Agent types, constraints, foreign keys, and indexes."""

        expected_columns = {
            "model_api_configs": {
                "api_config_id": ("TEXT", 1, 1),
                "display_name": ("TEXT", 1, 0),
                "api_type": ("TEXT", 1, 0),
                "base_url": ("TEXT", 1, 0),
                "model": ("TEXT", 1, 0),
                "api_key_secret_ref": ("TEXT", 1, 0),
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
                "encrypted_record_id": ("TEXT", 1, 0),
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
                    f"{table_name} schema does not match version 4"
                )
            actual = {
                row[1]: (str(row[2]).upper(), row[3], row[5])
                for row in self.connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if actual != required:
                raise StorageSelfCheckFailed(
                    f"{table_name} schema does not match version 4"
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
            ("encrypted_record_id",),
            ("conversation_id", "sequence"),
        } <= unique_indexes:
            raise StorageSelfCheckFailed(
                "agent_messages uniqueness schema does not match version 4"
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
                    f"{table_name} schema does not match version 4"
                )
        self._probe_agent_checks()

    def _probe_agent_checks(self) -> None:
        """Use a rolled-back write probe to prove critical CHECK constraints execute."""

        connection = self.connection
        conversation_id = str(uuid4())
        config_id = str(uuid4())
        valid_run_id = str(uuid4())
        invalid_status_run_id = str(uuid4())
        invalid_iteration_run_id = str(uuid4())
        invalid_negative_iteration_run_id = str(uuid4())
        connection.execute("SAVEPOINT agent_schema_probe")
        try:
            connection.execute(
                "INSERT INTO agent_conversations VALUES (?, 'now', 'now')",
                (conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO model_api_configs VALUES (
                    ?, 'probe', 'RESPONSES', 'https://example.invalid/', 'model',
                    '00000000-0000-4000-8000-00000000a003', 1, 'now', 'now'
                )
                """,
                (config_id,),
            )
            for invalid_column, invalid_value in (
                ("display_name", ""),
                ("display_name", "x" * 81),
                ("api_type", "INVALID"),
                ("base_url", ""),
                ("base_url", "x" * 2049),
                ("model", ""),
                ("model", "x" * 256),
                ("enabled", 2),
            ):
                values: dict[str, object] = {
                    "api_config_id": str(uuid4()),
                    "display_name": "probe",
                    "api_type": "RESPONSES",
                    "base_url": "https://example.invalid/",
                    "model": "model",
                    "api_key_secret_ref": str(uuid4()),
                    "enabled": 1,
                }
                values[invalid_column] = invalid_value
                self._expect_agent_constraint_failure(
                    """
                    INSERT INTO model_api_configs VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 'now', 'now'
                    )
                    """,
                    (
                        values["api_config_id"],
                        values["display_name"],
                        values["api_type"],
                        values["base_url"],
                        values["model"],
                        values["api_key_secret_ref"],
                        values["enabled"],
                    ),
                )
            self._expect_agent_constraint_failure(
                """
                INSERT INTO agent_runs VALUES (
                    ?, ?,
                    '00000000-0000-4000-8000-00000000a005', ?,
                    'INVALID', 0, NULL, 'now', NULL
                )
                """,
                (invalid_status_run_id, conversation_id, config_id),
            )
            self._expect_agent_constraint_failure(
                """
                INSERT INTO agent_runs VALUES (
                    ?, ?,
                    '00000000-0000-4000-8000-00000000a007', ?,
                    'RUNNING', 129, NULL, 'now', NULL
                )
                """,
                (invalid_iteration_run_id, conversation_id, config_id),
            )
            self._expect_agent_constraint_failure(
                """
                INSERT INTO agent_runs VALUES (
                    ?, ?,
                    '00000000-0000-4000-8000-00000000a008', ?,
                    'RUNNING', -1, NULL, 'now', NULL
                )
                """,
                (invalid_negative_iteration_run_id, conversation_id, config_id),
            )
            connection.execute(
                """
                INSERT INTO agent_runs VALUES (
                    ?, ?,
                    '00000000-0000-4000-8000-00000000a009', ?,
                    'RUNNING', 0, NULL, 'now', NULL
                )
                """,
                (valid_run_id, conversation_id, config_id),
            )
            self._expect_agent_constraint_failure(
                """
                INSERT INTO agent_messages VALUES (
                    ?, ?, 0, 'HUMAN', ?, NULL, ?, 'now'
                )
                """,
                (str(uuid4()), conversation_id, str(uuid4()), valid_run_id),
            )
            self._expect_agent_constraint_failure(
                """
                INSERT INTO agent_messages VALUES (
                    ?, ?, 1, 'INVALID', ?, NULL, ?, 'now'
                )
                """,
                (str(uuid4()), conversation_id, str(uuid4()), valid_run_id),
            )
        finally:
            connection.execute("ROLLBACK TO agent_schema_probe")
            connection.execute("RELEASE agent_schema_probe")

    def _expect_agent_constraint_failure(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> None:
        """Fail self-check when an invalid Agent row is unexpectedly accepted."""

        try:
            self.connection.execute(statement, parameters)
        except sqlite3.IntegrityError:
            return
        raise StorageSelfCheckFailed("Agent schema does not match version 4")

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
