from __future__ import annotations

import asyncio

import pytest

from harness_shell_sidecar.remote_io import RemoteSftp
from harness_shell_sidecar.remote_io.sftp import RemoteSftpError


def test_sftp_lstat_list_read_hash_and_symlink_rejection(
    runtime_context, connect_proxy, lab
) -> None:
    async def scenario() -> None:
        _, _, status = await connect_proxy()
        remote = RemoteSftp(runtime_context.runtime.sessions, runtime_context.artifacts)
        home = f"/home/{lab.target_username}"
        path = f"{home}/data.txt"
        info = await remote.lstat(status.session_id, path)
        listing = await remote.listdir(status.session_id, home, max_entries=1000)
        read = await remote.read_range(status.session_id, path, offset=0, length=262_144)
        digest = await remote.sha256(status.session_id, path)
        payload = runtime_context.artifacts.get(read.artifact.artifact_id)
        assert info.is_file is True
        assert any(entry.path == path for entry in listing.entries)
        assert digest.byte_count == len(payload)
        assert digest.sha256 == read.artifact.sha256
        with pytest.raises(RemoteSftpError, match="REMOTE_SYMLINK_FORBIDDEN"):
            await remote.read_range(
                status.session_id, f"{home}/data-link", offset=0, length=1
            )
        assert not hasattr(remote, "write")
        assert not hasattr(remote, "remove")
        assert not hasattr(remote, "rename")
        await runtime_context.runtime.disconnect(status.session_id)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())
