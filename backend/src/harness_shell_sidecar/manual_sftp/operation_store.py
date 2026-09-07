"""远程手动 SFTP 操作的严格明文持久化。"""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore, RuntimeDatabase

from .errors import ManualSftpError
from .models import (
    DeleteManifestEntry,
    DeletePlanSummary,
    JsSafeInt,
    OperationTerminalProjection,
    Sha256Hex,
    StrictModel,
    TransferSnapshot,
)


RECORD_TYPE = "manual_sftp_operation"
DELETE_PLAN_RECORD_TYPE = "manual_sftp_delete_plan"
RECORD_SCHEMA_VERSION = 2
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class RemoteOperationRecord(StrictModel):
    """仅持久化远程变更和核对状态，不保存文件内容。"""

    operation_id: UUID
    kind: Literal["upload", "recursive_delete", "rename", "remove", "mkdir"]
    state: Literal[
        "preparing",
        "transferring",
        "verifying",
        "committing",
        "succeeded",
        "failed",
        "cancelled",
        "cleanup_required",
        "outcome_unknown",
    ]
    connection_id: UUID
    connection_profile_version: JsSafeInt
    host_label: str
    target_host_key_fingerprint: str
    jump_connection_id: UUID | None
    jump_profile_version: JsSafeInt | None
    jump_host_key_fingerprint: str | None
    remote_path: str
    temp_path: str | None
    expected_sha256: Sha256Hex | None
    target_snapshot: TransferSnapshot | None
    terminal_receipt: OperationTerminalProjection | None
    created_at: str


class DeletePlanRecord(StrictModel):
    """持久化完整的一次性删除清单，不携带可回放会话 ID。"""

    delete_plan_id: UUID
    operation_id: UUID
    connection_id: UUID
    connection_profile_version: JsSafeInt
    host_label: str
    target_host_key_fingerprint: str
    jump_connection_id: UUID | None
    jump_profile_version: JsSafeInt | None
    jump_host_key_fingerprint: str | None
    root_path: str
    tombstone_path: str
    summary: DeletePlanSummary
    manifest: tuple[DeleteManifestEntry, ...]
    created_at: str
    consumed: bool
    terminal_receipt: OperationTerminalProjection | None


class ManualSftpOperationStore:
    """返回领域状态前校验每条明文操作记录。"""

    def __init__(self, database: RuntimeDatabase) -> None:
        """绑定数据库工厂，每次领域操作拥有独立短事务。"""

        self._database = database  # 每次领域存储操作创建短 Session。

    def put(self, record: RemoteOperationRecord) -> None:
        """原子插入或替换完整严格 JSON 记录。"""

        with self._database.write_session() as session:
            PlaintextRecordStore(session).put(
                PlaintextRecord(
                    RECORD_TYPE,
                    str(record.operation_id),
                    RECORD_SCHEMA_VERSION,
                    record.model_dump_json().encode("utf-8"),
                )
            )

    def get(self, operation_id: UUID) -> RemoteOperationRecord | None:
        """严格解码 UTF-8 JSON，校验记录内容及标识。"""

        with self._database.read_session() as session:
            stored = PlaintextRecordStore(session).get(RECORD_TYPE, str(operation_id))
        return _decode_operation(stored, operation_id)

    def delete(self, operation_id: UUID) -> bool:
        """通过组合标识删除操作记录。"""

        with self._database.write_session() as session:
            return PlaintextRecordStore(session).delete(RECORD_TYPE, str(operation_id))

    def list_non_terminal(self) -> tuple[RemoteOperationRecord, ...]:
        """按稳定创建顺序返回已校验的非终态记录。"""

        result: list[RemoteOperationRecord] = []
        with self._database.read_session() as session:
            store = PlaintextRecordStore(session)
            for record_id in store.list_ids(RECORD_TYPE):
                try:
                    operation_id = UUID(record_id)
                except (TypeError, ValueError) as error:
                    raise _operation_record_error(
                        "The operation record identity is invalid."
                    ) from error
                record = _decode_operation(store.get(RECORD_TYPE, record_id), operation_id)
                if record is None:
                    raise _operation_record_error("The operation record disappeared during listing.")
                if record.state not in TERMINAL_STATES:
                    result.append(record)
        result.sort(key=lambda value: (value.created_at, str(value.operation_id)))
        return tuple(result)

    def put_delete_plan(self, plan: DeletePlanRecord) -> None:
        """持久化完整的一次性删除计划和规范清单。"""

        with self._database.write_session() as session:
            PlaintextRecordStore(session).put(
                PlaintextRecord(
                    DELETE_PLAN_RECORD_TYPE,
                    str(plan.delete_plan_id),
                    RECORD_SCHEMA_VERSION,
                    plan.model_dump_json().encode("utf-8"),
                )
            )

    def get_delete_plan(self, delete_plan_id: UUID) -> DeletePlanRecord | None:
        """解码并校验明文删除计划。"""

        with self._database.read_session() as session:
            stored = PlaintextRecordStore(session).get(DELETE_PLAN_RECORD_TYPE, str(delete_plan_id))
        if stored is None:
            return None
        if stored.schema_version != RECORD_SCHEMA_VERSION:
            raise _delete_plan_error(
                "The recursive-delete plan schema is unsupported."
            )
        try:
            plan = DeletePlanRecord.model_validate_json(
                _validated_json_text(stored.payload)
            )
        except (UnicodeDecodeError, ValueError, ValidationError) as error:
            raise _delete_plan_error("The recursive-delete plan is invalid.") from error
        if plan.delete_plan_id != delete_plan_id:
            raise _delete_plan_error(
                "The recursive-delete plan identity is invalid."
            )
        return plan


def _decode_operation(stored: PlaintextRecord | None, operation_id: UUID) -> RemoteOperationRecord | None:
    """校验已经物化的记录，供单项读取与同一快照的列表读取共用。"""
    if stored is None:
        return None
    if stored.schema_version != RECORD_SCHEMA_VERSION:
        raise _operation_record_error("The operation record schema is unsupported.")
    try:
        record = RemoteOperationRecord.model_validate_json(
            _validated_json_text(stored.payload)
        )
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise _operation_record_error("The operation record is invalid.") from error
    if record.operation_id != operation_id:
        raise _operation_record_error("The operation record identity is invalid.")
    return record


def _validated_json_text(payload: bytes) -> str:
    """严格检查 UTF-8 和重复字段后返回规范 JSON。"""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """构造对象，遇到首个重复字段即失败。"""

        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate operation record field")
            result[name] = value
        return result

    value = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise ValueError("operation record must be a JSON object")
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _operation_record_error(message: str) -> ManualSftpError:
    """构建稳定的非法操作记录失败。"""

    return ManualSftpError("SFTP_OPERATION_RECORD_INVALID", message)


def _delete_plan_error(message: str) -> ManualSftpError:
    """构建稳定的非法删除计划失败。"""

    return ManualSftpError("SFTP_DELETE_PLAN_INVALID", message)


__all__ = [
    "DeletePlanRecord",
    "ManualSftpOperationStore",
    "RemoteOperationRecord",
]
