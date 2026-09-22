"""同步 SQLite 连接配置与真实事务控制。"""

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine, Connection, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import ConnectionPoolEntry, NullPool


def create_runtime_engine(path: Path, *, migration: bool = False) -> Engine:
    """返回调用者拥有的 Engine；迁移连接独立于业务外键配置。"""
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)),
                           poolclass=NullPool, echo=False, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, entry: ConnectionPoolEntry) -> None:
        """在任何 BEGIN 前配置驱动，禁止隐式事务与外键 PRAGMA 无效执行。"""
        connection.isolation_level = None
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=" + ("OFF" if migration else "ON"))

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        """由 SQLAlchemy 独占显式 BEGIN，写操作先取得保留锁。"""
        immediate = connection.get_execution_options().get("sqlite_immediate", False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def cleanup_preserving_error(action: Callable[[], None]) -> None:
    """清理失败仍暴露，但已有业务异常时只附加清理错误类型而不覆盖首错。"""
    original = sys.exception()
    try:
        action()
    except BaseException as cleanup:
        if original is None:
            raise
        original.add_note(f"Database cleanup failed: {type(cleanup).__name__}")
