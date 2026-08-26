"""Append-only, tamper-evident local audit ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .crypto import require_data_key
from .database import RuntimeDatabase


_GENESIS_HMAC = bytes(32)
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_type: str
    actor: str
    correlation_id: UUID
    body: dict[str, str]
    task_id: UUID | None = None
    workflow_run_id: UUID | None = None

    @classmethod
    def runtime_started(cls, *, correlation_id: UUID) -> AuditEvent:
        return cls(
            "runtime.started", "sidecar", correlation_id, {"state": "STARTING"}
        )

    @classmethod
    def sidecar_ready(cls, *, correlation_id: UUID) -> AuditEvent:
        return cls(
            "runtime.ready", "sidecar", correlation_id, {"state": "READY"}
        )

    @classmethod
    def runtime_stopped(cls, *, correlation_id: UUID) -> AuditEvent:
        return cls(
            "runtime.stopped", "sidecar", correlation_id, {"state": "STOPPED"}
        )

    @classmethod
    def runtime_paused(
        cls, *, correlation_id: UUID, error_code: str
    ) -> AuditEvent:
        _require_safe_error_code(error_code)
        return cls(
            "runtime.paused",
            "sidecar",
            correlation_id,
            {"state": "PAUSED", "error_code": error_code},
        )

    @classmethod
    def runtime_failed(
        cls, *, correlation_id: UUID, error_code: str
    ) -> AuditEvent:
        _require_safe_error_code(error_code)
        return cls(
            "runtime.failed",
            "sidecar",
            correlation_id,
            {"state": "FAILED", "error_code": error_code},
        )

    @classmethod
    def ssh_connect_attempt(
        cls,
        *,
        connection_id: UUID,
        correlation_id: UUID,
        attempt: int,
        outcome: str,
        error_code: str | None,
    ) -> AuditEvent:
        if attempt not in (1, 2):
            raise ValueError("SSH connect attempt must be 1 or 2")
        body = {
            "connection_id": str(connection_id),
            "attempt": str(attempt),
            "outcome": outcome,
        }
        if error_code is not None:
            _require_safe_error_code(error_code)
            body["error_code"] = error_code
        return cls("ssh.connect.attempt", "sidecar", correlation_id, body)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    sequence: int
    event_id: UUID
    timestamp: str
    event_type: str
    actor: str
    task_id: UUID | None
    workflow_run_id: UUID | None
    correlation_id: UUID
    body_json: str
    body_sha256: bytes
    previous_hmac: bytes
    entry_hmac: bytes


@dataclass(frozen=True, slots=True)
class AuditVerification:
    valid: bool
    first_invalid_sequence: int | None = None


class AuditLedger:
    def __init__(
        self, database: RuntimeDatabase, hmac_key: bytes | bytearray
    ) -> None:
        require_data_key(hmac_key)
        self._database = database
        self._hmac_key = (
            hmac_key if isinstance(hmac_key, bytearray) else bytearray(hmac_key)
        )

    def append(self, event: AuditEvent) -> AuditEntry:
        body_json = _canonical_json(event.body)
        body_sha256 = hashlib.sha256(body_json.encode("utf-8")).digest()
        event_id = uuid4()
        timestamp = _utc_now()
        connection = self._database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous = connection.execute(
                "SELECT sequence, entry_hmac FROM audit_entries ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = 1 if previous is None else previous[0] + 1
            previous_hmac = _GENESIS_HMAC if previous is None else previous[1]
            entry_hmac = self._entry_hmac(
                sequence=sequence,
                event_id=str(event_id),
                timestamp=timestamp,
                event_type=event.event_type,
                actor=event.actor,
                task_id=_uuid_text(event.task_id),
                workflow_run_id=_uuid_text(event.workflow_run_id),
                correlation_id=str(event.correlation_id),
                body_sha256=body_sha256,
                previous_hmac=previous_hmac,
            )
            connection.execute(
                """
                INSERT INTO audit_entries(
                    sequence, event_id, timestamp, event_type, actor, task_id,
                    workflow_run_id, correlation_id, body_json, body_sha256,
                    previous_hmac, entry_hmac
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    str(event_id),
                    timestamp,
                    event.event_type,
                    event.actor,
                    _uuid_text(event.task_id),
                    _uuid_text(event.workflow_run_id),
                    str(event.correlation_id),
                    body_json,
                    body_sha256,
                    previous_hmac,
                    entry_hmac,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        return AuditEntry(
            sequence,
            event_id,
            timestamp,
            event.event_type,
            event.actor,
            event.task_id,
            event.workflow_run_id,
            event.correlation_id,
            body_json,
            body_sha256,
            previous_hmac,
            entry_hmac,
        )

    def verify_chain(self) -> AuditVerification:
        rows = self._database.execute(
            """
            SELECT sequence, event_id, timestamp, event_type, actor, task_id,
                   workflow_run_id, correlation_id, body_json, body_sha256,
                   previous_hmac, entry_hmac
            FROM audit_entries
            ORDER BY sequence
            """
        ).fetchall()
        expected_sequence = 1
        expected_previous = _GENESIS_HMAC
        for row in rows:
            (
                sequence,
                event_id,
                timestamp,
                event_type,
                actor,
                task_id,
                workflow_run_id,
                correlation_id,
                body_json,
                stored_body_sha256,
                stored_previous,
                stored_entry_hmac,
            ) = row
            if sequence != expected_sequence:
                return AuditVerification(False, sequence)
            calculated_body_sha256 = hashlib.sha256(
                body_json.encode("utf-8")
            ).digest()
            calculated_entry_hmac = self._entry_hmac(
                sequence=sequence,
                event_id=event_id,
                timestamp=timestamp,
                event_type=event_type,
                actor=actor,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                correlation_id=correlation_id,
                body_sha256=calculated_body_sha256,
                previous_hmac=expected_previous,
            )
            if not (
                hmac.compare_digest(stored_body_sha256, calculated_body_sha256)
                and hmac.compare_digest(stored_previous, expected_previous)
                and hmac.compare_digest(stored_entry_hmac, calculated_entry_hmac)
            ):
                return AuditVerification(False, sequence)
            expected_previous = stored_entry_hmac
            expected_sequence += 1
        return AuditVerification(True)

    def zeroize(self) -> None:
        for index in range(len(self._hmac_key)):
            self._hmac_key[index] = 0

    def _entry_hmac(self, **fields) -> bytes:
        values = [
            b"harness-shell-audit-v1",
            str(fields["sequence"]).encode("ascii"),
            fields["event_id"].encode("utf-8"),
            fields["timestamp"].encode("utf-8"),
            fields["event_type"].encode("utf-8"),
            fields["actor"].encode("utf-8"),
            (fields["task_id"] or "").encode("utf-8"),
            (fields["workflow_run_id"] or "").encode("utf-8"),
            (fields["correlation_id"] or "").encode("utf-8"),
            fields["body_sha256"],
            fields["previous_hmac"],
        ]
        canonical = b"".join(
            len(value).to_bytes(8, "big") + value for value in values
        )
        return hmac.new(bytes(self._hmac_key), canonical, hashlib.sha256).digest()


def _canonical_json(body: dict[str, str]) -> str:
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _require_safe_error_code(error_code: str) -> None:
    if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
        raise ValueError("audit error code must use uppercase identifiers")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
