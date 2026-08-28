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
    """支持异步上下文、seek 和 read 的内存远端文件替身。"""

    def __init__(self, payload: bytes) -> None:
        """保存不可变正文并把游标置于文件开头。"""

        self.payload = payload  # 模拟远端文件的完整内容。
        self.offset = 0  # 下一次读取的字节偏移量。
        self.closed = False  # 是否退出异步上下文。
        self.read_sizes: list[int] = []  # 每次 read 请求的大小。

    async def __aenter__(self):
        """进入异步文件上下文并返回自身。"""

        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """记录远端文件句柄已关闭。"""

        self.closed = True

    async def seek(self, offset: int) -> None:
        """把内存读取游标移动到指定偏移。"""

        self.offset = offset

    async def read(self, size: int) -> bytes:
        """从当前游标返回最多 size 字节并推进游标。"""

        self.read_sizes.append(size)
        value = self.payload[self.offset : self.offset + size]
        self.offset += len(value)
        return value


@dataclass
class FakeName:
    """模拟 AsyncSSH scandir 返回的名称与属性组合。"""

    #: 目录项名称。
    filename: str
    #: 目录项的远端 stat 属性替身。
    attrs: object


class FakeSftpClient:
    """记录只读 SFTP 调用并返回内存文件的 Client 替身。"""

    def __init__(self, payload: bytes, *, symlink: bool = False) -> None:
        """创建普通文件或符号链接属性及空调用记录。"""

        mode = stat.S_IFLNK | 0o777 if symlink else stat.S_IFREG | 0o644
        self.attrs = SimpleNamespace(  # 所有测试路径共用的远端属性。
            size=len(payload), permissions=mode, mtime=123, mtime_ns=None
        )
        self.payload = payload  # open 返回的内存文件内容。
        self.lstat_calls: list[str] = []  # lstat 查询路径。
        self.open_calls: list[tuple[str, str]] = []  # 文件打开路径与模式。
        self.exited = False  # exit 是否被调用。
        self.waited = False  # wait_closed 是否被等待。

    async def lstat(self, path: str):
        """记录路径并返回预设属性。"""

        self.lstat_calls.append(path)
        return self.attrs

    async def open(self, path: str, mode: str):
        """记录打开参数并返回新的内存文件句柄。"""

        self.open_calls.append((path, mode))
        return FakeFile(self.payload)

    async def scandir(self, path: str):
        """异步产生三个具有相同属性的目录项。"""

        for index in range(3):
            yield FakeName(f"item-{index}", self.attrs)

    def exit(self) -> None:
        """记录 Client 已收到关闭请求。"""

        self.exited = True

    async def wait_closed(self) -> None:
        """记录调用方等待了 Client 关闭。"""

        self.waited = True


class FakeConnection:
    """每次调用返回下一个独立 SFTP Client 的 SSH 连接替身。"""

    def __init__(self, clients: list[FakeSftpClient]) -> None:
        """复制待返回 Client 列表并初始化已打开记录。"""

        self.clients = list(clients)  # 尚未被分配的 Client。
        self.opened: list[FakeSftpClient] = []  # 已按调用顺序返回的 Client。

    async def start_sftp_client(self):
        """返回下一个 Client，并记录 channel 隔离证据。"""

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
