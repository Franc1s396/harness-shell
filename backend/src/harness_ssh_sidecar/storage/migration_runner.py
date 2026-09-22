"""在发布 Runtime 前执行唯一 Alembic 升级链及提交前校验。"""

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .database import StorageSelfCheckFailed
from .engine import create_runtime_engine, cleanup_preserving_error
from .schema_check import check_current_schema, check_database_identity


def migration_resource_dir() -> Path:
    """定位源码包或解包产物内的固定迁移资源。"""
    directory = Path(__file__).parent / "alembic"
    if not (directory / "env.py").is_file() or not (directory / "versions").is_dir():
        raise StorageSelfCheckFailed("migration resources are missing")
    return directory


def upgrade_database(path: Path) -> None:
    """先只读拒绝旧库，再原子执行升级、版本更新和最终自检。"""
    if not path.is_absolute():
        raise StorageSelfCheckFailed("runtime database path must be absolute")
    config = Config()
    config.set_main_option("script_location", str(migration_resource_dir()).replace("%", "%%"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        raise StorageSelfCheckFailed("migration graph must have one head")
    revisions = frozenset(revision.revision for revision in script.walk_revisions())
    # 1. 已有文件拒绝路径不设置 WAL、不写 schema；mode=ro 防止意外新建。
    if path.exists() and path.stat().st_size:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as probe:
            check_database_identity(probe, revisions)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_runtime_engine(path, migration=True)
    try:
        # 2. 写锁取得后复核身份；Alembic 借用同一连接，不独立提交 revision。
        connection = engine.connect().execution_options(sqlite_immediate=True)
        try:
            transaction = connection.begin()
            try:
                check_database_identity(connection, revisions)
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                check_current_schema(connection)
                actual = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
                if actual != heads:
                    raise StorageSelfCheckFailed("migration did not reach the expected head")
                transaction.commit()
            except BaseException:
                if transaction.is_active:
                    cleanup_preserving_error(transaction.rollback)
                raise
        finally:
            cleanup_preserving_error(connection.close)
    finally:
        cleanup_preserving_error(engine.dispose)
