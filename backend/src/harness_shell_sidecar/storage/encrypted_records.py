"""Encrypted record persistence over runtime SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .crypto import decrypt_payload, encrypt_payload, record_aad, require_data_key
from .database import RuntimeDatabase


@dataclass(frozen=True, slots=True)
class EncryptedRecord:
    record_type: str
    record_id: str
    schema_version: int
    payload: bytes

    def __post_init__(self) -> None:
        if not self.record_type or not self.record_id:
            raise ValueError("record type and id must not be empty")
        if self.schema_version <= 0:
            raise ValueError("record schema version must be positive")


class EncryptedRecordStore:
    def __init__(
        self, database: RuntimeDatabase, data_key: bytes | bytearray
    ) -> None:
        require_data_key(data_key)
        self._database = database
        self._data_key = (
            data_key if isinstance(data_key, bytearray) else bytearray(data_key)
        )

    @property
    def connection(self):
        return self._database.connection

    def put(self, record: EncryptedRecord) -> None:
        nonce, ciphertext = encrypt_payload(
            self._data_key,
            record.payload,
            record_aad(
                record.record_type, record.record_id, record.schema_version
            ),
        )
        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO encrypted_records(
                record_type, record_id, schema_version, nonce, ciphertext,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_type, record_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                nonce = excluded.nonce,
                ciphertext = excluded.ciphertext,
                updated_at = excluded.updated_at
            """,
            (
                record.record_type,
                record.record_id,
                record.schema_version,
                nonce,
                ciphertext,
                now,
                now,
            ),
        )

    def get(self, record_type: str, record_id: str) -> EncryptedRecord | None:
        row = self._database.execute(
            """
            SELECT schema_version, nonce, ciphertext
            FROM encrypted_records
            WHERE record_type = ? AND record_id = ?
            """,
            (record_type, record_id),
        ).fetchone()
        if row is None:
            return None
        schema_version, nonce, ciphertext = row
        payload = decrypt_payload(
            self._data_key,
            nonce,
            ciphertext,
            record_aad(record_type, record_id, schema_version),
        )
        return EncryptedRecord(record_type, record_id, schema_version, payload)

    def delete(self, record_type: str, record_id: str) -> bool:
        cursor = self._database.execute(
            "DELETE FROM encrypted_records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        return cursor.rowcount == 1

    def zeroize(self) -> None:
        for index in range(len(self._data_key)):
            self._data_key[index] = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

