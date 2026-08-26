from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harness_shell_sidecar.remote_io import ArtifactStore
from harness_shell_sidecar.remote_io.sftp import RemoteSftp, RemoteSftpError
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase


class FakeFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.closed = False
        self.read_sizes: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    async def seek(self, offset: int) -> None:
        self.offset = offset

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        value = self.payload[self.offset : self.offset + size]
        self.offset += len(value)
        return value


@dataclass
class FakeName:
    filename: str
    attrs: object


class FakeSftpClient:
    def __init__(self, payload: bytes, *, symlink: bool = False) -> None:
        mode = stat.S_IFLNK | 0o777 if symlink else stat.S_IFREG | 0o644
        self.attrs = SimpleNamespace(
            size=len(payload), permissions=mode, mtime=123, mtime_ns=None
        )
        self.payload = payload
        self.lstat_calls: list[str] = []
        self.open_calls: list[tuple[str, str]] = []
        self.exited = False
        self.waited = False

    async def lstat(self, path: str):
        self.lstat_calls.append(path)
        return self.attrs

    async def open(self, path: str, mode: str):
        self.open_calls.append((path, mode))
        return FakeFile(self.payload)

    async def scandir(self, path: str):
        for index in range(3):
            yield FakeName(f"item-{index}", self.attrs)

    def exit(self) -> None:
        self.exited = True

    async def wait_closed(self) -> None:
        self.waited = True


class FakeConnection:
    def __init__(self, clients: list[FakeSftpClient]) -> None:
        self.clients = list(clients)
        self.opened: list[FakeSftpClient] = []

    async def start_sftp_client(self):
        client = self.clients.pop(0)
        self.opened.append(client)
        return client


def setup_sftp(tmp_path: Path, clients: list[FakeSftpClient]):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    records = EncryptedRecordStore(database, b"s" * 32)
    artifacts = ArtifactStore(database, records)
    sessions = SshSessionRegistry()
    connection = FakeConnection(clients)
    session = sessions.register(uuid4(), connection)
    return (
        database,
        records,
        artifacts,
        session,
        connection,
        RemoteSftp(sessions, artifacts),
    )


def test_readonly_sftp_operations_are_bounded_and_use_distinct_channels(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        payload = b"0123456789" * 20
        clients = [FakeSftpClient(payload) for _ in range(4)]
        database, records, artifacts, session, connection, remote = setup_sftp(
            tmp_path, clients
        )
        try:
            info = await remote.lstat(session.ssh_session_id, "/var/log/app.log")
            listing = await remote.listdir(
                session.ssh_session_id, "/var/log", max_entries=2
            )
            read = await remote.read_range(
                session.ssh_session_id,
                "/var/log/app.log",
                offset=10,
                length=25,
            )
            digest = await remote.sha256(
                session.ssh_session_id, "/var/log/app.log"
            )

            assert info.is_file is True
            assert len(listing.entries) == 2
            assert listing.truncated is True
            assert artifacts.get(read.artifact.artifact_id) == payload[10:35]
            assert read.eof is False
            assert digest.byte_count == len(payload)
            assert len(digest.sha256) == 64
            assert len({id(client) for client in connection.opened}) == 4
            assert all(client.exited and client.waited for client in connection.opened)
            assert all(mode == "rb" for client in connection.opened for _, mode in client.open_calls)
            assert len(session.child_channels) == 0
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())


def test_sftp_rejects_unsafe_inputs_and_symlinks_before_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        symlink = FakeSftpClient(b"target", symlink=True)
        database, records, _, session, connection, remote = setup_sftp(
            tmp_path, [symlink]
        )
        try:
            for path in ["relative/path", "/tmp/bad\x00path"]:
                with pytest.raises(RemoteSftpError, match="REMOTE_PATH_INVALID"):
                    await remote.read_range(
                        session.ssh_session_id, path, offset=0, length=1
                    )
            assert connection.opened == []

            with pytest.raises(RemoteSftpError, match="REMOTE_SYMLINK_FORBIDDEN"):
                await remote.read_range(
                    session.ssh_session_id, "/tmp/link", offset=0, length=1
                )
            assert symlink.lstat_calls == ["/tmp/link"]
            assert symlink.open_calls == []

            with pytest.raises(ValueError):
                await remote.listdir(
                    session.ssh_session_id, "/tmp", max_entries=1001
                )
            with pytest.raises(ValueError):
                await remote.read_range(
                    session.ssh_session_id,
                    "/tmp/file",
                    offset=0,
                    length=262_145,
                )
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())
