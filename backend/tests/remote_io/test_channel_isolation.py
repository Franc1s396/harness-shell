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
    """通过 Queue 提供可由测试精确控制的异步二进制流。"""

    def __init__(self) -> None:
        """创建空的字节 chunk 队列。"""

        self.queue: asyncio.Queue[bytes] = asyncio.Queue()  # 等待读取的 chunk。

    async def read(self, size: int) -> bytes:
        """等待并返回测试注入的下一个 chunk。"""

        return await self.queue.get()


class PtyWriter:
    """把 PTY 输入回显到同一 PTY stdout 的写端替身。"""

    def __init__(self, process) -> None:
        """绑定拥有 stdout 和关闭逻辑的 PTY 进程。"""

        self.process = process  # 接收写入和 EOF 的 PTY 进程。

    def write(self, value: bytes) -> None:
        """给输入加 PTY 标记后放入该进程自己的 stdout。"""

        self.process.stdout.queue.put_nowait(b"pty:" + value)

    def write_eof(self) -> None:
        """通过关闭进程模拟终端 EOF。"""

        self.process.close()


class PtyProcess:
    """拥有独立流和关闭事件的交互式 PTY 进程替身。"""

    def __init__(self) -> None:
        """创建互不共享的 stdin/stdout/stderr 与关闭状态。"""

        self.stdout = QueueReader()  # PTY 标准输出。
        self.stderr = QueueReader()  # PTY 标准错误。
        self.stdin = PtyWriter(self)  # PTY 输入写端。
        self.exit_status = 0  # 模拟正常退出码。
        self.exit_signal = None  # 正常退出无信号。
        self.closed = False  # 是否收到关闭请求。
        self._closed = asyncio.Event()  # wait_closed 的同步事件。

    def change_terminal_size(self, cols: int, rows: int) -> None:
        """接受尺寸变更；隔离测试不关心具体值。"""

        pass

    def close(self) -> None:
        """幂等关闭两个输出流并唤醒等待者。"""

        if self.closed:
            return
        self.closed = True
        self.stdout.queue.put_nowait(b"")
        self.stderr.queue.put_nowait(b"")
        self._closed.set()

    async def wait_closed(self) -> None:
        """等待进程关闭事件。"""

        await self._closed.wait()


class GatedReader:
    """在共享 Gate 开启后仅发送一次固定正文的流替身。"""

    def __init__(self, gate: asyncio.Event, payload: bytes) -> None:
        """绑定释放事件和首次读取时返回的正文。"""

        self.gate = gate  # 控制读取何时解除阻塞。
        self.payload = payload  # 首次读取返回的字节。
        self.sent = False  # 正文是否已经发送。

    async def read(self, size: int) -> bytes:
        """等待 Gate，首次返回正文，之后返回 EOF。"""

        await self.gate.wait()
        if self.sent:
            return b""
        self.sent = True
        return self.payload


class ExecProcess:
    """输出受独立 Gate 控制的远端 exec 进程替身。"""

    def __init__(self) -> None:
        """创建与 PTY/SFTP 无关的执行 Gate 和输出流。"""

        self.gate = asyncio.Event()  # 控制 exec 输出和结束。
        self.stdout = GatedReader(self.gate, b"agent-output")  # exec 标准输出。
        self.stderr = GatedReader(self.gate, b"")  # exec 标准错误。
        self.exit_status = 0  # 模拟正常退出码。
        self.exit_signal = None  # 正常退出无信号。
        self.closed = False  # 是否收到关闭请求。

    def close(self) -> None:
        """记录关闭并释放 exec Gate。"""

        self.closed = True
        self.gate.set()

    async def wait_closed(self) -> None:
        """等待 exec Gate 释放。"""

        await self.gate.wait()


class GatedFile:
    """读取动作受所属 SFTP Client Gate 控制的文件替身。"""

    def __init__(self, client) -> None:
        """绑定提供独立 Gate 的 SFTP Client。"""

        self.client = client  # 拥有读取 Gate 的 Client。

    async def __aenter__(self):
        """进入异步文件上下文并返回自身。"""

        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """退出上下文；关闭由 Client 生命周期测试覆盖。"""

        pass

    async def seek(self, offset: int) -> None:
        """接受偏移；隔离测试固定从零读取。"""

        pass

    async def read(self, size: int) -> bytes:
        """等待 SFTP Gate 后返回固定文件字节。"""

        await self.client.gate.wait()
        return b"sftp-bytes"


class SftpClient:
    """拥有独立读取 Gate 和关闭状态的 SFTP channel 替身。"""

    def __init__(self) -> None:
        """创建尚未释放且尚未关闭的 SFTP channel。"""

        self.gate = asyncio.Event()  # 控制文件读取和关闭完成。
        self.closed = False  # exit 是否被调用。

    async def lstat(self, path: str):
        """返回固定普通文件属性。"""

        return SimpleNamespace(
            size=10,
            permissions=stat.S_IFREG | 0o644,
            mtime=0,
            mtime_ns=0,
        )

    async def open(self, path: str, mode: str):
        """返回绑定当前 Client Gate 的文件替身。"""

        return GatedFile(self)

    def exit(self) -> None:
        """记录关闭并释放 SFTP Gate。"""

        self.closed = True
        self.gate.set()

    async def wait_closed(self) -> None:
        """等待 SFTP Gate 释放。"""

        await self.gate.wait()


class Connection:
    """同时提供相互独立 PTY、exec 和 SFTP channel 的连接替身。"""

    def __init__(self) -> None:
        """为三种远端操作各创建一个身份不同的资源。"""

        self.pty = PtyProcess()  # 交互式 PTY channel。
        self.exec = ExecProcess()  # 非交互命令 channel。
        self.sftp = SftpClient()  # 只读 SFTP channel。

    async def create_process(self, *args, **options):
        """按是否提供命令位置参数返回 exec 或 PTY 进程。"""

        return self.exec if args else self.pty

    async def start_sftp_client(self):
        """返回独立 SFTP Client。"""

        return self.sftp

    def close(self) -> None:
        """模拟无需额外关闭动作的 SSH 主连接。"""

        pass

    async def wait_closed(self) -> None:
        """模拟已立即关闭的 SSH 主连接。"""

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
