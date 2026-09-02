from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from harness_shell_sidecar.storage import (
    AuditEvent,
    AuditLedger,
    RuntimeDatabase,
)


CORRELATION_ID = UUID("018f3f83-7a53-7b5d-9c4e-1b2f68e27911")


def open_ledger(
    tmp_path: Path, key: bytes = b"a" * 32
) -> tuple[RuntimeDatabase, AuditLedger]:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    return database, AuditLedger(database, key)


def test_audit_chain_detects_body_tampering(tmp_path: Path) -> None:
    database, ledger = open_ledger(tmp_path)
    try:
        first = ledger.append(
            AuditEvent.runtime_started(correlation_id=CORRELATION_ID)
        )
        second = ledger.append(
            AuditEvent.runtime_ready(correlation_id=CORRELATION_ID)
        )
        assert first.sequence == 1
        assert second.sequence == 2
        assert ledger.verify_chain().valid is True

        database.execute(
            "UPDATE audit_entries SET body_json = ? WHERE event_id = ?",
            ('{"state":"forged"}', str(second.event_id)),
        )

        verification = ledger.verify_chain()
        assert verification.valid is False
        assert verification.first_invalid_sequence == second.sequence
    finally:
        ledger.zeroize()
        database.close()


def test_audit_chain_detects_wrong_key(tmp_path: Path) -> None:
    database, ledger = open_ledger(tmp_path)
    try:
        ledger.append(AuditEvent.runtime_started(correlation_id=CORRELATION_ID))

        verification = AuditLedger(database, b"x" * 32).verify_chain()

        assert verification.valid is False
        assert verification.first_invalid_sequence == 1
    finally:
        ledger.zeroize()
        database.close()


def test_audit_chain_detects_deleted_middle_entry(tmp_path: Path) -> None:
    database, ledger = open_ledger(tmp_path)
    try:
        ledger.append(AuditEvent.runtime_started(correlation_id=CORRELATION_ID))
        middle = ledger.append(
            AuditEvent.runtime_ready(correlation_id=CORRELATION_ID)
        )
        ledger.append(AuditEvent.runtime_stopped(correlation_id=CORRELATION_ID))
        database.execute(
            "DELETE FROM audit_entries WHERE event_id = ?", (str(middle.event_id),)
        )

        verification = ledger.verify_chain()

        assert verification.valid is False
        assert verification.first_invalid_sequence == 3
    finally:
        ledger.zeroize()
        database.close()


def test_runtime_failure_requires_a_safe_error_code() -> None:
    with pytest.raises(ValueError, match="error code"):
        AuditEvent.runtime_failed(
            correlation_id=CORRELATION_ID,
            error_code="token=plaintext-secret",
        )


def test_audit_body_is_canonical_json(tmp_path: Path) -> None:
    database, ledger = open_ledger(tmp_path)
    try:
        ledger.append(
            AuditEvent.runtime_paused(
                correlation_id=CORRELATION_ID,
                error_code="SIDECAR_EXITED",
            )
        )

        body = database.execute(
            "SELECT body_json FROM audit_entries WHERE sequence = 1"
        ).fetchone()[0]

        assert body == '{"error_code":"SIDECAR_EXITED","state":"PAUSED"}'
    finally:
        ledger.zeroize()
        database.close()

