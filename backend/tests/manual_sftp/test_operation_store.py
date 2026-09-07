"""明文手动 SFTP 操作记录测试。"""

from __future__ import annotations

from ..storage_support import RepositoryClient, sql

from pathlib import Path
from uuid import UUID

from harness_shell_sidecar.manual_sftp.models import TransferSnapshot
from harness_shell_sidecar.manual_sftp.operation_store import (
    ManualSftpOperationStore,
    RemoteOperationRecord,
)
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


OPERATION_ID = UUID("00000000-0000-4000-8000-000000000301")
CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000302")


def open_store(tmp_path: Path):
    """打开明文操作存储及拥有它的数据库。"""

    database = RuntimeDatabase.open(
        (tmp_path / "runtime.sqlite3").resolve()
    )
    records = RepositoryClient(database, PlaintextRecordStore)
    return database, records, ManualSftpOperationStore(database)


def record(operation_id: UUID, state: str) -> RemoteOperationRecord:
    """为持久化测试构建严格上传操作记录。"""

    return RemoteOperationRecord(
        operation_id=operation_id,
        kind="upload",
        state=state,
        connection_id=CONNECTION_ID,
        connection_profile_version=1,
        host_label="demo-host",
        target_host_key_fingerprint="SHA256:test-target",
        jump_connection_id=None,
        jump_profile_version=None,
        jump_host_key_fingerprint=None,
        remote_path="/home/demo/data.txt",
        temp_path=f"/home/demo/.harness-shell-upload-{operation_id}.part",
        expected_sha256="0" * 64,
        target_snapshot=TransferSnapshot(
            path="/home/demo/data.txt",
            exists=False,
            entry_type=None,
            size=None,
            mtime_ns=None,
            sha256=None,
        ),
        terminal_receipt=None,
        created_at="2026-08-29T00:00:00Z",
    )


def test_operation_store_round_trips_plaintext_records_and_filters_terminal(
    tmp_path: Path,
) -> None:
    database, records, operations = open_store(tmp_path)
    active_id = OPERATION_ID
    terminal_id = UUID("00000000-0000-4000-8000-000000000303")
    try:
        operations.put(record(active_id, "transferring"))
        operations.put(record(terminal_id, "failed"))

        assert operations.get(active_id) == record(active_id, "transferring")
        assert [value.operation_id for value in operations.list_non_terminal()] == [
            active_id
        ]
        stored = records.get("manual_sftp_operation", str(active_id))
        assert stored is not None
        assert b'"kind":"upload"' in stored.payload
        assert b"/home/demo/data.txt" in stored.payload
        assert operations.delete(active_id) is True
        assert operations.get(active_id) is None
    finally:
        database.close()
