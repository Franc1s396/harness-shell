"""Plaintext runtime-record persistence without encoding or encryption."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .database import RuntimeDatabase


@dataclass(frozen=True, slots=True)
class PlaintextRecord:
    """Describe one immutable plaintext payload and its composite identity."""

    #: Business namespace used as the first half of the durable composite key.
    record_type: str
    #: Identifier unique within ``record_type``.
    record_id: str
    #: Positive version used by the owning repository to validate the payload.
    schema_version: int
    #: Unchanged plaintext bytes persisted directly in Runtime SQLite.
    payload: bytes

    def __post_init__(self) -> None:
        """Reject identities or payloads that cannot form a stable record."""

        if not self.record_type or not self.record_id:
            raise ValueError("record type and id must not be empty")
        if self.schema_version <= 0:
            raise ValueError("record schema version must be positive")
        if not isinstance(self.payload, bytes):
            raise TypeError("record payload must be bytes")


class PlaintextRecordStore:
    """Own CRUD access to schema-v6 generic plaintext runtime records."""

    _database: RuntimeDatabase

    def __init__(self, database: RuntimeDatabase) -> None:
        """Bind the store to the Runtime-owned shared database connection."""

        # The Runtime owner closes this shared connection after all repositories.
        self._database = database

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the shared connection for repository-level atomic transactions."""

        return self._database.connection

    def put(self, record: PlaintextRecord) -> None:
        """Insert or update one payload while preserving its creation timestamp."""

        now = _utc_now()
        self._database.execute(
            """
            INSERT INTO runtime_records(
                record_type, record_id, schema_version, payload,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_type, record_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                record.record_type,
                record.record_id,
                record.schema_version,
                record.payload,
                now,
                now,
            ),
        )

    def get(self, record_type: str, record_id: str) -> PlaintextRecord | None:
        """Return one exact composite-key record, or ``None`` when absent."""

        row = self._database.execute(
            """
            SELECT schema_version, payload
            FROM runtime_records
            WHERE record_type = ? AND record_id = ?
            """,
            (record_type, record_id),
        ).fetchone()
        if row is None:
            return None
        schema_version, payload = row
        return PlaintextRecord(record_type, record_id, schema_version, payload)

    def delete(self, record_type: str, record_id: str) -> bool:
        """Delete one exact composite-key record and report whether it existed."""

        cursor = self._database.execute(
            "DELETE FROM runtime_records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        return cursor.rowcount == 1

    def list_ids(self, record_type: str) -> Sequence[str]:
        """Return IDs from one namespace in stable lexical order."""

        rows = self._database.execute(
            """
            SELECT record_id
            FROM runtime_records
            WHERE record_type = ?
            ORDER BY record_id
            """,
            (record_type,),
        ).fetchall()
        return tuple(row[0] for row in rows)


def _utc_now() -> str:
    """Return a millisecond UTC timestamp for record lifecycle metadata."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
