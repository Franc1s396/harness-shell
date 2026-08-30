"""Isolated SFTP channel ownership bound to live SSH sessions."""

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
    """Own one SFTP client until deterministic close and registry removal."""

    #: Live SSH session which owns the child channel.
    owner: SshSession
    #: Public AsyncSSH SFTP client opened with byte paths.
    client: Any
    #: Prevent duplicate exit/wait operations.
    _closed: bool = False

    async def close(self) -> None:
        """Close once and always remove the client from the SSH child registry."""

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
    """Open short-lived SFTP clients only from explicitly selected live sessions."""

    def __init__(self, ssh_sessions: SshSessionRegistry) -> None:
        """Bind the sole live SSH session registry."""

        self._ssh_sessions = ssh_sessions

    async def open(self, ssh_session_id: UUID) -> SftpChannelLease:
        """Open and register one byte-path SFTP client for a live session."""

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
        """Resolve an unambiguous session with the exact frozen authenticated chain."""

        connection_sessions = self._ssh_sessions.find_by_connection_id(connection_id)
        if not connection_sessions:
            raise ManualSftpError(
                "SFTP_SESSION_NOT_CONNECTED",
                "Recovery requires an active SSH session for this connection.",
            )
        sessions = self._ssh_sessions.find_recovery_session(
            connection_id=connection_id,
            connection_profile_version=connection_profile_version,
            target_host_key_fingerprint=target_host_key_fingerprint,
            jump_connection_id=jump_connection_id,
            jump_profile_version=jump_profile_version,
            jump_host_key_fingerprint=jump_host_key_fingerprint,
        )
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
