"""Binary-safe interactive PTY lifecycle over established SSH sessions."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from harness_shell_sidecar.ssh.sessions import SshSession, SshSessionRegistry

from .models import PtySession


MAX_PTY_CHUNK_BYTES = 32_768
PtyEventListener = Callable[[dict], Awaitable[None]]


class PtyManagerError(RuntimeError):
    """携带稳定错误码的 PTY 生命周期异常。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定错误码及不包含终端内容的具体失败原因。"""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # 面向调用方的稳定错误码。
        self.safe_message = message  # 不包含 PTY 输入或输出的安全诊断。


@dataclass(slots=True)
class _PtyState:
    """PtyManager 内部拥有的可变 PTY 资源与事件序号状态。"""

    #: 对外公开的严格 PTY 会话快照。
    session: PtySession
    #: 拥有当前 channel 的 SSH 主会话。
    owner: SshSession
    #: AsyncSSH 创建的二进制 PTY 进程 channel。
    process: Any
    #: 分别排空 stdout 和 stderr 的后台任务。
    readers: tuple[asyncio.Task[None], ...]
    #: 等待读取任务和进程结束并发布关闭事件的后台任务。
    monitor: asyncio.Task[None] | None
    #: PTY 完成全部收敛后置位的同步事件。
    closed: asyncio.Event
    #: 下一条 PTY 输出事件使用的单调递增序号。
    next_sequence: int = 1


class PtyManager:
    """在已验证 SSH 会话上管理二进制安全的交互式 PTY channel。"""

    def __init__(
        self,
        ssh_sessions: SshSessionRegistry,
        *,
        event_listener: PtyEventListener,
    ) -> None:
        """绑定 SSH 所有权注册表和唯一 PTY 事件发布回调。"""

        self._ssh_sessions = ssh_sessions  # 定位主连接并登记子 channel。
        self._event_listener = event_listener  # 发布有序输出与关闭事件。
        self._sessions: dict[UUID, _PtyState] = {}  # 活动 PTY 的内部状态索引。

    def get(self, pty_session_id: UUID) -> PtySession | None:
        """返回 PTY 会话快照；不存在或已清理时返回 None。"""

        state = self._sessions.get(pty_session_id)
        return None if state is None else state.session

    async def open(
        self, ssh_session_id: UUID, *, cols: int, rows: int
    ) -> PtySession:
        """创建二进制 PTY channel，并启动双流读取与关闭监控任务。"""

        owner = self._ssh_sessions.get(ssh_session_id)
        if owner is None:
            raise PtyManagerError(
                "SSH_SESSION_NOT_FOUND",
                "the requested SSH session is not available for PTY creation",
            )
        pty_session_id = uuid4()
        session = PtySession(
            pty_session_id=pty_session_id,
            ssh_session_id=ssh_session_id,
            connection_id=owner.connection_id,
            cols=cols,
            rows=rows,
            state="OPEN",
        )
        process = await owner.connection.create_process(
            term_type="xterm-256color",
            term_size=(cols, rows),
            encoding=None,
        )
        owner.child_channels.add(process)
        closed = asyncio.Event()
        state = _PtyState(
            session=session,
            owner=owner,
            process=process,
            readers=(),
            monitor=None,
            closed=closed,
        )
        self._sessions[pty_session_id] = state
        state.readers = (
            asyncio.create_task(self._read_stream(state, process.stdout)),
            asyncio.create_task(self._read_stream(state, process.stderr)),
        )
        state.monitor = asyncio.create_task(self._monitor(state))
        return session

    async def write(self, pty_session_id: UUID, data: bytes) -> None:
        """把一段有大小上限的二进制输入写入活动 PTY。"""

        state = self._require_open(pty_session_id)
        if not data or len(data) > MAX_PTY_CHUNK_BYTES:
            raise PtyManagerError(
                "PTY_INPUT_SIZE_INVALID",
                "PTY input must contain between 1 and 32768 bytes",
            )
        state.process.stdin.write(data)

    async def resize(
        self, pty_session_id: UUID, *, cols: int, rows: int
    ) -> PtySession:
        """校验并应用新的终端尺寸，同时更新公开会话快照。"""

        state = self._require_open(pty_session_id)
        updated = state.session.model_copy(update={"cols": cols, "rows": rows})
        updated = PtySession.model_validate(updated.model_dump())
        state.process.change_terminal_size(cols, rows)
        state.session = updated
        return updated

    async def close(self, pty_session_id: UUID) -> PtySession:
        """先发送 EOF 等待优雅关闭，超时后强制关闭并等待收敛。"""

        state = self._require_open(pty_session_id)
        state.process.stdin.write_eof()
        try:
            await asyncio.wait_for(state.closed.wait(), timeout=2)
        except TimeoutError:
            state.process.close()
            await state.process.wait_closed()
            await state.closed.wait()
        return state.session.model_copy(update={"state": "CLOSED"})

    async def close_all(self) -> None:
        """关闭全部 OPEN PTY，并等待已在关闭中的会话收敛。"""

        for state in list(self._sessions.values()):
            if state.session.state == "OPEN":
                await self.close(state.session.pty_session_id)
            else:
                await state.closed.wait()

    def _require_open(self, pty_session_id: UUID) -> _PtyState:
        """返回活动内部状态，不存在或非 OPEN 时抛出稳定错误。"""

        state = self._sessions.get(pty_session_id)
        if state is None or state.session.state != "OPEN":
            raise PtyManagerError(
                "PTY_SESSION_NOT_FOUND",
                "the requested PTY session is absent or no longer open",
            )
        return state

    async def _read_stream(self, state: _PtyState, stream) -> None:
        """读取单个二进制流并按大小上限发布有序 Base64 输出事件。"""

        while True:
            data = await stream.read(MAX_PTY_CHUNK_BYTES)
            if not data:
                return
            if not isinstance(data, bytes):
                raise PtyManagerError(
                    "PTY_STREAM_NOT_BINARY",
                    "the SSH PTY stream returned a non-binary chunk",
                )
            for offset in range(0, len(data), MAX_PTY_CHUNK_BYTES):
                chunk = data[offset : offset + MAX_PTY_CHUNK_BYTES]
                sequence = state.next_sequence
                state.next_sequence += 1
                await self._event_listener(
                    {
                        "event": "ssh.pty.output",
                        "pty_session_id": str(state.session.pty_session_id),
                        "stream_sequence": sequence,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                    }
                )

    async def _monitor(self, state: _PtyState) -> None:
        """等待 PTY 结束、发布关闭事件，并从双重所有权索引清理资源。"""

        try:
            await asyncio.gather(*state.readers)
            await state.process.wait_closed()
            state.session = state.session.model_copy(update={"state": "CLOSED"})
            await self._event_listener(
                {
                    "event": "ssh.pty.closed",
                    "pty_session_id": str(state.session.pty_session_id),
                    "exit_status": state.process.exit_status,
                    "exit_signal": state.process.exit_signal,
                }
            )
        finally:
            state.owner.child_channels.discard(state.process)
            self._sessions.pop(state.session.pty_session_id, None)
            state.closed.set()
