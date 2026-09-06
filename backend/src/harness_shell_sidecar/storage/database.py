"""严格失败的 SQLite 启动与 schema 管理。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


PLAINTEXT_SCHEMA_VERSION = 7
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
        "agent_context_summaries",
    }
)


class StorageSelfCheckFailed(RuntimeError):
    """持久化状态不可信时抛出。"""


class RuntimeDatabase:
    """负责 SQLite 建库、安全配置、自检与关闭。"""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        """封装已打开但尚需由 open 完成配置和验证的 SQLite 连接。"""

        self.path = path  # 当前运行实例数据库文件的绝对路径。
        self.connection = connection  # 供同一运行时各仓储共享的底层连接。
        self._closed = False  # 防止关闭后继续执行 SQL 或重复 checkpoint。

    @classmethod
    def open_plaintext(cls, path: Path) -> RuntimeDatabase:
        """打开 schema v7 数据库，拒绝其他版本。"""

        if not path.is_absolute():
            raise StorageSelfCheckFailed("runtime database path must be absolute")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        connection = sqlite3.connect(path.resolve(), isolation_level=None)
        database = cls(path.resolve(), connection)
        try:
            if existed:
                database._require_exact_schema_version(PLAINTEXT_SCHEMA_VERSION)
                database._self_check_plaintext_v7()
                database._configure()
            else:
                database._configure()
                migration = (
                    Path(__file__).parent
                    / "migrations"
                    / "007_context_runtime.sql"
                ).read_text(encoding="utf-8")
                connection.executescript(migration)
                database._self_check_plaintext_v7()
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
        """为协作领域仓库管理一个立即事务。"""

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
        """验证 Agent 类型、约束、外键和索引。"""

        # 1. 定义目标表的精确列契约，逐表比较严格类型和结构。
        expected_columns = {
            "agent_context_summaries": {
                "conversation_id": ("TEXT", 1, 1),
                "revision": ("INTEGER", 1, 0),
                "covered_through_sequence": ("INTEGER", 1, 0),
                "summary_text": ("TEXT", 1, 0),
                "source_run_id": ("TEXT", 1, 0),
                "created_at": ("TEXT", 1, 0),
                "updated_at": ("TEXT", 1, 0),
            },
            "model_api_configs": {
                "context_window_size": ("INTEGER", 1, 0),
                "context_compaction_threshold_ratio": ("REAL", 1, 0),
                "max_output_tokens": ("INTEGER", 1, 0),
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
        # 2. 检查会话内序号与消息记录关联的唯一性索引。
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
        # 3. 比较外键目标与删除行为，防止孤立记录或错误级联。
        expected_foreign_keys = {
            "agent_context_summaries": {
                ("agent_conversations", "conversation_id", "conversation_id"),
                ("agent_runs", "source_run_id", "agent_run_id"),
            },
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
        """验证数据库仅记录目标 schema 版本。"""

        tables = self._table_names()
        if "schema_migrations" not in tables:
            raise StorageSelfCheckFailed("incompatible schema")
        try:
            versions = self._schema_versions()
        except sqlite3.DatabaseError as exc:
            raise StorageSelfCheckFailed("incompatible schema") from exc
        if versions != [expected_version]:
            raise StorageSelfCheckFailed("incompatible schema")

    def _self_check_plaintext_v7(self) -> None:
        """验证明文 schema v7 契约。"""

        # 1. 先验证 SQLite 完整性和精确表集合，拒绝多余或缺失对象。
        integrity = self.connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise StorageSelfCheckFailed("SQLite integrity check failed")
        tables = self._table_names()
        if tables != PLAINTEXT_REQUIRED_TABLES:
            raise StorageSelfCheckFailed("plaintext schema v7 tables do not match")
        # 2. 校验明文记录和凭据列，再核对连接版本字段及 Host Key 索引。
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
                    f"{table_name} schema does not match version 7"
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
                "connection_profiles version schema does not match version 7"
            )
        indexes = {
            row[1]
            for row in self.connection.execute("PRAGMA index_list(host_keys)").fetchall()
        }
        if "one_active_host_key_per_connection" not in indexes:
            raise StorageSelfCheckFailed(
                "schema v7 is missing the active host-key index"
            )
        # 3. 校验 Agent 表关系，并检查 v7 上下文预算的约束和默认值。
        self._check_agent_schema(
            api_key_column="api_key_credential_id",
            message_record_column="record_id",
            schema_version=7,
        )
        # 检查约束时不使用 INSERT 探测；被拒绝的已有数据库
        # 必须逐字节保持不变，包括 WAL。
        required_checks = {
            "model_api_configs": (
                "CHECK (context_window_size > 0 AND context_window_size <= 9007199254740991)",
                "CHECK (context_compaction_threshold_ratio > 0 AND context_compaction_threshold_ratio < 1)",
                "CHECK (max_output_tokens > 0 AND max_output_tokens < context_window_size)",
            ),
            "agent_context_summaries": (
                "CHECK (revision > 0)", "CHECK (covered_through_sequence > 0)",
                "CHECK (length(trim(summary_text)) > 0)",
            ),
        }
        for table, checks in required_checks.items():
            sql = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()[0]
            if any(" ".join(check.lower().split()) not in " ".join(sql.lower().split()) for check in checks):
                raise StorageSelfCheckFailed("context constraints do not match version 7")
        defaults = {row[1]: row[4] for row in self.connection.execute("PRAGMA table_info(model_api_configs)")}
        for field, expected in {"context_window_size": "128000",
                                "context_compaction_threshold_ratio": "0.75", "max_output_tokens": "8192"}.items():
            if defaults[field] != expected:
                raise StorageSelfCheckFailed("context defaults do not match version 7")
        # 4. 确认摘要随所属会话级联删除，保持单一权威历史关系。
        summary_foreign_keys = self.connection.execute("PRAGMA foreign_key_list(agent_context_summaries)").fetchall()
        if {row[3]: row[6] for row in summary_foreign_keys} != {
            "conversation_id": "CASCADE", "source_run_id": "NO ACTION",
        }:
            raise StorageSelfCheckFailed("summary deletion contract does not match version 7")

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
