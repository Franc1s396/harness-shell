"""Runtime 拥有的同步 Engine 与操作级 Session 生命周期。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .engine import create_runtime_engine, cleanup_preserving_error


class StorageSelfCheckFailed(RuntimeError):
    """数据库身份、结构或资源状态不可信。"""


class RuntimeDatabase:
    """拥有 Engine，不长期持有连接；应用操作显式借用短 Session。"""

    def __init__(self, path: Path, engine: Engine) -> None:
        """保存已经迁移与验证的数据库资源。"""
        self.path = path  # 唯一 Runtime 数据文件的绝对路径。
        self._engine = engine  # 本对象独占关闭的连接工厂。
        # 每次操作显式加入外层事务，关闭后不可重用，也不隐式开启新事务。
        self._sessions = sessionmaker(autobegin=False, expire_on_commit=False,
                                     join_transaction_mode="rollback_only", close_resets_only=False)
        self._closed = False  # 关闭后禁止创建新 Session。
        self._active = 0  # 活动操作计数，禁止资源仍被借用时关闭。

    @classmethod
    def open(cls, path: Path) -> "RuntimeDatabase":
        """迁移提交后创建业务 Engine；失败不发布 Runtime。"""
        from .migration_runner import upgrade_database
        upgrade_database(path)
        engine = create_runtime_engine(path)
        try:
            # PRAGMA journal_mode 不能在自动 BEGIN 的业务路径执行。
            raw = engine.raw_connection()
            try:
                if raw.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise StorageSelfCheckFailed("SQLite WAL mode unavailable")
            finally:
                cleanup_preserving_error(raw.close)
            return cls(path, engine)
        except BaseException:
            cleanup_preserving_error(engine.dispose)
            raise

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        """读取操作退出时回滚，避免误写被提交。"""
        with self._session(write=False) as session:
            yield session

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """写操作使用 BEGIN IMMEDIATE；成功才提交全部仓库变更。"""
        with self._session(write=True) as session:
            yield session

    @contextmanager
    def _session(self, *, write: bool) -> Iterator[Session]:
        """Connection 独占提交，Session.close 不接管外层事务。"""
        if self._closed:
            raise StorageSelfCheckFailed("runtime database is closed")
        self._active += 1
        try:
            connection = self._engine.connect().execution_options(sqlite_immediate=write)
            try:
                transaction = connection.begin()
                try:
                    session = self._sessions(bind=connection)
                    try:
                        session.begin()
                        yield session
                        if write:
                            session.flush()
                    finally:
                        cleanup_preserving_error(session.close)
                    if write:
                        transaction.commit()
                    else:
                        transaction.rollback()
                except BaseException:
                    # flush 失败时 SQLAlchemy 可能已回滚外层事务，不重复操作已失效事务。
                    if transaction.is_active:
                        cleanup_preserving_error(transaction.rollback)
                    raise
            finally:
                cleanup_preserving_error(connection.close)
        finally:
            self._active -= 1

    def close(self) -> None:
        """最后一个业务 owner 结束后 checkpoint 并释放 Engine。"""
        if self._closed:
            return
        if self._active:
            raise StorageSelfCheckFailed("database still has active sessions")
        try:
            raw = self._engine.raw_connection()
            try:
                if raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise StorageSelfCheckFailed("database checkpoint is busy")
            finally:
                cleanup_preserving_error(raw.close)
        finally:
            cleanup_preserving_error(self._engine.dispose)
            self._closed = True
