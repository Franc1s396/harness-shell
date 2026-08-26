"""Immutable encrypted Artifact storage."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from uuid import UUID, uuid4

from harness_shell_sidecar.storage import EncryptedRecord, EncryptedRecordStore
from harness_shell_sidecar.storage.crypto import RecordAuthenticationFailed
from harness_shell_sidecar.storage.database import RuntimeDatabase

from .models import ArtifactReference


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(
        self, database: RuntimeDatabase, record_store: EncryptedRecordStore
    ) -> None:
        self._database = database
        self._records = record_store

    def put(
        self,
        payload: bytes,
        *,
        media_type: str,
        sensitivity: str,
        complete: bool,
        artifact_id: UUID | None = None,
    ) -> ArtifactReference:
        artifact_id = artifact_id or uuid4()
        record_id = str(artifact_id)
        digest = hashlib.sha256(payload).hexdigest()
        if not media_type or sensitivity not in {"normal", "sensitive"}:
            raise ValueError("artifact metadata is invalid")
        existing = self._database.execute(
            "SELECT 1 FROM artifact_metadata WHERE artifact_id = ? "
            "UNION ALL SELECT 1 FROM encrypted_records "
            "WHERE record_type = 'artifact' AND record_id = ? LIMIT 1",
            (record_id, record_id),
        ).fetchone()
        if existing is not None:
            raise ArtifactIntegrityError("ARTIFACT_ALREADY_EXISTS")

        connection = self._database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._records.put(
                EncryptedRecord(
                    record_type="artifact",
                    record_id=record_id,
                    schema_version=1,
                    payload=payload,
                )
            )
            self._database.execute(
                """
                INSERT INTO artifact_metadata(
                    artifact_id, sha256, byte_count, media_type,
                    sensitivity, complete, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    digest,
                    len(payload),
                    media_type,
                    sensitivity,
                    int(complete),
                    _utc_now(),
                ),
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
        return ArtifactReference(
            artifact_id=artifact_id,
            sha256=digest,
            byte_count=len(payload),
            media_type=media_type,
            sensitivity=sensitivity,
            encrypted=True,
            complete=complete,
        )

    def get(self, artifact_id: UUID) -> bytes:
        reference = self.reference(artifact_id)
        try:
            record = self._records.get("artifact", str(artifact_id))
        except RecordAuthenticationFailed as exc:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED") from exc
        if record is None:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED")
        digest = hashlib.sha256(record.payload).hexdigest()
        if (
            not hmac.compare_digest(digest, reference.sha256)
            or len(record.payload) != reference.byte_count
        ):
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED")
        return record.payload

    def reference(self, artifact_id: UUID) -> ArtifactReference:
        row = self._database.execute(
            """
            SELECT sha256, byte_count, media_type, sensitivity, complete
            FROM artifact_metadata WHERE artifact_id = ?
            """,
            (str(artifact_id),),
        ).fetchone()
        if row is None:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED")
        sha256, byte_count, media_type, sensitivity, complete = row
        return ArtifactReference(
            artifact_id=artifact_id,
            sha256=sha256,
            byte_count=byte_count,
            media_type=media_type,
            sensitivity=sensitivity,
            encrypted=True,
            complete=bool(complete),
        )

    def self_check(self) -> None:
        metadata_ids = {
            row[0]
            for row in self._database.execute(
                "SELECT artifact_id FROM artifact_metadata"
            ).fetchall()
        }
        payload_ids = {
            row[0]
            for row in self._database.execute(
                "SELECT record_id FROM encrypted_records "
                "WHERE record_type = 'artifact'"
            ).fetchall()
        }
        if metadata_ids != payload_ids:
            raise ArtifactIntegrityError("ARTIFACT_INTEGRITY_FAILED")
        for artifact_id in metadata_ids:
            self.get(UUID(artifact_id))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
