"""Upload and download state-machine tests for manual SFTP."""

from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncssh
import pytest
from asyncssh.constants import FXR_ATOMIC, FXR_OVERWRITE

from harness_shell_sidecar.manual_sftp.channels import SftpChannelFactory
from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.operation_store import RemoteOperationStore
from harness_shell_sidecar.manual_sftp.transfers import DownloadManager, UploadManager
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase


CONNECTION_ID = UUID("00000000-0000-4000-8000-000000000311")
TARGET_PATH = "/home/demo/data.bin"


class FakeFileHandle:
    """Sequential public SFTP file-handle API over shared bytes."""

    def __init__(self, remote: "FakeRemote", path: str, mode: str) -> None:
        self.remote = remote
        self.path = path
        self.mode = mode
        self.offset = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        await self.close()
        return False

    async def read(self, length: int) -> bytes:
        payload = self.remote.files[self.path]
        chunk = payload[self.offset : self.offset + length]
        self.offset += len(chunk)
        return chunk

    async def write(self, payload: bytes) -> None:
        self.remote.files[self.path] += payload
        self.offset += len(payload)

    async def close(self) -> None:
        self.closed = True


class FakeSftpClient:
    """One isolated channel over shared deterministic remote state."""

    version = 3

    def __init__(self, remote: "FakeRemote") -> None:
        self.remote = remote
        self.exited = False

    async def lstat(self, path: bytes):
        decoded = path.decode("utf-8")
        if decoded not in self.remote.files:
            raise asyncssh.SFTPNoSuchFile("missing")
        return SimpleNamespace(
            permissions=stat.S_IFREG | 0o644,
            size=len(self.remote.files[decoded]),
            mtime=self.remote.mtime,
            mtime_ns=0,
        )

    async def open(self, path: bytes, mode: str) -> FakeFileHandle:
        decoded = path.decode("utf-8")
        if mode == "xb":
            if decoded in self.remote.files:
                raise asyncssh.SFTPFileAlreadyExists("exists")
            self.remote.files[decoded] = b""
        elif mode == "rb" and decoded not in self.remote.files:
            raise asyncssh.SFTPNoSuchFile("missing")
        return FakeFileHandle(self.remote, decoded, mode)

    async def remove(self, path: bytes) -> None:
        decoded = path.decode("utf-8")
        self.remote.remove_calls.append(decoded)
        if self.remote.remove_fails:
            raise asyncssh.SFTPFailure("remove failed")
        self.remote.files.pop(decoded, None)

    async def rename(self, source: bytes, target: bytes, *, flags: int) -> None:
        source_path = source.decode("utf-8")
        target_path = target.decode("utf-8")
        self.remote.rename_calls.append((source_path, target_path, flags))
        if self.remote.atomic_unsupported:
            raise asyncssh.SFTPOpUnsupported("atomic rename unsupported")
        if self.remote.create_target_before_rename:
            self.remote.files[target_path] = b"external writer"
            self.remote.create_target_before_rename = False
        if flags == 0 and target_path in self.remote.files:
            raise asyncssh.SFTPFileAlreadyExists("exists")
        self.remote.files[target_path] = self.remote.files.pop(source_path)

    def exit(self) -> None:
        self.exited = True

    async def wait_closed(self) -> None:
        return None


class FakeConnection:
    """Create a fresh SFTP client for each isolated operation channel."""

    def __init__(self, remote: "FakeRemote") -> None:
        self.remote = remote

    async def start_sftp_client(self, **options) -> FakeSftpClient:
        assert options == {"path_encoding": None}
        return FakeSftpClient(self.remote)


