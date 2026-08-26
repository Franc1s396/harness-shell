"""Strictly read-only, bounded SFTP operations on isolated channels."""

from __future__ import annotations

import hashlib
import posixpath
import stat
from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry

from .artifacts import ArtifactStore
from .models import (
    RemoteHashResult,
    RemoteListResult,
    RemoteReadRangeResult,
    RemoteStat,
)


MAX_LIST_ENTRIES = 1_000
MAX_READ_RANGE_BYTES = 262_144
HASH_CHUNK_BYTES = 64 * 1024
T = TypeVar("T")


class RemoteSftpError(RuntimeError):
    pass


class RemoteSftp:
    def __init__(
        self, ssh_sessions: SshSessionRegistry, artifacts: ArtifactStore
    ) -> None:
        self._ssh_sessions = ssh_sessions
        self._artifacts = artifacts

    async def lstat(self, ssh_session_id: UUID, path: str) -> RemoteStat:
        path = _validate_path(path)

        async def operation(client):
            return _stat(path, await client.lstat(path))

        return await self._with_client(ssh_session_id, operation)

    async def listdir(
        self, ssh_session_id: UUID, path: str, max_entries: int
    ) -> RemoteListResult:
        path = _validate_path(path)
        if not 1 <= max_entries <= MAX_LIST_ENTRIES:
            raise ValueError("max_entries must be between 1 and 1000")

        async def operation(client):
            entries: list[RemoteStat] = []
            truncated = False
            async for entry in client.scandir(path):
                if len(entries) == max_entries:
                    truncated = True
                    break
                entry_path = posixpath.join(path.rstrip("/") or "/", entry.filename)
                entries.append(_stat(entry_path, entry.attrs))
            return RemoteListResult(
                path=path, entries=tuple(entries), truncated=truncated
            )

        return await self._with_client(ssh_session_id, operation)

    async def read_range(
        self,
        ssh_session_id: UUID,
        path: str,
        offset: int,
        length: int,
    ) -> RemoteReadRangeResult:
        path = _validate_path(path)
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= length <= MAX_READ_RANGE_BYTES:
            raise ValueError("length must be between 1 and 262144")

        async def operation(client):
            info = _stat(path, await client.lstat(path))
            _require_regular_file(info)
            async with await client.open(path, "rb") as remote_file:
                await remote_file.seek(offset)
                payload = await remote_file.read(length)
            if not isinstance(payload, bytes) or len(payload) > length:
                raise RemoteSftpError("REMOTE_READ_INVALID")
            artifact = self._artifacts.put(
                payload,
                media_type="application/octet-stream",
                sensitivity="sensitive",
                complete=True,
            )
            return RemoteReadRangeResult(
                path=path,
                offset=offset,
                requested_length=length,
                artifact=artifact,
                eof=offset + len(payload) >= info.size,
            )

        return await self._with_client(ssh_session_id, operation)

    async def sha256(self, ssh_session_id: UUID, path: str) -> RemoteHashResult:
        path = _validate_path(path)

        async def operation(client):
            info = _stat(path, await client.lstat(path))
            _require_regular_file(info)
            digest = hashlib.sha256()
            byte_count = 0
            async with await client.open(path, "rb") as remote_file:
                while True:
                    chunk = await remote_file.read(HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes) or len(chunk) > HASH_CHUNK_BYTES:
                        raise RemoteSftpError("REMOTE_READ_INVALID")
                    digest.update(chunk)
                    byte_count += len(chunk)
            return RemoteHashResult(
                path=path,
                sha256=digest.hexdigest(),
                byte_count=byte_count,
            )

        return await self._with_client(ssh_session_id, operation)

    async def _with_client(
        self, ssh_session_id: UUID, operation: Callable[[object], Awaitable[T]]
    ) -> T:
        owner = self._ssh_sessions.get(ssh_session_id)
        if owner is None:
            raise RemoteSftpError("SSH_SESSION_NOT_FOUND")
        client = await owner.connection.start_sftp_client()
        owner.child_channels.add(client)
        try:
            return await operation(client)
        finally:
            client.exit()
            await client.wait_closed()
            owner.child_channels.discard(client)


def _validate_path(path: str) -> str:
    if not path or not path.startswith("/") or "\x00" in path:
        raise RemoteSftpError("REMOTE_PATH_INVALID")
    return path


def _stat(path: str, attrs) -> RemoteStat:
    permissions = attrs.permissions
    if permissions is None:
        raise RemoteSftpError("REMOTE_ATTRIBUTES_INCOMPLETE")
    mtime_ns = getattr(attrs, "mtime_ns", None)
    if mtime_ns is None and attrs.mtime is not None:
        mtime_ns = int(attrs.mtime * 1_000_000_000)
    return RemoteStat(
        path=path,
        size=0 if attrs.size is None else attrs.size,
        mode=permissions,
        mtime_ns=mtime_ns,
        is_file=stat.S_ISREG(permissions),
        is_dir=stat.S_ISDIR(permissions),
    )


def _require_regular_file(info: RemoteStat) -> None:
    if stat.S_ISLNK(info.mode):
        raise RemoteSftpError("REMOTE_SYMLINK_FORBIDDEN")
    if not info.is_file:
        raise RemoteSftpError("REMOTE_NOT_REGULAR_FILE")
