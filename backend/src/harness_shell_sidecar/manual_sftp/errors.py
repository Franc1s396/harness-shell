"""Stable, bounded failures for user-operated manual SFTP."""

from __future__ import annotations

from typing import Literal

import asyncssh


MAX_SAFE_MESSAGE_LENGTH = 256


class ManualSftpError(RuntimeError):
    """Carry only a stable error code and bounded non-sensitive message."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        operation_state: Literal["cleanup_required", "outcome_unknown"] | None = None,
    ) -> None:
        """Reject invalid public failures instead of truncating or guessing."""

        if not error_code or len(error_code) > 64:
            raise ValueError("manual SFTP error code is invalid")
        if not message or len(message) > MAX_SAFE_MESSAGE_LENGTH:
            raise ValueError("manual SFTP error message is invalid")
        self.error_code = error_code
        self.safe_message = message
        # Only these two durable non-terminal states may cross the private protocol error path.
        self.operation_state = operation_state
        super().__init__(f"{error_code}: {message}")


def map_typed_sftp_status(
    error: BaseException,
    *,
    missing_code: str = "SFTP_SOURCE_NOT_FOUND",
    exists_code: str = "SFTP_TARGET_EXISTS",
    unsupported_code: str = "SFTP_OPERATION_UNSUPPORTED",
) -> ManualSftpError | None:
    """Map only deterministic AsyncSSH status classes; unknown failures stay unknown."""

    if isinstance(error, asyncssh.SFTPPermissionDenied):
        return ManualSftpError(
            "SFTP_PERMISSION_DENIED", "The server denied the remote SFTP request."
        )
    if isinstance(error, (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath)):
        return ManualSftpError(missing_code, "The remote source does not exist.")
    if isinstance(error, asyncssh.SFTPFileAlreadyExists):
        return ManualSftpError(exists_code, "The remote target already exists.")
    if isinstance(error, asyncssh.SFTPOpUnsupported):
        return ManualSftpError(
            unsupported_code, "The server does not support the required SFTP operation."
        )
    return None