class FakeRemote:
    """Shared remote filesystem state and observable mutation calls."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.mtime = 1_770_000_000
        self.remove_calls: list[str] = []
        self.rename_calls: list[tuple[str, str, int]] = []
        self.remove_fails = False
        self.atomic_unsupported = False
        self.create_target_before_rename = False


def managers(tmp_path: Path, remote: FakeRemote):
    """Build transfer managers bound to one live SSH session."""

    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    records = RemoteOperationStore(EncryptedRecordStore(database, b"d" * 32))
    sessions = SshSessionRegistry()
    owner = sessions.register(
        CONNECTION_ID,
        FakeConnection(remote),
        connection_profile_version=1,
        host_label="demo-host",
        target_host_key_fingerprint="SHA256:test-target",
    )
    channels = SftpChannelFactory(sessions)
    return (
        database,
        owner,
        UploadManager(channels, records),
        DownloadManager(channels),
    )


def test_upload_rejects_out_of_order_chunk_without_committing(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeRemote()
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            prepared = await uploads.begin(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=snapshot,
            )
            with pytest.raises(
                ManualSftpError, match="SFTP_PROTOCOL_SEQUENCE_INVALID"
            ):
                await uploads.write_chunk(
                    operation_id=prepared.operation_id,
                    sequence=1,
                    offset=0,
                    chunk=b"payload",
                )
            assert remote.rename_calls == []
            assert remote.remove_calls == [prepared.temp_path]
        finally:
            database.close()

    asyncio.run(scenario())


def test_upload_finish_requests_atomic_overwrite_without_fallback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeRemote({TARGET_PATH: b"before"})
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            operation_id = uuid4()
            prepared = await uploads.begin(
                operation_id=operation_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=snapshot,
            )
            await uploads.write_chunk(
                operation_id=operation_id,
                sequence=0,
                offset=0,
                chunk=b"payload",
            )
            terminal = await uploads.finish(operation_id)

            assert terminal.state == "succeeded"
            assert remote.rename_calls == [
                (prepared.temp_path, TARGET_PATH, FXR_ATOMIC | FXR_OVERWRITE)
            ]
            assert remote.files[TARGET_PATH] == b"payload"
        finally:
            database.close()

    asyncio.run(scenario())


def test_upload_finish_uses_v3_no_clobber_rename_when_target_was_absent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeRemote()
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            operation_id = uuid4()
            prepared = await uploads.begin(
                operation_id=operation_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=snapshot,
            )
            await uploads.write_chunk(
                operation_id=operation_id,
                sequence=0,
                offset=0,
                chunk=b"payload",
            )

            terminal = await uploads.finish(operation_id)

            assert terminal.state == "succeeded"
            assert remote.rename_calls == [(prepared.temp_path, TARGET_PATH, 0)]
        finally:
            database.close()

    asyncio.run(scenario())


def test_v3_no_clobber_commit_preserves_a_target_created_after_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeRemote()
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            operation_id = uuid4()
            prepared = await uploads.begin(
                operation_id=operation_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=snapshot,
            )
            await uploads.write_chunk(
                operation_id=operation_id,
                sequence=0,
                offset=0,
                chunk=b"payload",
            )
            remote.create_target_before_rename = True

            with pytest.raises(ManualSftpError) as raised:
                await uploads.finish(operation_id)

            assert raised.value.error_code == "SFTP_TARGET_CHANGED"
            assert remote.files[TARGET_PATH] == b"external writer"
            assert prepared.temp_path not in remote.files
        finally:
            database.close()

    asyncio.run(scenario())


def test_download_detects_remote_snapshot_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeRemote({TARGET_PATH: b"payload"})
        database, owner, _uploads, downloads = managers(tmp_path, remote)
        try:
            transfer = await downloads.begin(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
            )
            chunk = await downloads.read_chunk(
                transfer.operation_id, sequence=0, offset=0
            )
            assert chunk.chunk_b64 == "cGF5bG9hZA=="
            remote.mtime += 1
            with pytest.raises(ManualSftpError, match="SFTP_TARGET_CHANGED"):
                await downloads.finish(transfer.operation_id)
        finally:
            database.close()

    asyncio.run(scenario())


def test_upload_operation_id_is_never_reused_after_begin_or_abort(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        remote = FakeRemote()
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        operation_id = uuid4()
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            await uploads.begin(
                operation_id=operation_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"").hexdigest(),
                source_byte_count=0,
                target_snapshot=snapshot,
            )
            with pytest.raises(ManualSftpError, match="SFTP_OPERATION_DUPLICATE"):
                await uploads.begin(
                    operation_id=operation_id,
                    ssh_session_id=owner.ssh_session_id,
                    path=TARGET_PATH,
                    source_sha256=hashlib.sha256(b"").hexdigest(),
                    source_byte_count=0,
                    target_snapshot=snapshot,
                )
            await uploads.abort(operation_id)
            with pytest.raises(
                ManualSftpError, match="SFTP_OPERATION_ALREADY_FINALIZED"
            ):
                await uploads.begin(
                    operation_id=operation_id,
                    ssh_session_id=owner.ssh_session_id,
                    path=TARGET_PATH,
                    source_sha256=hashlib.sha256(b"").hexdigest(),
                    source_byte_count=0,
                    target_snapshot=snapshot,
                )
        finally:
            database.close()

    asyncio.run(scenario())


def test_transfer_chunk_requests_use_thirty_second_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload and download chunks have their own 30-second request deadline."""

    observed_deadlines: list[int] = []

    class RecordedTimeout:
        """Record requested deadlines while allowing the real fake I/O to complete."""

        def __init__(self, seconds: int) -> None:
            self._seconds = seconds

        async def __aenter__(self) -> None:
            observed_deadlines.append(self._seconds)

        async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
            return False

    async def scenario() -> None:
        monkeypatch.setattr(
            "harness_shell_sidecar.manual_sftp.transfers.asyncio.timeout",
            lambda seconds: RecordedTimeout(seconds),
        )
        remote = FakeRemote({TARGET_PATH: b"payload"})
        database, owner, uploads, downloads = managers(tmp_path, remote)
        try:
            upload_snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            upload_id = uuid4()
            await uploads.begin(
                operation_id=upload_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=upload_snapshot,
            )
            observed_deadlines.clear()
            await uploads.write_chunk(
                operation_id=upload_id, sequence=0, offset=0, chunk=b"payload"
            )
            assert observed_deadlines == [30]

            download = await downloads.begin(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
            )
            observed_deadlines.clear()
            await downloads.read_chunk(download.operation_id, sequence=0, offset=0)
            assert observed_deadlines == [30]
        finally:
            database.close()

    asyncio.run(scenario())


