"""绑定活动 SSH 会话的独立 SFTP 通道所有权。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from harness_shell_sidecar.ssh.sessions import SshSession, SshSessionRegistry

from .errors import ManualSftpError


CHANNEL_OPERATION_TIMEOUT_SECONDS = 15


@dataclass(slots=True)
class SftpChannelLease:
    """拥有 SFTP 客户端，直到确定性关闭并从注册表移除。"""

    #: 拥有此子通道的活动 SSH 会话。
    owner: SshSession
    #: 使用字节路径打开的 AsyncSSH 公共 SFTP 客户端。
    client: Any
    #: 防止重复执行退出和等待操作。
    _closed: bool = False

    async def close(self) -> None:
        """只关闭一次，并始终从 SSH 子资源注册表移除客户端。"""

        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        try:
            self.client.exit()
        except BaseException as exc:
            first_error = exc
        try:
            async with asyncio.timeout(CHANNEL_OPERATION_TIMEOUT_SECONDS):
                await self.client.wait_closed()
        except TimeoutError as exc:
            if first_error is None:
                first_error = ManualSftpError(
                    "SFTP_OPERATION_TIMEOUT",
                    "The SFTP channel close request timed out.",
                )
            first_error.__cause__ = exc
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        finally:
            self.owner.child_channels.discard(self.client)
        if first_error is not None:
            raise first_error


class SftpChannelFactory:
    """仅从用户显式选择的活动会话打开短生命周期 SFTP 客户端。"""

    def __init__(self, ssh_sessions: SshSessionRegistry) -> None:
        """绑定唯一的活动 SSH 会话注册表。"""

        self._ssh_sessions = ssh_sessions

    async def open(self, ssh_session_id: UUID) -> SftpChannelLease:
        """为活动会话打开并注册使用字节路径的 SFTP 客户端。"""

        owner = self._ssh_sessions.get(ssh_session_id)
        if owner is None:
            raise ManualSftpError(
                "SFTP_SESSION_NOT_CONNECTED",
                "The selected SSH session is not active.",
            )
        try:
            async with asyncio.timeout(CHANNEL_OPERATION_TIMEOUT_SECONDS):
                client = await owner.connection.start_sftp_client(path_encoding=None)
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT",
                "The SFTP channel open request timed out.",
            ) from exc
        except Exception as exc:
            raise ManualSftpError(
                "SFTP_CHANNEL_OPEN_FAILED", "The SFTP channel could not be opened."
            ) from exc
        owner.child_channels.add(client)
        return SftpChannelLease(owner, client)

    def session_id_for_recovery(
        self,
        *,
        connection_id: UUID,
        connection_profile_version: int,
        target_host_key_fingerprint: str,
        jump_connection_id: UUID | None,
        jump_profile_version: int | None,
        jump_host_key_fingerprint: str | None,
    ) -> UUID:
        """按精确冻结的认证链解析无歧义会话。"""

        # 1. 确认原连接仍有活动会话，避免恢复流程自行建立网络连接。
        connection_sessions = self._ssh_sessions.find_by_connection_id(connection_id)
        if not connection_sessions:
            raise ManualSftpError(
                "SFTP_SESSION_NOT_CONNECTED",
                "Recovery requires an active SSH session for this connection.",
            )
        # 2. 精确匹配目标和跳板版本、Host Key 等完整认证链。
        sessions = self._ssh_sessions.find_recovery_session(
            connection_id=connection_id,
            connection_profile_version=connection_profile_version,
            target_host_key_fingerprint=target_host_key_fingerprint,
            jump_connection_id=jump_connection_id,
            jump_profile_version=jump_profile_version,
            jump_host_key_fingerprint=jump_host_key_fingerprint,
        )
        # 3. 无匹配或多个匹配都显式失败，仅返回唯一可信会话。
        if not sessions:
            raise ManualSftpError(
                "SFTP_RECOVERY_SESSION_MISMATCH",
                "The active SSH session does not match the recovery endpoint identity.",
            )
        if len(sessions) != 1:
            raise ManualSftpError(
                "SFTP_RECOVERY_SESSION_AMBIGUOUS",
                "Recovery requires exactly one active session for this connection.",
            )
        return sessions[0].ssh_session_id
