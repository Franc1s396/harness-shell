from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from harness_shell_sidecar.remote_io import (
    ArtifactStore,
    RemoteExecRequest,
    RemoteExecutor,
    RemoteSftp,
)
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase
from harness_shell_sidecar.terminal import PtyManager


class QueueReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, size: int) -> bytes:
        return await self.queue.get()


class PtyWriter:
    def __init__(self, process) -> None:
        self.process = process

    def write(self, value: bytes) -> None:
        self.process.stdout.queue.put_nowait(b"pty:" + value)

    def write_eof(self) -> None:
        self.process.close()


class PtyProcess:
    def __init__(self) -> None:
        self.stdout = QueueReader()
        self.stderr = QueueReader()
        self.stdin = PtyWriter(self)
        self.exit_status = 0
        self.exit_signal = None
        self.closed = False
        self._closed = asyncio.Event()

    def change_terminal_size(self, cols: int, rows: int) -> None:
        pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stdout.queue.put_nowait(b"")
        self.stderr.queue.put_nowait(b"")
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()


class GatedReader:
    def __init__(self, gate: asyncio.Event, payload: bytes) -> None:
        self.gate = gate
        self.payload = payload
        self.sent = False

    async def read(self, size: int) -> bytes:
        await self.gate.wait()
        if self.sent:
            return b""
        self.sent = True
        return self.payload


class ExecProcess:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.stdout = GatedReader(self.gate, b"agent-output")
        self.stderr = GatedReader(self.gate, b"")
        self.exit_status = 0
        self.exit_signal = None
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.gate.set()

    async def wait_closed(self) -> None:
        await self.gate.wait()


class GatedFile:
    def __init__(self, client) -> None:
        self.client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        pass

    async def seek(self, offset: int) -> None:
        pass

    async def read(self, size: int) -> bytes:
        await self.client.gate.wait()
        return b"sftp-bytes"


class SftpClient:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.closed = False

    async def lstat(self, path: str):
        return SimpleNamespace(
            size=10,
            permissions=stat.S_IFREG | 0o644,
            mtime=0,
            mtime_ns=0,
        )

    async def open(self, path: str, mode: str):
        return GatedFile(self)

    def exit(self) -> None:
        self.closed = True
        self.gate.set()

    async def wait_closed(self) -> None:
        await self.gate.wait()


class Connection:
    def __init__(self) -> None:
        self.pty = PtyProcess()
        self.exec = ExecProcess()
        self.sftp = SftpClient()

    async def create_process(self, *args, **options):
        return self.exec if args else self.pty

    async def start_sftp_client(self):
        return self.sftp

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def wait_until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_pty_exec_and_sftp_channels_are_identity_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
        records = EncryptedRecordStore(database, b"i" * 32)
        artifacts = ArtifactStore(database, records)
        sessions = SshSessionRegistry()
        connection = Connection()
        ssh_session = sessions.register(uuid4(), connection)
        events: list[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        terminal = PtyManager(sessions, event_listener=emit)
        executor = RemoteExecutor(sessions, artifacts)
        sftp = RemoteSftp(sessions, artifacts)
        try:
            pty = await terminal.open(ssh_session.ssh_session_id, cols=80, rows=24)
            exec_task = asyncio.create_task(
                executor.run(
                    RemoteExecRequest(
                        operation_id=uuid4(),
                        ssh_session_id=ssh_session.ssh_session_id,
                        command="agent-command",
                        timeout_ms=60_000,
                        output_budget_bytes=1024,
                    )
                )
            )
            sftp_task = asyncio.create_task(
                sftp.read_range(
                    ssh_session.ssh_session_id,
                    "/remote/file",
                    offset=0,
                    length=10,
                )
            )
            await wait_until(lambda: len(ssh_session.child_channels) == 3)
            assert len({id(channel) for channel in ssh_session.child_channels}) == 3

            nonce = b"nonce-71a"
            await terminal.write(pty.pty_session_id, nonce)
            await wait_until(
                lambda: any(event["event"] == "ssh.pty.output" for event in events)
            )
            transcript = b"".join(
                __import__("base64").b64decode(event["data_b64"])
                for event in events
                if event["event"] == "ssh.pty.output"
            )
            assert nonce in transcript
            assert b"agent-command" not in transcript

            await terminal.close(pty.pty_session_id)
            assert not exec_task.done()
            assert not sftp_task.done()

            connection.exec.gate.set()
            connection.sftp.gate.set()
            await exec_task
            await sftp_task
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())


def test_disconnect_closes_pty_exec_and_sftp_channel_types(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
        records = EncryptedRecordStore(database, b"j" * 32)
        artifacts = ArtifactStore(database, records)
        sessions = SshSessionRegistry()
        connection = Connection()
        ssh_session = sessions.register(uuid4(), connection)

        async def emit(event: dict) -> None:
            pass

        terminal = PtyManager(sessions, event_listener=emit)
        executor = RemoteExecutor(sessions, artifacts)
        sftp = RemoteSftp(sessions, artifacts)
        try:
            await terminal.open(ssh_session.ssh_session_id, cols=80, rows=24)
            exec_task = asyncio.create_task(
                executor.run(
                    RemoteExecRequest(
                        operation_id=uuid4(),
                        ssh_session_id=ssh_session.ssh_session_id,
                        command="agent-command",
                        timeout_ms=60_000,
                        output_budget_bytes=1024,
                    )
                )
            )
            sftp_task = asyncio.create_task(
                sftp.read_range(
                    ssh_session.ssh_session_id,
                    "/remote/file",
                    offset=0,
                    length=10,
                )
            )
            await wait_until(lambda: len(ssh_session.child_channels) == 3)
            await sessions.close(ssh_session.ssh_session_id)
            await asyncio.gather(exec_task, sftp_task)
            assert connection.pty.closed is True
            assert connection.exec.closed is True
            assert connection.sftp.closed is True
            assert len(sessions) == 0
        finally:
            records.zeroize()
            database.close()

    asyncio.run(scenario())
