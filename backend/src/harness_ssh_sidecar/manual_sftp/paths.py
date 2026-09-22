"""对远程 POSIX 路径和单个名称执行严格失败校验。"""

from __future__ import annotations

from .errors import ManualSftpError


def validate_remote_path(path: str) -> str:
    """返回已合法的 POSIX 绝对路径，不做规范化。"""

    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ManualSftpError(
            "SFTP_PATH_INVALID", "Remote path must be absolute and contain no NUL."
        )
    return path


def validate_basename(name: str) -> str:
    """返回无歧义的单个远程路径分量，不改写内容。"""

    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise ManualSftpError(
            "SFTP_PATH_INVALID", "Remote name must be one non-special component."
        )
    return name


def join_remote_path(directory: str, name: str) -> str:
    """拼接已校验的 POSIX 分量，不规范化用户输入。"""

    validated_directory = validate_remote_path(directory)
    validated_name = validate_basename(name)
    separator = "" if validated_directory.endswith("/") else "/"
    return f"{validated_directory}{separator}{validated_name}"
