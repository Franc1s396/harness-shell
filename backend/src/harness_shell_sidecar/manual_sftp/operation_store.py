"""Authenticated encrypted persistence for remote manual SFTP operations."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from harness_shell_sidecar.storage import EncryptedRecord, EncryptedRecordStore

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
    """Persist only encrypted mutation/reconciliation state, never file content."""

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
    """Persist a complete one-shot delete manifest without a replayable session ID."""

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


class RemoteOperationStore:
    """Authenticate every decrypted operation before returning domain state."""

    def __init__(self, records: EncryptedRecordStore) -> None:
        """Bind the shared encrypted record store."""

        self._records = records

    def put(self, record: RemoteOperationRecord) -> None:
        """Encrypt and atomically insert or replace one complete record."""

        payload = record.model_dump_json().encode("utf-8")
        self._records.put(
            EncryptedRecord(
                RECORD_TYPE,
                str(record.operation_id),
                RECORD_SCHEMA_VERSION,
                payload,
            )
        )

    def get(self, operation_id: UUID) -> RemoteOperationRecord | None:
        """Authenticate, decrypt, validate, and identity-check one record."""

        encrypted = self._records.get(RECORD_TYPE, str(operation_id))
        if encrypted is None:
            return None
        if encrypted.schema_version != RECORD_SCHEMA_VERSION:
            raise ManualSftpError(
                "SFTP_OPERATION_RECORD_INVALID",
                "The encrypted operation record schema is unsupported.",
            )
        try:
            record = RemoteOperationRecord.model_validate_json(encrypted.payload)
        except (ValueError, ValidationError) as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_RECORD_INVALID",
                "The encrypted operation record is invalid.",
            ) from exc
        if record.operation_id != operation_id:
            raise ManualSftpError(
                "SFTP_OPERATION_RECORD_INVALID",
                "The encrypted operation record identity is invalid.",
            )
        return record

    def delete(self, operation_id: UUID) -> bool:
        """Delete one operation record by authenticated composite identity."""

        return self._records.delete(RECORD_TYPE, str(operation_id))

    def list_non_terminal(self) -> tuple[RemoteOperationRecord, ...]:
        """Decrypt records selected only by IDs and return non-terminal state."""

        rows = self._records.connection.execute(
            """
            SELECT record_id
            FROM encrypted_records
            WHERE record_type = ?
            ORDER BY record_id
            """,
            (RECORD_TYPE,),
        ).fetchall()
        result: list[RemoteOperationRecord] = []
        for (record_id,) in rows:
            try:
                operation_id = UUID(record_id)
            except (TypeError, ValueError) as exc:
                raise ManualSftpError(
                    "SFTP_OPERATION_RECORD_INVALID",
                    "The encrypted operation record identity is invalid.",
                ) from exc
            record = self.get(operation_id)
            if record is None:
                raise ManualSftpError(
                    "SFTP_OPERATION_RECORD_INVALID",
                    "The encrypted operation record disappeared during listing.",
                )
            if record.state not in TERMINAL_STATES:
                result.append(record)
        return tuple(result)

    def put_delete_plan(self, plan: DeletePlanRecord) -> None:
        """Encrypt a complete one-shot delete plan and its canonical manifest."""

        self._records.put(
            EncryptedRecord(
                DELETE_PLAN_RECORD_TYPE,
                str(plan.delete_plan_id),
                RECORD_SCHEMA_VERSION,
                plan.model_dump_json().encode("utf-8"),
            )
        )

    def get_delete_plan(self, delete_plan_id: UUID) -> DeletePlanRecord | None:
        """Authenticate and validate one encrypted delete plan."""

        encrypted = self._records.get(
            DELETE_PLAN_RECORD_TYPE, str(delete_plan_id)
        )
        if encrypted is None:
            return None
        if encrypted.schema_version != RECORD_SCHEMA_VERSION:
            raise ManualSftpError(
                "SFTP_DELETE_PLAN_INVALID",
                "The encrypted recursive-delete plan schema is unsupported.",
            )
        try:
            plan = DeletePlanRecord.model_validate_json(encrypted.payload)
        except (ValueError, ValidationError) as exc:
            raise ManualSftpError(
                "SFTP_DELETE_PLAN_INVALID",
                "The encrypted recursive-delete plan is invalid.",
            ) from exc
        if plan.delete_plan_id != delete_plan_id:
            raise ManualSftpError(
                "SFTP_DELETE_PLAN_INVALID",
                "The encrypted recursive-delete plan identity is invalid.",
            )
        return plan
