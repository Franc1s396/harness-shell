"""不编码或加密的明文运行时记录持久化。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import RuntimeDatabase


@dataclass(frozen=True, slots=True)
class PlaintextRecord:
    """描述不可变明文载荷及其组合标识。"""

    #: 持久化组合键第一部分使用的业务命名空间。
    record_type: str
    #: record_type 内唯一的标识。
    record_id: str
    #: 所属仓库用来校验载荷的正整数版本。
    schema_version: int
    #: 不改写、直接持久化到 Runtime SQLite 的明文字节。
    payload: bytes

    def __post_init__(self) -> None:
        """拒绝无法形成稳定记录的标识或载荷。"""

        if not self.record_type or not self.record_id:
            raise ValueError("record type and id must not be empty")
        if self.schema_version <= 0:
            raise ValueError("record schema version must be positive")
        if not isinstance(self.payload, bytes):
            raise TypeError("record payload must be bytes")


class PlaintextRecordStore:
    """管理 schema v7 通用明文运行时记录的 CRUD。"""

    _database: RuntimeDatabase

    def __init__(self, database: RuntimeDatabase) -> None:
        """将存储绑定到 Runtime 拥有的共享数据库连接。"""

        # 所有仓库结束后，由 Runtime 管理者关闭共享连接。
        self._database = database

    @property
    def connection(self) -> sqlite3.Connection:
        """提供共享连接以支持仓库级原子事务。"""

        return self._database.connection

    def put(self, record: PlaintextRecord) -> None:
        """插入或更新载荷，同时保留创建时间戳。"""

        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO runtime_records(
                record_type, record_id, schema_version, payload,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_type, record_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                record.record_type,
                record.record_id,
                record.schema_version,
                record.payload,
                now,
                now,
            ),
        )

    def get(self, record_type: str, record_id: str) -> PlaintextRecord | None:
        """返回精确组合键记录；不存在时返回 None。"""

        row = self._database.execute(
            """
            SELECT schema_version, payload
            FROM runtime_records
            WHERE record_type = ? AND record_id = ?
            """,
            (record_type, record_id),
        ).fetchone()
        if row is None:
            return None
        schema_version, payload = row
        return PlaintextRecord(record_type, record_id, schema_version, payload)

    def delete(self, record_type: str, record_id: str) -> bool:
        """删除精确组合键记录，并报告其原先是否存在。"""

        cursor = self._database.execute(
            "DELETE FROM runtime_records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        return cursor.rowcount == 1

    def list_ids(self, record_type: str) -> Sequence[str]:
        """按稳定字典序返回命名空间内的 ID。"""

        rows = self._database.execute(
            """
            SELECT record_id
            FROM runtime_records
            WHERE record_type = ?
            ORDER BY record_id
            """,
            (record_type,),
        ).fetchall()
        return tuple(row[0] for row in rows)


def _utc_now() -> str:
    """为记录生命周期元数据返回毫秒级 UTC 时间戳。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
