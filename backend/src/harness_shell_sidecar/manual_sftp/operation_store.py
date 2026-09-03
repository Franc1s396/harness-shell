"""Strict plaintext persistence for remote manual SFTP operations."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore

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
    """Persist only remote mutation/reconciliation state, never file content."""

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


class ManualSftpOperationStore:
    """Validate every plaintext operation before returning domain state."""

    def __init__(self, records: PlaintextRecordStore) -> None:
        """Bind the shared schema-v6 plaintext record store."""

        self._records = records

    def put(self, record: RemoteOperationRecord) -> None:
        """Atomically insert or replace one complete strict JSON record."""

        self._records.put(
            PlaintextRecord(
                RECORD_TYPE,
                str(record.operation_id),
                RECORD_SCHEMA_VERSION,
                record.model_dump_json().encode("utf-8"),
            )
        )

    def get(self, operation_id: UUID) -> RemoteOperationRecord | None:
        """Decode strict UTF-8 JSON, validate, and identity-check one record."""

        stored = self._records.get(RECORD_TYPE, str(operation_id))
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

    def delete(self, operation_id: UUID) -> bool:
        """Delete one operation record by its composite identity."""

        return self._records.delete(RECORD_TYPE, str(operation_id))

    def list_non_terminal(self) -> tuple[RemoteOperationRecord, ...]:
        """Return validated non-terminal records in stable creation order."""

        result: list[RemoteOperationRecord] = []
        for record_id in self._records.list_ids(RECORD_TYPE):
            try:
                operation_id = UUID(record_id)
            except (TypeError, ValueError) as error:
                raise _operation_record_error(
                    "The operation record identity is invalid."
                ) from error
            record = self.get(operation_id)
            if record is None:
                raise _operation_record_error(
                    "The operation record disappeared during listing."
                )
            if record.state not in TERMINAL_STATES:
                result.append(record)
        result.sort(key=lambda value: (value.created_at, str(value.operation_id)))
        return tuple(result)

    def put_delete_plan(self, plan: DeletePlanRecord) -> None:
        """Persist a complete one-shot delete plan and canonical manifest."""

        self._records.put(
            PlaintextRecord(
                DELETE_PLAN_RECORD_TYPE,
                str(plan.delete_plan_id),
                RECORD_SCHEMA_VERSION,
                plan.model_dump_json().encode("utf-8"),
            )
        )

    def get_delete_plan(self, delete_plan_id: UUID) -> DeletePlanRecord | None:
        """Decode and validate one plaintext delete plan."""

        stored = self._records.get(DELETE_PLAN_RECORD_TYPE, str(delete_plan_id))
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


def _validated_json_text(payload: bytes) -> str:
    """Return canonical JSON after strict UTF-8 and duplicate-field checks."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Build one object and fail on the first duplicate field."""

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
    """Build the stable invalid operation-record failure."""

    return ManualSftpError("SFTP_OPERATION_RECORD_INVALID", message)


def _delete_plan_error(message: str) -> ManualSftpError:
    """Build the stable invalid delete-plan failure."""

    return ManualSftpError("SFTP_DELETE_PLAN_INVALID", message)


__all__ = [
    "DeletePlanRecord",
    "ManualSftpOperationStore",
    "RemoteOperationRecord",
]
