"""使用独立 sqlite3 连接验证启动迁移的真实落盘行为。"""

import importlib
import sqlite3
from pathlib import Path

import pytest


def upgrade(path: Path) -> None:
    """把缺失实现转换为明确的功能缺失断言。"""
    try:
        runner = importlib.import_module("harness_shell_sidecar.storage.migration_runner")
    except ModuleNotFoundError:
        pytest.fail("Alembic startup migration is not implemented")
    runner.upgrade_database(path)


def test_initial_revision_creates_strict_business_tables(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    upgrade(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0001_initial",)]
        strict = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
        assert strict["runtime_records"] == 1
        assert strict["connection_profiles"] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    upgrade(path)


def test_legacy_database_is_rejected_without_changes(tmp_path: Path) -> None:
    from harness_shell_sidecar.storage import StorageSelfCheckFailed
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
        connection.execute("INSERT INTO schema_migrations VALUES (7)")
    before = path.read_bytes()
    with pytest.raises(StorageSelfCheckFailed):
        upgrade(path)
    assert path.read_bytes() == before
