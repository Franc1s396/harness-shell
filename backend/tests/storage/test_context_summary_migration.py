"""真实旧 revision 的摘要迁移和失败回滚，不接触用户数据库。"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from harness_shell_sidecar.storage import migration_runner as runner
from .test_migration_transactions import snapshot


def _old_database(path: Path) -> tuple:
    """建立 0002 数据库及 revision=7 的旧摘要，返回迁移前完整摘要字段。"""
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            config = Config()
            config.set_main_option("script_location", str(runner.migration_resource_dir()))
            config.attributes["connection"] = connection
            command.upgrade(config, "0002_agent_retry")
            connection.exec_driver_sql("""INSERT INTO model_api_configs
                (api_config_id, display_name, api_type, base_url, model, api_key_credential_id, enabled, created_at, updated_at)
                VALUES ('config', 'test', 'CHAT_COMPLETIONS', 'https://example.test', 'test', 'key', 1, 'created', 'updated')""")
            connection.exec_driver_sql("INSERT INTO agent_conversations VALUES ('conversation', 'created', 'updated')")
            connection.exec_driver_sql("""INSERT INTO agent_runs
                (agent_run_id, conversation_id, ssh_session_id, api_config_id, status, react_iteration, started_at)
                VALUES ('run', 'conversation', 'ssh', 'config', 'COMPLETED', 0, 'created')""")
            original = ('conversation', 7, 20, '旧摘要保持原样', 'run', 'created', 'updated')
            connection.exec_driver_sql("INSERT INTO agent_context_summaries VALUES (?, ?, ?, ?, ?, ?, ?)", original)
            return original
    finally:
        engine.dispose()


def test_summary_history_upgrade_keeps_legacy_revision_and_allows_append(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    original = _old_database(path)
    runner.upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM agent_context_summaries").fetchall() == [original]
        connection.execute("INSERT INTO agent_context_summaries VALUES ('conversation', 8, 30, 'new', 'run', 'new', 'new')")
        assert connection.execute("SELECT revision FROM agent_context_summaries ORDER BY revision").fetchall() == [(7,), (8,)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO agent_context_summaries VALUES ('conversation', 8, 40, 'duplicate', 'run', 'new', 'new')")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert next(row[5] for row in connection.execute("PRAGMA table_list") if row[1] == "agent_context_summaries") == 1
    runner.upgrade_database(path)


def test_summary_history_upgrade_rolls_back_rebuild_and_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "old.sqlite3"
    _old_database(path)
    before = snapshot(path)
    real_check = runner.check_current_schema

    def reject_candidate(connection) -> None:
        """新表已完成且通过自检后注入提交前失败。"""
        real_check(connection)
        raise RuntimeError("injected post-rebuild failure")

    monkeypatch.setattr(runner, "check_current_schema", reject_candidate)
    with pytest.raises(RuntimeError, match="post-rebuild"):
        runner.upgrade_database(path)
    assert snapshot(path) == before
