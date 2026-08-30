"""Remote POSIX path validation tests."""

from __future__ import annotations

import pytest

from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.paths import (
    join_remote_path,
    validate_basename,
    validate_remote_path,
)


@pytest.mark.parametrize("path", ["", "relative", "/a\x00b"])
def test_remote_path_rejects_non_absolute_or_nul(path: str) -> None:
    with pytest.raises(ManualSftpError, match="SFTP_PATH_INVALID"):
        validate_remote_path(path)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\x00b"])
def test_basename_rejects_ambiguous_components(name: str) -> None:
    with pytest.raises(ManualSftpError, match="SFTP_PATH_INVALID"):
        validate_basename(name)


def test_remote_paths_are_validated_without_normalizing_user_input() -> None:
    assert validate_remote_path("/home/demo/../demo") == "/home/demo/../demo"
    assert join_remote_path("/home/demo", "data.txt") == "/home/demo/data.txt"
