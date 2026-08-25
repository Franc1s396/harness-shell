from __future__ import annotations

from pathlib import Path

import pytest

from harness_shell_sidecar.storage import (
    EncryptedRecord,
    EncryptedRecordStore,
    RecordAuthenticationFailed,
    RuntimeDatabase,
)


def open_store(
    tmp_path: Path, data_key: bytes = b"d" * 32
) -> tuple[RuntimeDatabase, EncryptedRecordStore]:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    return database, EncryptedRecordStore(database, data_key)


def test_round_trip_uses_record_identity_as_aad(tmp_path: Path) -> None:
    database, store = open_store(tmp_path)
    source = EncryptedRecord(
        "checkpoint", "cp-1", 1, b'{"state":"READY"}'
    )
    try:
        store.put(source)
        assert store.get("checkpoint", "cp-1") == source
        store.connection.execute(
            "UPDATE encrypted_records SET record_id = 'cp-2' WHERE record_id = 'cp-1'"
        )

        with pytest.raises(RecordAuthenticationFailed):
            store.get("checkpoint", "cp-2")
    finally:
        database.close()


def test_same_plaintext_uses_unique_nonce(tmp_path: Path) -> None:
    database, store = open_store(tmp_path)
    try:
        store.put(EncryptedRecord("event", "1", 1, b"same"))
        store.put(EncryptedRecord("event", "2", 1, b"same"))
        rows = store.connection.execute(
            "SELECT nonce, ciphertext FROM encrypted_records ORDER BY record_id"
        ).fetchall()

        assert rows[0][0] != rows[1][0]
        assert rows[0][1] != rows[1][1]
    finally:
        database.close()


def test_wrong_key_cannot_decrypt_record(tmp_path: Path) -> None:
    database, store = open_store(tmp_path)
    try:
        store.put(EncryptedRecord("event", "1", 1, b"payload"))
        wrong_key_store = EncryptedRecordStore(database, b"x" * 32)

        with pytest.raises(RecordAuthenticationFailed):
            wrong_key_store.get("event", "1")
    finally:
        database.close()


def test_missing_and_deleted_records_are_explicit(tmp_path: Path) -> None:
    database, store = open_store(tmp_path)
    try:
        assert store.get("event", "missing") is None
        assert store.delete("event", "missing") is False
        store.put(EncryptedRecord("event", "1", 1, b"payload"))
        assert store.delete("event", "1") is True
        assert store.get("event", "1") is None
    finally:
        database.close()


def test_store_rejects_non_256_bit_key(tmp_path: Path) -> None:
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    try:
        with pytest.raises(ValueError, match="32 bytes"):
            EncryptedRecordStore(database, b"short")
    finally:
        database.close()

