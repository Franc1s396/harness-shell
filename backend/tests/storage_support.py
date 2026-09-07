"""测试调用者的短事务与独立 SQL 检查；不进入生产包。"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from harness_shell_sidecar.storage import RuntimeDatabase


class RepositoryClient:
    """让测试每次调用真实仓库时拥有独立事务，不模拟仓库结果。"""

    def __init__(self, database: RuntimeDatabase, factory: Callable) -> None:
        """工厂接收真实 Session；返回值在事务关闭前已物化。"""
        self.database = database  # 测试调用者借用的数据库工厂。
        self.factory = factory  # 把当前 Session 传给真实 repository。

    def __getattr__(self, name: str) -> Callable:
        """为测试的数据库操作提供应用层原本拥有的事务边界。"""
        def invoke(*args: Any, **kwargs: Any) -> Any:
            """调用真实仓库并在成功时提交，失败时回滚。"""
            read = name.startswith(("get", "list", "load", "resolve", "active_", "conversation_exists"))
            context = self.database.read_session if read else self.database.write_session
            with context() as session:
                return getattr(self.factory(session), name)(*args, **kwargs)
        return invoke


@dataclass
class SqlResult:
    """独立 sqlite3 检查返回的已物化行，连接在 helper 内关闭。"""

    rows: list[tuple]  # 已物化的异构 SQL 列，由具体测试验证形状。
    rowcount: int  # 独立 SQL 执行影响的行数。

    def fetchone(self) -> tuple | None:
        """返回第一行或空结果。"""
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        """返回完整独立查询结果。"""
        return self.rows


def sql(database: RuntimeDatabase, statement: str, parameters: tuple = ()) -> SqlResult:
    """在测试自己的连接执行受控 SQL，独立验证 ORM 落盘结果。"""
    with closing(sqlite3.connect(database.path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        cursor = connection.execute(statement, parameters)
        return SqlResult(cursor.fetchall(), cursor.rowcount)
