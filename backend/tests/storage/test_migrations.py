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
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("0003_context_summary_history",)]
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


def test_retry_upgrade_preserves_existing_data(tmp_path: Path) -> None:
    """从真实 0001 revision 升级，不重建或丢弃旧正文。"""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from harness_shell_sidecar.storage.migration_runner import migration_resource_dir
    path = tmp_path / "old.sqlite3"
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            config = Config()
            config.set_main_option("script_location", str(migration_resource_dir()))
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial")
            connection.exec_driver_sql("INSERT INTO runtime_records VALUES ('test','old',1,X'6162','c','u')")
    finally:
        engine.dispose()
    upgrade(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT payload FROM runtime_records WHERE record_id='old'").fetchone() == (b"ab",)
        assert "user_message_id" in {row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
