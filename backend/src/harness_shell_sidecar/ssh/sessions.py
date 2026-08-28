"""Opaque SSH session registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass(slots=True)
class SshSession:
    """一个已认证 SSH 主连接及其跳板和子 channel 所有权记录。"""

    #: 暴露给后续 PTY、exec 和 SFTP 操作的不透明会话标识符。
    ssh_session_id: UUID
    #: 创建该会话的连接配置标识符。
    connection_id: UUID
    #: 已认证且 Host Key 已验证的目标 AsyncSSH 连接。
    connection: Any
    #: 可选的已认证 ProxyJump 连接，必须晚于目标子连接关闭。
    jump_connection: Any | None = None
    #: 由该会话派生、关闭会话时必须先收敛的 PTY/exec/SFTP channel。
    child_channels: set[Any] = field(default_factory=set)


class SshSessionRegistry:
    """拥有所有活动 SSH 会话并按安全顺序关闭其资源的注册表。"""

    def __init__(self) -> None:
        """创建空的进程内会话索引。"""

        self._sessions: dict[UUID, SshSession] = {}  # session_id 到所有权记录的映射。

    def __len__(self) -> int:
        """返回当前活动 SSH 会话数量。"""

        return len(self._sessions)

    def register(
        self, connection_id: UUID, connection: Any, jump_connection: Any | None = None
    ) -> SshSession:
        """为已建立连接分配不透明会话 ID 并接管其生命周期。"""

        session = SshSession(
            uuid4(), connection_id, connection, jump_connection
        )
        self._sessions[session.ssh_session_id] = session
        return session

    def get(self, session_id: UUID) -> SshSession | None:
        """读取活动会话；不存在时返回 None。"""

        return self._sessions.get(session_id)

    async def close(self, session_id: UUID) -> SshSession | None:
        """依次关闭子 channel、目标连接和跳板，并保留首个清理错误。"""

        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        channels = list(session.child_channels)
        for channel in channels:
            try:
                close = getattr(channel, "close", None)
                if close is not None:
                    close()
                else:
                    channel.exit()
            except BaseException as exc:
                remember(exc)
        for channel in channels:
            try:
                await channel.wait_closed()
            except BaseException as exc:
                remember(exc)
        session.child_channels.clear()

        try:
            session.connection.close()
        except BaseException as exc:
            remember(exc)
        try:
            await session.connection.wait_closed()
        except BaseException as exc:
            remember(exc)

        if session.jump_connection is not None:
            try:
                session.jump_connection.close()
            except BaseException as exc:
                remember(exc)
            try:
                await session.jump_connection.wait_closed()
            except BaseException as exc:
                remember(exc)
        if first_error is not None:
            raise first_error
        return session

    async def close_all(self) -> None:
        """尝试关闭全部会话，完成所有清理后再抛出首个错误。"""

        first_error: BaseException | None = None
        for session_id in list(self._sessions):
            try:
                await self.close(session_id)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
