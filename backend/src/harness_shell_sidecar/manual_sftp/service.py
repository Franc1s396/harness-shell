"""User-operated manual SFTP browse, transfer, mutation, and recovery service."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import asyncssh

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import EncryptedRecordStore

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .listings import ListingManager, remote_entry
from .models import (
    DeletePlanSummary,
    DownloadChunk,
    DownloadReady,
    ListingBatch,
    ManualSftpContext,
    OperationTerminalProjection,
    RecoverySummary,
    RemoteEntry,
    RemoteFileHash,
    TransferSnapshot,
    UploadChunkAck,
    UploadReady,
)
from .mutations import MutationManager
from .operation_store import RemoteOperationStore
from .paths import validate_remote_path
from .recovery import RecoveryManager
from .transfers import DownloadManager, UploadManager


METADATA_TIMEOUT_SECONDS = 15
HASH_NO_PROGRESS_TIMEOUT_SECONDS = 60
HASH_CHUNK_BYTES = 64 * 1024
MAX_JS_SAFE_INTEGER = 2**53 - 1


class ManualSftpService:
    """Own all user-operated SFTP channels, cursors, operations, and recovery state."""

    def __init__(
        self,
        ssh_sessions: SshSessionRegistry,
        records: EncryptedRecordStore,
        event_listener: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Create read owners while retaining collaborators for later operation stages."""

        self._channels = SftpChannelFactory(ssh_sessions)
        self._listings = ListingManager(self._channels)
        self._records = records
        self._event_listener = event_listener
        self._operations = RemoteOperationStore(records)
        self._uploads = UploadManager(self._channels, self._operations)
        self._downloads = DownloadManager(self._channels)
        self._mutations = MutationManager(
            self._channels, self._operations, self._event_listener
        )
        self._recovery = RecoveryManager(
            self._channels, self._operations, self._mutations
        )

    async def open(self, ssh_session_id: UUID) -> ManualSftpContext:
        """Resolve the home directory on one explicitly bound live SSH session."""

        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                home = _decode_remote_path(await lease.client.getcwd())
            return ManualSftpContext(
                ssh_session_id=ssh_session_id,
                connection_id=lease.owner.connection_id,
                home=validate_remote_path(home),
                host_label=lease.owner.host_label,
                sftp_version=lease.client.version,
            )
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied access to the remote home directory.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def list_begin(self, ssh_session_id: UUID, path: str) -> ListingBatch:
        """Start a bounded directory listing."""

        return await self._listings.begin(ssh_session_id, path)

    async def list_next(self, listing_id: UUID, sequence: int) -> ListingBatch:
        """Continue an active directory listing at its exact next sequence."""

        return await self._listings.next(listing_id, sequence)

    async def list_close(self, listing_id: UUID) -> None:
        """Close one listing cursor explicitly."""

        await self._listings.close(listing_id)

    async def lstat(self, ssh_session_id: UUID, path: str) -> RemoteEntry:
        """Read metadata without following symbolic links."""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                attrs = await lease.client.lstat(remote_path.encode("utf-8"))
            return remote_entry(remote_path, attrs)
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote metadata request.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def readlink(self, ssh_session_id: UUID, path: str) -> RemoteEntry:
        """Return explicit link text while retaining no-follow link metadata."""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                encoded = remote_path.encode("utf-8")
                attrs = await lease.client.lstat(encoded)
                target = _decode_remote_text(await lease.client.readlink(encoded))
            entry = remote_entry(remote_path, attrs, link_target=target)
            if entry.entry_type != "symlink":
                raise ManualSftpError(
                    "SFTP_NOT_A_SYMLINK", "The selected remote entry is not a symlink."
                )
            return entry
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote link request.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def realpath(self, ssh_session_id: UUID, path: str) -> RemoteEntry:
        """Resolve an explicitly opened link/path and return target metadata."""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                resolved = validate_remote_path(
                    _decode_remote_path(
                        await lease.client.realpath(remote_path.encode("utf-8"))
                    )
                )
                attrs = await lease.client.lstat(resolved.encode("utf-8"))
            return remote_entry(resolved, attrs)
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote path resolution request.",
            ) from exc
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
            raise map_typed_sftp_status(exc) from exc
        finally:
            await lease.close()

    async def sha256(
        self,
        ssh_session_id: UUID,
        path: str,
        *,
        cancelled: asyncio.Event | None = None,
    ) -> RemoteFileHash:
        """Hash one regular file and reject any metadata change seen before return."""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            _require_active(cancelled)
            initial = await self._snapshot(lease, remote_path)
            if not initial.exists or initial.entry_type != "file":
                raise ManualSftpError(
                    "SFTP_NOT_A_REGULAR_FILE",
                    "The selected remote entry is not a regular file.",
                )
            digest = hashlib.sha256()
            byte_count = 0
            async with await lease.client.open(remote_path.encode("utf-8"), "rb") as file:
                while True:
                    _require_active(cancelled)
                    try:
                        async with asyncio.timeout(HASH_NO_PROGRESS_TIMEOUT_SECONDS):
                            chunk = await file.read(HASH_CHUNK_BYTES)
                    except TimeoutError as exc:
                        raise ManualSftpError(
                            "SFTP_HASH_TIMEOUT", "The remote file hash made no progress."
                        ) from exc
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes) or len(chunk) > HASH_CHUNK_BYTES:
                        raise ManualSftpError(
                            "SFTP_REMOTE_READ_INVALID",
                            "The remote file returned an invalid read chunk.",
                        )
                    byte_count += len(chunk)
                    if byte_count > MAX_JS_SAFE_INTEGER:
                        raise ManualSftpError(
                            "SFTP_FILE_SIZE_UNSUPPORTED",
                            "The remote file size is outside the supported range.",
                        )
                    digest.update(chunk)
            _require_active(cancelled)
            final = await self._snapshot(lease, remote_path)
            if final != initial or (
                initial.size is not None and byte_count != initial.size
            ):
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED", "The remote file changed during hashing."
                )
            sha256 = digest.hexdigest()
            return RemoteFileHash(
                path=remote_path,
                snapshot=initial.model_copy(update={"sha256": sha256}),
                sha256=sha256,
                byte_count=byte_count,
            )
        except asyncssh.SFTPPermissionDenied as exc:
            raise ManualSftpError(
                "SFTP_PERMISSION_DENIED",
                "The server denied the remote file hash request.",
            ) from exc
        finally:
            await lease.close()

    async def upload_preflight(
        self, ssh_session_id: UUID, path: str
    ) -> TransferSnapshot:
        """Freeze one remote upload target before user overwrite confirmation."""

        return await self._uploads.preflight(ssh_session_id, path)

    async def upload_begin(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        source_sha256: str,
        source_byte_count: int,
        target_snapshot: TransferSnapshot,
    ) -> UploadReady:
        """Begin an upload from privileged Rust-owned frozen source metadata."""

        return await self._uploads.begin(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
            source_sha256=source_sha256,
            source_byte_count=source_byte_count,
            target_snapshot=target_snapshot,
        )

    async def upload_chunk(
        self,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        """Write one exact upload chunk."""

        return await self._uploads.write_chunk(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            chunk=chunk,
        )

    async def upload_finish(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """Verify and atomically commit one upload."""

        return await self._uploads.finish(operation_id)

    async def upload_abort(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """Cancel one active upload and remove its remote temp."""

        return await self._uploads.abort(operation_id)

    async def download_begin(
        self, *, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DownloadReady:
        """Freeze and open one remote download source."""

        return await self._downloads.begin(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
        )

    async def download_chunk(
        self, operation_id: UUID, *, sequence: int, offset: int
    ) -> DownloadChunk:
        """Pull one sequential remote download chunk."""

        return await self._downloads.read_chunk(
            operation_id, sequence=sequence, offset=offset
        )

    async def download_finish(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """Verify one fully pulled remote source."""

        return await self._downloads.finish(operation_id)

    async def download_abort(
        self, operation_id: UUID
    ) -> OperationTerminalProjection:
        """Cancel one active remote download read."""

        return await self._downloads.abort(operation_id)

    async def mkdir(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        parent_path: str,
        name: str,
    ) -> OperationTerminalProjection:
        """Create one user-requested remote directory."""

        return await self._mutations.mkdir(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            parent_path=parent_path,
            name=name,
        )

    async def rename(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        source_path: str,
        target_path: str,
        overwrite: bool,
        source_snapshot: TransferSnapshot | None,
        target_snapshot: TransferSnapshot | None,
    ) -> OperationTerminalProjection:
        """Atomically rename one unchanged remote entry."""

        return await self._mutations.rename(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            source_path=source_path,
            target_path=target_path,
            overwrite=overwrite,
            source_snapshot=source_snapshot,
            target_snapshot=target_snapshot,
        )

    async def remove(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        expected_snapshot: TransferSnapshot,
    ) -> OperationTerminalProjection:
        """Remove one unchanged file/link or empty directory."""

        return await self._mutations.remove(
            operation_id=operation_id,
            ssh_session_id=ssh_session_id,
            path=path,
            expected_snapshot=expected_snapshot,
        )

    async def delete_preflight(
        self, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DeletePlanSummary:
        """Build one complete encrypted no-follow recursive-delete plan."""

        return await self._mutations.delete_preflight(
            ssh_session_id, path, operation_id=operation_id
        )

    async def delete_execute(
        self, delete_plan_id: UUID
    ) -> OperationTerminalProjection:
        """Consume one recursive-delete plan at most once."""

        return await self._mutations.delete_execute(delete_plan_id)

    def list_recoveries(self) -> tuple[RecoverySummary, ...]:
        """Return non-terminal encrypted recovery summaries."""

        return self._recovery.list()

    async def recovery_inspect(self, recovery_id: UUID):
        """Perform read-only reconciliation for one recovery record."""

        return await self._recovery.inspect(recovery_id)

    async def recovery_execute(
        self, recovery_id: UUID, action: str, operation_id: UUID
    ):
        """Execute a recovery action with the Rust-selected fresh mutation identity."""

        return await self._recovery.execute(recovery_id, action, operation_id)

    async def close_all(self) -> None:
        """Close listing and transfer owners before the parent SSH registry."""

        first_error: BaseException | None = None
        for owner in (
            self._listings,
            self._uploads,
            self._downloads,
            self._mutations,
            self._recovery,
        ):
            try:
                await owner.close_all()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    async def _snapshot(
        lease: SftpChannelLease, remote_path: str
    ) -> TransferSnapshot:
        """Capture strict no-follow metadata for compare-before-return checks."""

        try:
            async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
                attrs = await lease.client.lstat(remote_path.encode("utf-8"))
        except TimeoutError as exc:
            raise ManualSftpError(
                "SFTP_OPERATION_TIMEOUT", "The SFTP metadata request timed out."
            ) from exc
        entry = remote_entry(remote_path, attrs)
        return TransferSnapshot(
            path=remote_path,
            exists=True,
            entry_type=entry.entry_type,
            size=entry.size,
            mtime_ns=entry.mtime_ns,
            sha256=None,
        )


def _decode_remote_text(value: Any) -> str:
    """Decode a public AsyncSSH byte result as strict UTF-8."""

    try:
        if isinstance(value, bytes):
            return value.decode("utf-8", "strict")
        if isinstance(value, str):
            value.encode("utf-8", "strict")
            return value
    except UnicodeError as exc:
        raise ManualSftpError(
            "SFTP_FILENAME_ENCODING_UNSUPPORTED", "A remote path is not valid UTF-8."
        ) from exc
    raise ManualSftpError(
        "SFTP_REMOTE_RESPONSE_INVALID", "The SFTP server returned an invalid path."
    )


def _decode_remote_path(value: Any) -> str:
    """Decode and validate one absolute remote path response."""

    return validate_remote_path(_decode_remote_text(value))


def _require_active(cancelled: asyncio.Event | None) -> None:
    """Stop cooperative work at defined boundaries after cancellation."""

    if cancelled is not None and cancelled.is_set():
        raise ManualSftpError(
            "SFTP_REQUEST_CANCELLED", "The manual SFTP request was cancelled."
        )
