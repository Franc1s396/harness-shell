"""Fail-closed validation for remote POSIX paths and basenames."""

from __future__ import annotations

from .errors import ManualSftpError


def validate_remote_path(path: str) -> str:
    """Return an already-valid absolute POSIX path without normalizing it."""

    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ManualSftpError(
            "SFTP_PATH_INVALID", "Remote path must be absolute and contain no NUL."
        )
    return path


def validate_basename(name: str) -> str:
    """Return one unambiguous remote path component without rewriting it."""

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
    """Join validated POSIX components without canonicalizing user input."""

    validated_directory = validate_remote_path(directory)
    validated_name = validate_basename(name)
    separator = "" if validated_directory.endswith("/") else "/"
    return f"{validated_directory}{separator}{validated_name}"
