"""只读验证数据库身份和当前 ORM 对应的严格持久化契约。"""

import sqlite3
from sqlalchemy import CheckConstraint, Connection, UniqueConstraint, inspect

from .database import StorageSelfCheckFailed
from .orm import Base


def check_database_identity(connection: Connection | sqlite3.Connection,
                            revisions: frozenset[str]) -> str | None:
    """拒绝旧库和未知版本；只读预检与持锁后的复检共用同一规则。"""
    execute = connection.exec_driver_sql if isinstance(connection, Connection) else connection.execute
    objects = execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if not objects:
        return None
    tables = {name for kind, name in objects if kind == "table"}
    if "alembic_version" not in tables or "schema_migrations" in tables:
        raise StorageSelfCheckFailed("incompatible database identity")
    rows = execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1 or rows[0][0] not in revisions:
        raise StorageSelfCheckFailed("unsupported database revision")
    return rows[0][0]


def check_current_schema(connection: Connection) -> None:
    """逐项比较结构并检查实际数据；不以写入探测约束。"""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if tables != set(Base.metadata.tables) | {"alembic_version"}:
        raise StorageSelfCheckFailed("database tables do not match current schema")
    if inspector.get_view_names():
        raise StorageSelfCheckFailed("unexpected database views")
    if connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").first():
        raise StorageSelfCheckFailed("unexpected database triggers")
    strict = {row[1]: row[5] for row in connection.exec_driver_sql("PRAGMA table_list")}
    # 1. ORM metadata 描述当前契约，历史 revision 不导入它。
    for name, table in Base.metadata.tables.items():
        if strict.get(name) != 1:
            raise StorageSelfCheckFailed(f"{name} must be STRICT")
        columns = inspector.get_columns(name)
        expected = {column.name: column for column in table.columns}
        if {column["name"] for column in columns} != set(expected):
            raise StorageSelfCheckFailed(f"{name} columns do not match")
        for column in columns:
            model = expected[column["name"]]
            default = str(model.server_default.arg) if model.server_default else None
            if (str(column["type"]) != str(model.type)
                    or column["nullable"] != model.nullable
                    or column["default"] != default):
                raise StorageSelfCheckFailed(f"{name} column contract does not match")
        if inspector.get_pk_constraint(name)["constrained_columns"] != [c.name for c in table.primary_key]:
            raise StorageSelfCheckFailed(f"{name} primary key does not match")
        actual_unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)}
        expected_unique = {tuple(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)}
        if actual_unique != expected_unique:
            raise StorageSelfCheckFailed(f"{name} uniqueness does not match")
        # 2. CHECK 文本使用确定的空白规范化；不猜测或修复未知表达式。
        checks = {" ".join(c["sqltext"].split()) for c in inspector.get_check_constraints(name)}
        expected_checks = {" ".join(str(c.sqltext).split()) for c in table.constraints if isinstance(c, CheckConstraint)}
        if checks != expected_checks:
            raise StorageSelfCheckFailed(f"{name} checks do not match")
        # Inspector 对内联外键的 ondelete 反射不完整；直接使用 SQLite 权威元数据。
        foreign_rows = connection.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")').fetchall()
        actual_fk = {((row[3],), row[2], (row[4],), row[6]) for row in foreign_rows}
        expected_fk = {(tuple(c.parent.name for c in fk.elements), fk.referred_table.name,
                        tuple(c.column.name for c in fk.elements), fk.ondelete or "NO ACTION")
                       for fk in table.foreign_key_constraints}
        if actual_fk != expected_fk:
            raise StorageSelfCheckFailed(f"{name} foreign keys do not match")
        indexes = inspector.get_indexes(name)
        if {idx["name"] for idx in indexes} != {idx.name for idx in table.indexes}:
            raise StorageSelfCheckFailed(f"{name} indexes do not match")
        for actual in indexes:
            index = next(i for i in table.indexes if i.name == actual["name"])
            predicate = index.dialect_options["sqlite"].get("where")
            expected_where = str(predicate.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True, "include_table": False})) if predicate is not None else None
            actual_where = actual.get("dialect_options", {}).get("sqlite_where")
            if (actual["column_names"] != list(index.columns.keys())
                    or bool(actual["unique"]) != index.unique
                    or (str(actual_where) if actual_where is not None else None) != expected_where):
                raise StorageSelfCheckFailed(f"{name} index definition does not match")
    # 3. 校验包含 batch 重建结果的完整性，在外层 COMMIT 之前失败。
    if connection.exec_driver_sql("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise StorageSelfCheckFailed("SQLite integrity check failed")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise StorageSelfCheckFailed("SQLite foreign key check failed")
