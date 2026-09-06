"""用户手动 SFTP 操作的稳定有界失败。"""

from __future__ import annotations

from typing import Literal

import asyncssh


MAX_SAFE_MESSAGE_LENGTH = 256


class ManualSftpError(RuntimeError):
    """仅携带稳定错误码与有界非敏感消息。"""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        operation_state: Literal["cleanup_required", "outcome_unknown"] | None = None,
    ) -> None:
        """拒绝非法公开失败值，不截断或猜测。"""

        if not error_code or len(error_code) > 64:
            raise ValueError("manual SFTP error code is invalid")
        if not message or len(message) > MAX_SAFE_MESSAGE_LENGTH:
            raise ValueError("manual SFTP error message is invalid")
        self.error_code = error_code
        self.safe_message = message
        # 只有这两个持久化非终态可经过内部协议错误路径。
        self.operation_state = operation_state
        super().__init__(f"{error_code}: {message}")


def map_typed_sftp_status(
    error: BaseException,
    *,
    missing_code: str = "SFTP_SOURCE_NOT_FOUND",
    exists_code: str = "SFTP_TARGET_EXISTS",
    unsupported_code: str = "SFTP_OPERATION_UNSUPPORTED",
) -> ManualSftpError | None:
    """只映射确定的 AsyncSSH 状态类；未知失败仍保持未知。"""

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
