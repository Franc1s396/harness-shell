"""不编码或加密的明文运行时记录持久化。"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .orm import RuntimeRecordRow


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
    """借用当前操作 Session 读写明文记录，不提交或关闭事务。"""

    def __init__(self, session: Session) -> None:
        """Session 生命周期由应用操作拥有。"""
        self._session = session  # 只在当前操作中使用，不跨 await 保存。

    def put(self, record: PlaintextRecord) -> None:
        """更新 payload，保留首次创建时间。"""
        now = _utc_now()
        statement = insert(RuntimeRecordRow).values(
            record_type=record.record_type, record_id=record.record_id,
            schema_version=record.schema_version, payload=record.payload,
            created_at=now, updated_at=now)
        self._session.execute(statement.on_conflict_do_update(
            index_elements=[RuntimeRecordRow.record_type, RuntimeRecordRow.record_id],
            set_={"schema_version": statement.excluded.schema_version,
                  "payload": statement.excluded.payload, "updated_at": statement.excluded.updated_at}))

    def get(self, record_type: str, record_id: str) -> PlaintextRecord | None:
        """返回已物化载荷，不向调用者泄露 ORM 实例。"""
        row = self._session.get(RuntimeRecordRow, (record_type, record_id), populate_existing=True)
        return None if row is None else PlaintextRecord(row.record_type, row.record_id, row.schema_version, row.payload)

    def delete(self, record_type: str, record_id: str) -> bool:
        """删除组合键记录并报告它原先是否存在。"""
        result = self._session.execute(delete(RuntimeRecordRow).where(
            RuntimeRecordRow.record_type == record_type, RuntimeRecordRow.record_id == record_id))
        return result.rowcount == 1

    def list_ids(self, record_type: str) -> Sequence[str]:
        """按稳定字典顺序物化命名空间中的标识。"""
        return tuple(self._session.scalars(select(RuntimeRecordRow.record_id).where(
            RuntimeRecordRow.record_type == record_type).order_by(RuntimeRecordRow.record_id)))


def _utc_now() -> str:
    """生成现有协议的毫秒级 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
