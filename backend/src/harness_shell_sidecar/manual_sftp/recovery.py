"""Read-only reconciliation and explicit new recovery actions."""

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
from .operation_store import RemoteOperationRecord, RemoteOperationStore
from .transfers import _snapshot


class RecoveryManager:
    """Inspect uncertain state and execute only new user-confirmed operations."""

    def __init__(
        self,
        channels: SftpChannelFactory,
        operations: RemoteOperationStore,
        mutations: MutationManager,
    ) -> None:
        """Bind live-session resolution, encrypted state, and new mutation owner."""

        self._channels = channels
        self._operations = operations
        self._mutations = mutations

    def list(self) -> tuple[RecoverySummary, ...]:
        """Return safe summaries for encrypted non-terminal operation records."""

        return tuple(self._summary(record) for record in self._operations.list_non_terminal())

    async def inspect(
        self, recovery_id: UUID
    ) -> RecoverySummary | OperationTerminalProjection:
        """Use only metadata/hash reads to reconcile one recovery record."""

        record = self._record(recovery_id, include_terminal=True)
        if record.state in {"succeeded", "failed", "cancelled"}:
            if record.terminal_receipt is None:
                raise ManualSftpError(
                    "SFTP_OPERATION_RECORD_INVALID",
                    "The terminal recovery record has no receipt.",
                )
            return record.terminal_receipt
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
            return self._summary(record)
        finally:
            await lease.close()

    async def execute(
        self, recovery_id: UUID, action: str, operation_id: UUID
    ) -> RecoverySummary | OperationTerminalProjection:
        """Use the Rust-selected fresh identity for every mutating recovery action."""

        self._require_fresh_operation(recovery_id, operation_id)
        record = self._record(recovery_id)
        summary = self._summary(record)
        if action not in summary.available_actions:
            raise ManualSftpError(
                "SFTP_RECOVERY_ACTION_INVALID",
                "The requested recovery action is not available.",
            )
        if action == "keep":
            return summary
        if action == "verify":
            return await self.inspect(recovery_id)

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
        """Reject old or already-persisted identities before any recovery mutation I/O."""

        if operation_id == recovery_id or self._operations.get(operation_id):
            raise ManualSftpError(
                "SFTP_OPERATION_DUPLICATE",
                "The recovery mutation operation ID must be fresh.",
            )

    async def close_all(self) -> None:
        """Recovery owns no persistent remote channel between explicit requests."""

    def _session_id(self, record: RemoteOperationRecord) -> UUID:
        """Resolve only the exact profile, Host Key, and ProxyJump identity."""

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
        """Resolve one recovery ID to its encrypted non-terminal operation."""

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
        """Project only safe display state and an explicit action allowlist."""

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
        """Mark the old operation resolved without replacing it with the new action."""

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
        """Atomically replace a non-terminal record with its proven read-only result."""

        self._operations.put(
            record.model_copy(
                update={"state": receipt.state, "terminal_receipt": receipt}
            )
        )
        return receipt


def _reconciled_terminal(
    operation_id: UUID, state: str, error_code: str | None, message: str
) -> OperationTerminalProjection:
    """Build a safe read-only reconciliation result for the old operation."""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state=state,
        error_code=error_code,
        message=message,
        sha256=None,
        byte_count=None,
        recovery_id=None,
    )
