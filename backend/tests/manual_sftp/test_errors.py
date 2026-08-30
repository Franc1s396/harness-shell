"""Stable mapping for deterministic AsyncSSH SFTP status responses."""

from __future__ import annotations

import asyncssh
import pytest

from harness_shell_sidecar.manual_sftp.errors import map_typed_sftp_status


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (asyncssh.SFTPPermissionDenied("denied"), "SFTP_PERMISSION_DENIED"),
        (asyncssh.SFTPNoSuchFile("missing"), "SFTP_SOURCE_NOT_FOUND"),
        (asyncssh.SFTPNoSuchPath("missing"), "SFTP_SOURCE_NOT_FOUND"),
        (asyncssh.SFTPFileAlreadyExists("exists"), "SFTP_TARGET_EXISTS"),
        (asyncssh.SFTPOpUnsupported("unsupported"), "SFTP_OPERATION_UNSUPPORTED"),
    ],
)
def test_typed_status_mapping_is_stable(error: BaseException, expected_code: str) -> None:
    mapped = map_typed_sftp_status(error)

    assert mapped is not None
    assert mapped.error_code == expected_code


def test_unknown_sftp_failure_is_not_guessed() -> None:
    assert map_typed_sftp_status(asyncssh.SFTPFailure("unknown")) is None
