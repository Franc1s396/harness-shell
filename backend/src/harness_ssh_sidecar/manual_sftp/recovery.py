"""只读状态核对与显式新恢复操作。"""

from __future__ import annotations

import posixpath
from uuid import UUID

from .channels import SftpChannelFactory
from .errors import ManualSftpError
from .models import (
    OperationTerminalProjection,
    RecoverySummary,
)
from .mutations import MutationManager
from .operation_store import ManualSftpOperationStore, RemoteOperationRecord
from .transfers import _snapshot


class RecoveryManager:
    """检查不确定状态，仅执行用户新确认的操作。"""

    def __init__(
        self,
        channels: SftpChannelFactory,
        operations: ManualSftpOperationStore,
        mutations: MutationManager,
    ) -> None:
        """绑定活动会话解析、明文状态和新变更管理者。"""

        self._channels = channels
        self._operations = operations
        self._mutations = mutations

    def list(self) -> tuple[RecoverySummary, ...]:
        """返回明文非终态操作记录的安全摘要。"""

        return tuple(self._summary(record) for record in self._operations.list_non_terminal())

    async def inspect(
        self, recovery_id: UUID
    ) -> RecoverySummary | OperationTerminalProjection:
        """仅使用元数据和哈希读取核对恢复记录。"""

        # 1. 优先返回已经证实的终态回执，避免重复远程核对。
        record = self._record(recovery_id, include_terminal=True)
        if record.state in {"succeeded", "failed", "cancelled"}:
            if record.terminal_receipt is None:
                raise ManualSftpError(
                    "SFTP_OPERATION_RECORD_INVALID",
                    "The terminal recovery record has no receipt.",
                )
            return record.terminal_receipt
        # 2. 未终结记录必须匹配原认证链，再仅用元数据和哈希检查远程状态。
        session_id = self._session_id(record)
        lease = await self._channels.open(session_id)
        try:
            if record.kind == "upload" and record.temp_path is not None:
                target = await _snapshot(
                    lease.client,
                    record.remote_path,
                    include_hash=record.expected_sha256 is not None,
                )
                if target.exists and target.sha256 == record.expected_sha256:
                    return self._persist_reconciled_terminal(
                        record,
                        _reconciled_terminal(
                            record.operation_id,
                            "succeeded",
                            None,
                            "The uploaded target matches the expected hash.",
                        ),
                    )
                temporary = await _snapshot(
                    lease.client, record.temp_path, include_hash=False
                )
                if temporary.exists:
                    return self._summary(record)
                return self._persist_reconciled_terminal(
                    record,
                    _reconciled_terminal(
                        record.operation_id,
                        "failed",
                        "SFTP_RECOVERY_TARGET_MISSING",
                        "Neither the upload target nor its temporary file could be verified.",
                    ),
                )
            if record.kind == "recursive_delete" and record.temp_path is not None:
                root = await _snapshot(
                    lease.client, record.remote_path, include_hash=False
                )
                tombstone = await _snapshot(
                    lease.client, record.temp_path, include_hash=False
                )
                if tombstone.exists:
                    return self._summary(record)
                if not root.exists:
                    return self._persist_reconciled_terminal(
                        record,
                        _reconciled_terminal(
                            record.operation_id,
                            "succeeded",
                            None,
                            "The recursive-delete root and tombstone are absent.",
                        ),
                    )
                return self._persist_reconciled_terminal(
                    record,
                    _reconciled_terminal(
                        record.operation_id,
                        "failed",
                        "SFTP_RECOVERY_DELETE_NOT_APPLIED",
                        "The original recursive-delete root still exists.",
                    ),
                )
            # 3. 无法证明终态时保留不确定状态，并只返回允许的显式恢复操作。
            return self._summary(record)
        finally:
            await lease.close()

    async def execute(
        self, recovery_id: UUID, action: str, operation_id: UUID
    ) -> RecoverySummary | OperationTerminalProjection:
        """每次恢复变更使用 React 选择的新标识。"""

        # 1. 拒绝旧操作标识，并验证操作位于当前记录的允许列表。
        self._require_fresh_operation(recovery_id, operation_id)
        record = self._record(recovery_id)
        summary = self._summary(record)
        if action not in summary.available_actions:
            raise ManualSftpError(
                "SFTP_RECOVERY_ACTION_INVALID",
                "The requested recovery action is not available.",
            )
        # 2. 保留和验证分支不执行变更；验证交给只读核对。
        if action == "keep":
            return summary
        if action == "verify":
            return await self.inspect(recovery_id)

        # 3. 变更必须重新匹配认证链，并以新标识派发删除、续删或还原。
        session_id = self._session_id(record)
        if action == "delete_temp" and record.temp_path is not None:
            lease = await self._channels.open(session_id)
            try:
                snapshot = await _snapshot(
                    lease.client, record.temp_path, include_hash=True
                )
            finally:
                await lease.close()
            result = await self._mutations.remove(
                operation_id=operation_id,
                ssh_session_id=session_id,
                path=record.temp_path,
                expected_snapshot=snapshot,
            )
            self._resolve_old(record)
            return result
        if action == "continue_delete" and record.temp_path is not None:
            plan = await self._mutations.delete_preflight(
                session_id,
                record.temp_path,
                operation_id=operation_id,
            )
            result = await self._mutations.delete_execute(plan.delete_plan_id)
            if result.state == "succeeded":
                self._resolve_old(record)
            return result
        if action == "restore_tombstone" and record.temp_path is not None:
            result = await self._mutations.rename(
                operation_id=operation_id,
                ssh_session_id=session_id,
                source_path=record.temp_path,
                target_path=record.remote_path,
                overwrite=False,
            )
            self._resolve_old(record)
            return result
        raise ManualSftpError(
            "SFTP_RECOVERY_ACTION_INVALID",
            "The requested recovery action is not implemented for this record.",
        )

    def _require_fresh_operation(
        self, recovery_id: UUID, operation_id: UUID
    ) -> None:
        """执行任何恢复变更 I/O 前拒绝旧标识或已持久化标识。"""

        if operation_id == recovery_id or self._operations.get(operation_id):
            raise ManualSftpError(
                "SFTP_OPERATION_DUPLICATE",
                "The recovery mutation operation ID must be fresh.",
            )

    async def close_all(self) -> None:
        """恢复流程不在显式请求之间持有远程通道。"""

    def _session_id(self, record: RemoteOperationRecord) -> UUID:
        """仅按精确配置、Host Key 和 ProxyJump 标识解析。"""

        return self._channels.session_id_for_recovery(
            connection_id=record.connection_id,
            connection_profile_version=record.connection_profile_version,
            target_host_key_fingerprint=record.target_host_key_fingerprint,
            jump_connection_id=record.jump_connection_id,
            jump_profile_version=record.jump_profile_version,
            jump_host_key_fingerprint=record.jump_host_key_fingerprint,
        )

    def _record(
        self, recovery_id: UUID, *, include_terminal: bool = False
    ) -> RemoteOperationRecord:
        """将恢复 ID 解析为明文非终态操作。"""

        record = self._operations.get(recovery_id)
        if record is None or (
            not include_terminal
            and record.state in {"succeeded", "failed", "cancelled"}
        ):
            raise ManualSftpError(
                "SFTP_RECOVERY_NOT_FOUND", "The recovery record was not found."
            )
        return record

    @staticmethod
    def _summary(record: RemoteOperationRecord) -> RecoverySummary:
        """仅投影安全显示状态和显式操作允许列表。"""

        if record.kind == "upload":
            kind = "upload_temp"
            remote_path = record.temp_path
            actions = ("verify", "delete_temp", "keep")
        elif record.kind == "recursive_delete":
            kind = "delete_tombstone"
            remote_path = record.temp_path
            actions = ("verify", "continue_delete", "restore_tombstone", "keep")
        else:
            kind = "mutation_unknown"
            remote_path = record.remote_path
            actions = ("verify", "keep")
        state = (
            "cleanup_required"
            if record.state == "cleanup_required"
            else "outcome_unknown"
        )
        return RecoverySummary(
            recovery_id=record.operation_id,
            operation_id=record.operation_id,
            kind=kind,
            host_label=record.host_label,
            remote_path=remote_path,
            display_name=posixpath.basename((remote_path or record.remote_path).rstrip("/"))
            or "/",
            state=state,
            created_at=record.created_at,
            available_actions=actions,
        )

    def _resolve_old(self, record: RemoteOperationRecord) -> None:
        """将旧操作标为已解决，不用新操作替换它。"""

        receipt = _reconciled_terminal(
            record.operation_id,
            "failed",
            record.terminal_receipt.error_code if record.terminal_receipt else None,
            "The separate user-confirmed recovery action completed.",
        )
        self._operations.put(
            record.model_copy(update={"state": "failed", "terminal_receipt": receipt})
        )

    def _persist_reconciled_terminal(
        self,
        record: RemoteOperationRecord,
        receipt: OperationTerminalProjection,
    ) -> OperationTerminalProjection:
        """根据已证实的只读结果原子替换非终态记录。"""

        self._operations.put(
            record.model_copy(
                update={"state": receipt.state, "terminal_receipt": receipt}
            )
        )
        return receipt


def _reconciled_terminal(
    operation_id: UUID, state: str, error_code: str | None, message: str
) -> OperationTerminalProjection:
    """为旧操作构建安全的只读核对结果。"""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state=state,
        error_code=error_code,
        message=message,
        sha256=None,
        byte_count=None,
        recovery_id=None,
    )
