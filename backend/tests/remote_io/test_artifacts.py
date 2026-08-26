from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.remote_io.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase


def setup_store(tmp_path: Path):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    records = EncryptedRecordStore(database, b"d" * 32)
    return database, records, ArtifactStore(database, records)


def test_artifact_round_trip_is_encrypted_immutable_and_truthful(tmp_path: Path) -> None:
    marker = b"REMOTE-ARTIFACT-PLAINTEXT-MARKER-8a17"
    database, records, store = setup_store(tmp_path)
    try:
        reference = store.put(
            marker,
            media_type="application/octet-stream",
            sensitivity="sensitive",
            complete=True,
        )
        assert store.get(reference.artifact_id) == marker
        assert reference.byte_count == len(marker)
        assert len(reference.sha256) == 64
        assert reference.encrypted is True
        assert marker not in database.path.read_bytes()

        with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_ALREADY_EXISTS"):
            store.put(
                b"replacement",
                artifact_id=reference.artifact_id,
                media_type="application/octet-stream",
                sensitivity="sensitive",
                complete=True,
            )
    finally:
        records.zeroize()
        database.close()


@pytest.mark.parametrize("delete_payload", [True, False])
def test_artifact_self_check_rejects_orphaned_halves(
    tmp_path: Path, delete_payload: bool
) -> None:
    database, records, store = setup_store(tmp_path)
    try:
        reference = store.put(
            b"integrity",
            artifact_id=uuid4(),
            media_type="application/octet-stream",
            sensitivity="normal",
            complete=True,
        )
        if delete_payload:
            database.execute(
                "DELETE FROM encrypted_records WHERE record_type = 'artifact' "
                "AND record_id = ?",
                (str(reference.artifact_id),),
            )
        else:
            database.execute(
                "DELETE FROM artifact_metadata WHERE artifact_id = ?",
                (str(reference.artifact_id),),
            )
        with pytest.raises(ArtifactIntegrityError, match="ARTIFACT_INTEGRITY_FAILED"):
            store.self_check()
    finally:
        records.zeroize()
        database.close()
