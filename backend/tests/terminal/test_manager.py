from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from uuid import uuid4

import pytest

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.terminal.manager import PtyManager, PtyManagerError


class FakeReader:
    """从 Queue 返回测试注入二进制 chunk 的异步流替身。"""

    def __init__(self) -> None:
        """创建空的 chunk 队列。"""

        self.queue: asyncio.Queue[bytes] = asyncio.Queue()  # 等待读取的数据。

    async def read(self, size: int) -> bytes:
        """等待并返回下一段数据。"""

        return await self.queue.get()

    async def feed(self, value: bytes) -> None:
        """向读取端注入下一段数据或 EOF。"""

        await self.queue.put(value)


class FakeWriter:
    """记录 PTY 输入和 EOF 的同步写端替身。"""

    def __init__(self) -> None:
        """初始化空写入记录和未发送 EOF 状态。"""

        self.writes: list[bytes] = []  # 按顺序接收到的输入字节。
        self.eof = False  # write_eof 是否被调用。

    def write(self, value: bytes) -> None:
        """记录一段 PTY 输入。"""

        self.writes.append(value)

    def write_eof(self) -> None:
        """记录调用方请求优雅关闭输入流。"""

        self.eof = True


class FakeProcess:
    """可控制输出、退出状态和关闭时机的 PTY 进程替身。"""

    def __init__(self) -> None:
        """创建独立流、生命周期标记及终端尺寸记录。"""

        self.stdin = FakeWriter()  # PTY 输入写端。
        self.stdout = FakeReader()  # PTY 标准输出。
        self.stderr = FakeReader()  # PTY 标准错误。
        self.exit_status = None  # 可由 finish 设置的退出码。
        self.exit_signal = None  # 可由 finish 设置的退出信号。
        self.closed = False  # close 是否被调用。
        self.waited = False  # wait_closed 是否完成。
        self.sizes: list[tuple[int, int]] = []  # 应用过的终端尺寸。
        self._closed = asyncio.Event()  # 控制 wait_closed 完成。

    def change_terminal_size(self, cols: int, rows: int) -> None:
        """记录一次终端尺寸调整。"""

        self.sizes.append((cols, rows))

    def close(self) -> None:
        """幂等关闭进程、结束输出流并唤醒等待者。"""

        if self.closed:
            return
        self.closed = True
        self.stdout.queue.put_nowait(b"")
        self.stderr.queue.put_nowait(b"")
        self._closed.set()

    async def wait_closed(self) -> None:
        """等待关闭事件并记录收敛已完成。"""

        await self._closed.wait()
        self.waited = True

    async def finish(
        self, *, exit_status: int | None = 0, exit_signal: str | None = None
    ) -> None:
        """模拟远端进程自然结束并关闭两个输出流。"""

        self.exit_status = exit_status
        self.exit_signal = exit_signal
        await self.stdout.feed(b"")
        await self.stderr.feed(b"")
        self._closed.set()


@dataclass
class FakeConnection:
    """返回固定 PTY 进程并记录创建选项的 SSH 连接替身。"""

    #: create_process 返回的固定进程。
    process: FakeProcess

    def __post_init__(self) -> None:
        """初始化尚未调用 create_process 的状态。"""

        self.options: dict | None = None  # 最近一次 PTY 创建选项。

    async def create_process(self, **options):
        """记录 PTY 创建选项并返回固定进程。"""

        self.options = options
        return self.process

    def close(self) -> None:
        """模拟无需额外动作的 SSH 主连接关闭。"""

        pass

    async def wait_closed(self) -> None:
        """模拟 SSH 主连接已立即完成关闭。"""

        pass


def setup_manager():
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    process = FakeProcess()
    connection = FakeConnection(process)
    sessions = SshSessionRegistry()
    ssh_session = sessions.register(uuid4(), connection)
    manager = PtyManager(sessions, event_listener=emit)
    return manager, sessions, ssh_session, process, connection, events


async def wait_until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_open_streams_binary_bytes_in_order_and_caps_chunks() -> None:
    async def scenario() -> None:
        manager, _, ssh_session, process, connection, events = setup_manager()
        pty = await manager.open(ssh_session.ssh_session_id, cols=100, rows=30)
        assert connection.options == {
            "term_type": "xterm-256color",
            "term_size": (100, 30),
            "encoding": None,
        }

        marker = "中文🙂".encode()
        await process.stdout.feed(marker[:2])
        await process.stdout.feed(marker[2:])
        await wait_until(
            lambda: len([e for e in events if e["event"] == "ssh.pty.output"])
            == 2
        )
        await process.stderr.feed(b"e" * 40_000)
        await process.finish(exit_status=None, exit_signal="TERM")
        await wait_until(lambda: any(e["event"] == "ssh.pty.closed" for e in events))

        output = [e for e in events if e["event"] == "ssh.pty.output"]
        assert [e["stream_sequence"] for e in output] == list(
            range(1, len(output) + 1)
        )
        decoded = [base64.b64decode(e["data_b64"]) for e in output]
        assert all(0 < len(chunk) <= 32_768 for chunk in decoded)
        assert b"".join(decoded).count(marker) == 1
        closed = events[-1]
        assert closed == {
            "event": "ssh.pty.closed",
            "pty_session_id": str(pty.pty_session_id),
            "exit_status": None,
            "exit_signal": "TERM",
        }

    asyncio.run(scenario())


def test_write_resize_close_and_unknown_session_fail_closed() -> None:
    async def scenario() -> None:
        manager, _, ssh_session, process, _, _ = setup_manager()
        pty = await manager.open(ssh_session.ssh_session_id, cols=80, rows=24)

        await manager.write(pty.pty_session_id, "echo 中文\r".encode())
        assert process.stdin.writes == ["echo 中文\r".encode()]
        await manager.resize(pty.pty_session_id, cols=120, rows=40)
        assert process.sizes == [(120, 40)]

        with pytest.raises(PtyManagerError, match="PTY_SESSION_NOT_FOUND"):
            await manager.write(uuid4(), b"must-not-route")
        assert process.stdin.writes == ["echo 中文\r".encode()]

        close_task = asyncio.create_task(manager.close(pty.pty_session_id))
        await wait_until(lambda: process.stdin.eof)
        await process.finish(exit_status=0)
        await close_task

    asyncio.run(scenario())


def test_disconnect_closes_registered_pty_channel() -> None:
    async def scenario() -> None:
        manager, sessions, ssh_session, process, _, events = setup_manager()
        pty = await manager.open(ssh_session.ssh_session_id, cols=80, rows=24)

        await sessions.close(ssh_session.ssh_session_id)
        await wait_until(lambda: manager.get(pty.pty_session_id) is None)
        assert process.closed is True
        assert process.waited is True
        assert events[-1]["event"] == "ssh.pty.closed"

    asyncio.run(scenario())
