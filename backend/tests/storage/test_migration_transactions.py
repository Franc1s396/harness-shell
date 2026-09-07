"""跨 revision 的 DDL、数据与版本更新必须全部原子回滚。"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from harness_shell_sidecar.storage import StorageSelfCheckFailed
from harness_shell_sidecar.storage import migration_runner as runner


def snapshot(path: Path) -> tuple:
    """独立读出完整 schema、业务数据和版本，不复用受测 ORM。"""
    with sqlite3.connect(path) as connection:
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
        rows = [(name, connection.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall())
                for kind, name, sql in schema if kind == "table"]
        return schema, rows


def add_revision(directory: Path, revision: str, parent: str, body: str) -> None:
    """生成仅用于隔离数据库的受控迁移。"""
    code = (f"from alembic import op\nimport sqlalchemy as sa\nrevision={revision!r}\n"
            f"down_revision={parent!r}\nbranch_labels=None\ndepends_on=None\n"
            "def upgrade():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n")
    (directory / "versions" / f"{revision}.py").write_text(code, encoding="utf-8")


@pytest.mark.parametrize("failure", ["ddl", "batch", "validation"])
def test_entire_upgrade_chain_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO runtime_records VALUES ('test','one',1,?, 'created','updated')", (b"original",))
    before = snapshot(path)
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    add_revision(directory, "test_second", "0001_initial",
                 "op.execute(\"UPDATE runtime_records SET payload=X'6368616E676564'\")")
    body = 'op.create_table("migration_probe", sa.Column("id", sa.Integer, primary_key=True))'
    if failure == "batch":
        body = ('with op.batch_alter_table("runtime_records", recreate="always", table_kwargs={"sqlite_strict": True}) as batch:\n'
                '    batch.add_column(sa.Column("probe", sa.Text))')
    if failure != "validation":
        body += '\nraise RuntimeError("injected failure")'
    add_revision(directory, "test_third", "test_second", body)
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    expected = StorageSelfCheckFailed if failure == "validation" else RuntimeError
    with pytest.raises(expected):
        runner.upgrade_database(path)
    assert snapshot(path) == before


@pytest.mark.parametrize("change", [
    "UPDATE alembic_version SET version_num='unknown'",
    "INSERT INTO alembic_version VALUES ('another')",
    "CREATE TABLE extra(value TEXT)",
    "DROP INDEX one_active_host_key_per_connection",
])
def test_invalid_existing_database_is_not_modified(tmp_path: Path, change: str) -> None:
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(change)
    before = snapshot(path)
    with pytest.raises(StorageSelfCheckFailed):
        runner.upgrade_database(path)
    assert snapshot(path) == before



def test_initial_failure_leaves_retryable_empty_database(tmp_path: Path, monkeypatch) -> None:
    """首次 DDL 后失败只留下空文件；修复原因后可以正常建立基线。"""
    path = tmp_path / "runtime.sqlite3"
    original = runner.check_current_schema
    def fail_validation(connection):
        """在版本写入之后注入提交前失败。"""
        raise RuntimeError("injected final validation")
    monkeypatch.setattr(runner, "check_current_schema", fail_validation)
    with pytest.raises(RuntimeError, match="injected"):
        runner.upgrade_database(path)
    assert snapshot(path) == ([], [])
    monkeypatch.setattr(runner, "check_current_schema", original)
    runner.upgrade_database(path)
    assert snapshot(path)[0]


def test_successful_batch_preserves_strict_and_rows(tmp_path: Path, monkeypatch) -> None:
    """成功重建当前形状后，在同一事务完成外键检查与版本推进。"""
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO runtime_records VALUES ('test','one',1,X'61','c','u')")
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    add_revision(directory, "test_batch", "0001_initial",
                 'with op.batch_alter_table("runtime_records", recreate="always", table_args=[sa.CheckConstraint("schema_version > 0")], table_kwargs={"sqlite_strict": True}) as batch:\n    pass')
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    runner.upgrade_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("test_batch",)]
        assert connection.execute("SELECT payload FROM runtime_records").fetchall() == [(b"a",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_foreign_key_validation_rolls_back_data_and_revision(tmp_path: Path, monkeypatch) -> None:
    """迁移期间允许重建表，但最终孤立引用必须阻止整个升级提交。"""
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    before = snapshot(path)
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    add_revision(directory, "test_orphan", "0001_initial",
                 "op.execute(\"INSERT INTO host_keys VALUES ('h','missing','ssh-ed25519','fingerprint',X'61','active','now',NULL)\")")
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    with pytest.raises(StorageSelfCheckFailed, match="foreign key"):
        runner.upgrade_database(path)
    assert snapshot(path) == before


def test_migration_write_lock_has_bounded_timeout(tmp_path: Path) -> None:
    """另一个写连接持锁时按固定忙等待上限失败，不无界阻塞。"""
    import time
    from sqlalchemy.exc import OperationalError
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(OperationalError, match="locked"):
            runner.upgrade_database(path)
        assert 4 <= time.monotonic() - start < 10
    finally:
        connection.rollback()
        connection.close()



def test_failure_during_batch_copy_rolls_back_temporary_table(tmp_path, monkeypatch):
    """重建临时表已创建但数据复制失败时，不留下临时表或版本推进。"""
    from sqlalchemy import Engine, event
    path = tmp_path / "runtime.sqlite3"
    runner.upgrade_database(path)
    before = snapshot(path)
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    add_revision(directory, "test_copy", "0001_initial",
                 'with op.batch_alter_table("runtime_records", recreate="always", table_args=[sa.CheckConstraint("schema_version > 0")], table_kwargs={"sqlite_strict": True}) as batch:\n    pass')
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    observed = []
    def interrupt_copy(connection, cursor, statement, parameters, context, many):
        """只在重建表复制阶段制造失败，记录真实 BEGIN 顺序。"""
        observed.append(statement)
        if statement.startswith("INSERT INTO _alembic_tmp_runtime_records"):
            raise RuntimeError("injected batch copy failure")
    event.listen(Engine, "before_cursor_execute", interrupt_copy)
    try:
        with pytest.raises(RuntimeError, match="batch copy failure"):
            runner.upgrade_database(path)
    finally:
        event.remove(Engine, "before_cursor_execute", interrupt_copy)
    assert observed[0] == "BEGIN IMMEDIATE"
    assert snapshot(path) == before



@pytest.mark.parametrize("fail_after_rebuild", [False, True])
def test_batch_preserves_foreign_key_and_partial_index(tmp_path, monkeypatch, fail_after_rebuild):
    """真实 Host Key 表重建保留 FK、部分索引与数据，后续失败可整批回滚。"""
    from harness_shell_sidecar.storage import RuntimeDatabase
    from harness_shell_sidecar.connections import ConnectionRepository
    from ..connections.test_repository import profile_input, candidate
    path = tmp_path / "runtime.sqlite3"
    database = RuntimeDatabase.open(path)
    with database.write_session() as session:
        repository = ConnectionRepository(session)
        profile = repository.create(profile_input("prod"))
        repository.trust_first_host_key(candidate(profile.connection_id, b"first"))
    database.close()
    before = snapshot(path)
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    # 历史表定义显式携带 FK 删除动作，不借用会丢失内联 ondelete 的反射结果。
    body = """table = sa.Table("host_keys", sa.MetaData(),
    sa.Column("host_key_id", sa.Text, primary_key=True, nullable=False),
    sa.Column("connection_id", sa.Text, sa.ForeignKey("connection_profiles.connection_id", ondelete="CASCADE"), nullable=False),
    sa.Column("key_algorithm", sa.Text, nullable=False),
    sa.Column("fingerprint_sha256", sa.Text, nullable=False),
    sa.Column("public_key_openssh", sa.LargeBinary, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("confirmed_at", sa.Text, nullable=False),
    sa.Column("replaced_at", sa.Text),
    sa.UniqueConstraint("connection_id", "fingerprint_sha256"),
    sa.CheckConstraint("status IN ('active', 'replaced')"), sqlite_strict=True)
sa.Index("one_active_host_key_per_connection", table.c.connection_id, unique=True, sqlite_where=sa.text("status = 'active'"))
with op.batch_alter_table("host_keys", recreate="always", copy_from=table, table_kwargs={"sqlite_strict": True}) as batch:
    pass"""
    add_revision(directory, "test_keys", "0001_initial", body)
    add_revision(directory, "test_final", "test_keys",
                 'raise RuntimeError("after rebuild")' if fail_after_rebuild else 'pass')
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    if fail_after_rebuild:
        with pytest.raises(RuntimeError, match="after rebuild"):
            runner.upgrade_database(path)
        assert snapshot(path) == before
    else:
        runner.upgrade_database(path)
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [("test_final",)]
            assert connection.execute("SELECT COUNT(*) FROM host_keys").fetchone() == (1,)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='one_active_host_key_per_connection'").fetchone()