def test_atomic_unsupported_never_retries_with_weaker_rename(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeRemote({TARGET_PATH: b"before"})
        remote.atomic_unsupported = True
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            operation_id = uuid4()
            prepared = await uploads.begin(
                operation_id=operation_id,
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"payload").hexdigest(),
                source_byte_count=7,
                target_snapshot=snapshot,
            )
            await uploads.write_chunk(
                operation_id=operation_id,
                sequence=0,
                offset=0,
                chunk=b"payload",
            )
            with pytest.raises(
                ManualSftpError, match="SFTP_ATOMIC_REPLACE_UNSUPPORTED"
            ):
                await uploads.finish(operation_id)
            assert remote.rename_calls == [
                (prepared.temp_path, TARGET_PATH, FXR_ATOMIC | FXR_OVERWRITE)
            ]
            assert prepared.temp_path not in remote.files
            assert remote.files[TARGET_PATH] == b"before"
        finally:
            database.close()

    asyncio.run(scenario())


def test_abort_cleanup_failure_is_explicit_and_retains_temp(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeRemote()
        database, owner, uploads, _downloads = managers(tmp_path, remote)
        try:
            snapshot = await uploads.preflight(owner.ssh_session_id, TARGET_PATH)
            prepared = await uploads.begin(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
                source_sha256=hashlib.sha256(b"").hexdigest(),
                source_byte_count=0,
                target_snapshot=snapshot,
            )
            remote.remove_fails = True
            with pytest.raises(
                ManualSftpError, match="SFTP_UPLOAD_CLEANUP_REQUIRED"
            ):
                await uploads.abort(prepared.operation_id)
            assert prepared.temp_path in remote.files
        finally:
            database.close()

    asyncio.run(scenario())


def test_zero_byte_download_finishes_without_empty_chunk(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = FakeRemote({TARGET_PATH: b""})
        database, owner, _uploads, downloads = managers(tmp_path, remote)
        try:
            transfer = await downloads.begin(
                operation_id=uuid4(),
                ssh_session_id=owner.ssh_session_id,
                path=TARGET_PATH,
            )
            assert transfer.byte_count == 0
            terminal = await downloads.finish(transfer.operation_id)
            assert terminal.state == "succeeded"
            assert terminal.sha256 == hashlib.sha256(b"").hexdigest()
        finally:
            database.close()

    asyncio.run(scenario())


def test_download_operation_id_is_already_finalized_after_terminal_state(
    tmp_path: Path,
) -> None:
    """Finished and aborted download IDs differ from active duplicate requests."""

    async def scenario() -> None:
        for terminal_action in ("finish", "abort"):
            remote = FakeRemote({TARGET_PATH: b"payload"})
            database, owner, _uploads, downloads = managers(
                tmp_path / terminal_action, remote
            )
            operation_id = uuid4()
            try:
                transfer = await downloads.begin(
                    operation_id=operation_id,
                    ssh_session_id=owner.ssh_session_id,
                    path=TARGET_PATH,
                )
                if terminal_action == "finish":
                    await downloads.read_chunk(
                        transfer.operation_id, sequence=0, offset=0
                    )
                    await downloads.finish(operation_id)
                else:
                    await downloads.abort(operation_id)
                with pytest.raises(ManualSftpError) as raised:
                    await downloads.begin(
                        operation_id=operation_id,
                        ssh_session_id=owner.ssh_session_id,
                        path=TARGET_PATH,
                    )
                assert raised.value.error_code == "SFTP_OPERATION_ALREADY_FINALIZED"
            finally:
                database.close()

    asyncio.run(scenario())
