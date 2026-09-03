"""Strict upload and download state machines for manual SFTP."""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncssh
from asyncssh.constants import FXR_ATOMIC, FXR_OVERWRITE

from .channels import SftpChannelFactory, SftpChannelLease
from .errors import ManualSftpError, map_typed_sftp_status
from .listings import remote_entry
from .models import (
    DownloadChunk,
    DownloadReady,
    OperationTerminalProjection,
    TransferSnapshot,
    UploadChunkAck,
    UploadReady,
)
from .operation_store import ManualSftpOperationStore, RemoteOperationRecord
from .paths import validate_remote_path


SFTP_CHUNK_BYTES = 262_144
HASH_CHUNK_BYTES = 64 * 1024
NO_PROGRESS_TIMEOUT_SECONDS = 60
CHUNK_REQUEST_TIMEOUT_SECONDS = 30
METADATA_TIMEOUT_SECONDS = 15
MAX_JS_SAFE_INTEGER = 2**53 - 1


@dataclass(slots=True)
class _UploadState:
    """Own one upload channel, handle, sequence, offset, and canonical snapshot."""

    #: Stable operation identity shared with React and plaintext records.
    operation_id: UUID
    #: Isolated SFTP channel lease.
    lease: SftpChannelLease
    #: Exclusive remote temporary file handle, or None after close.
    handle: Any | None
    #: Validated final remote target path.
    target_path: str
    #: Same-directory exclusive temporary path.
    temp_path: str
    #: Frozen target snapshot from preflight and begin revalidation.
    target_snapshot: TransferSnapshot
    #: Frozen local-source byte count supplied by privileged Rust Core.
    source_byte_count: int
    #: Frozen local-source hash supplied by privileged Rust Core.
    source_sha256: str
    #: Sequence required for the next chunk.
    expected_sequence: int
    #: Offset required for the next chunk.
    expected_offset: int
    #: Streaming digest of accepted upload bytes.
    digest: Any
    #: Immutable record creation timestamp reused across state updates.
    created_at: str


@dataclass(slots=True)
class _DownloadState:
    """Own one download channel, handle, position, and frozen remote source."""

    #: Stable operation identity.
    operation_id: UUID
    #: Isolated SFTP channel lease.
    lease: SftpChannelLease
    #: Open remote read handle, or None after close.
    handle: Any | None
    #: Validated source path.
    source_path: str
    #: Frozen metadata plus mandatory source hash.
    source_snapshot: TransferSnapshot
    #: Sequence required for the next pull request.
    expected_sequence: int
    #: Offset required for the next pull request.
    expected_offset: int
    #: Streaming digest over bytes returned to Rust.
    digest: Any
    #: Whether a returned chunk proved EOF for this state.
    eof: bool


class UploadManager:
    """Own remote upload temporaries through strict atomic commit or cleanup."""

    def __init__(
        self, channels: SftpChannelFactory, operations: ManualSftpOperationStore
    ) -> None:
        """Bind channel and plaintext operation owners."""

        self._channels = channels
        self._operations = operations
        self._active: dict[UUID, _UploadState] = {}

    async def preflight(
        self, ssh_session_id: UUID, path: str
    ) -> TransferSnapshot:
        """Freeze the current target, including mandatory hash for regular files."""

        remote_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            return await _snapshot(lease.client, remote_path, include_hash=True)
        finally:
            await lease.close()

    async def begin(
        self,
        *,
        operation_id: UUID,
        ssh_session_id: UUID,
        path: str,
        source_sha256: str,
        source_byte_count: int,
        target_snapshot: TransferSnapshot,
    ) -> UploadReady:
        """Persist intent, revalidate target, then exclusively create one temp."""

        self._validate_source(source_sha256, source_byte_count)
        target_path = validate_remote_path(path)
        if target_snapshot.path != target_path:
            raise ManualSftpError(
                "SFTP_TARGET_SNAPSHOT_INVALID",
                "The upload target snapshot does not match the target path.",
            )
        existing = self._operations.get(operation_id)
        if operation_id in self._active or existing is not None:
            error_code = (
                "SFTP_OPERATION_ALREADY_FINALIZED"
                if existing is not None
                and existing.state in {"succeeded", "failed", "cancelled"}
                else "SFTP_OPERATION_DUPLICATE"
            )
            raise ManualSftpError(
                error_code, "The manual SFTP operation ID cannot be reused."
            )

        lease = await self._channels.open(ssh_session_id)
        temp_path = _upload_temp_path(target_path, operation_id)
        created_at = _utc_now()
        preparing = RemoteOperationRecord(
            operation_id=operation_id,
            kind="upload",
            state="preparing",
            connection_id=lease.owner.connection_id,
            connection_profile_version=lease.owner.connection_profile_version,
            host_label=lease.owner.host_label,
            target_host_key_fingerprint=lease.owner.target_host_key_fingerprint,
            jump_connection_id=lease.owner.jump_connection_id,
            jump_profile_version=lease.owner.jump_profile_version,
            jump_host_key_fingerprint=lease.owner.jump_host_key_fingerprint,
            remote_path=target_path,
            temp_path=temp_path,
            expected_sha256=source_sha256,
            target_snapshot=target_snapshot,
            terminal_receipt=None,
            created_at=created_at,
        )
        self._operations.put(preparing)
        try:
            current = await _snapshot(
                lease.client, target_path, include_hash=target_snapshot.exists
            )
            if current != target_snapshot:
                raise ManualSftpError(
                    "SFTP_TARGET_CHANGED",
                    "The remote upload target changed after preflight.",
                )
            try:
                handle = await _metadata_await(
                    lease.client.open(temp_path.encode("utf-8"), "xb")
                )
            except asyncssh.SFTPFileAlreadyExists as exc:
                raise ManualSftpError(
                    "SFTP_TEMP_PATH_CONFLICT",
                    "The exclusive remote upload temporary path already exists.",
                ) from exc
        except BaseException as exc:
            receipt = _terminal(
                operation_id,
                "failed",
                exc.error_code if isinstance(exc, ManualSftpError) else "SFTP_UPLOAD_BEGIN_FAILED",
                "The remote upload could not be started.",
            )
            self._operations.put(
                preparing.model_copy(
                    update={"state": "failed", "terminal_receipt": receipt}
                )
            )
            await lease.close()
            raise

        state = _UploadState(
            operation_id=operation_id,
            lease=lease,
            handle=handle,
            target_path=target_path,
            temp_path=temp_path,
            target_snapshot=target_snapshot,
            source_byte_count=source_byte_count,
            source_sha256=source_sha256,
            expected_sequence=0,
            expected_offset=0,
            digest=hashlib.sha256(),
            created_at=created_at,
        )
        self._active[operation_id] = state
        self._put_upload_state(state, "transferring")
        return UploadReady(
            operation_id=operation_id,
            temp_path=temp_path,
            next_sequence=0,
            next_offset=0,
        )

    async def write_chunk(
        self,
        *,
        operation_id: UUID,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        """Accept exactly one non-empty bounded sequential chunk."""

        state = self._require_upload(operation_id)
        if sequence != state.expected_sequence or offset != state.expected_offset:
            error = ManualSftpError(
                "SFTP_PROTOCOL_SEQUENCE_INVALID",
                "The upload sequence or offset is invalid.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        if not isinstance(chunk, bytes) or not 1 <= len(chunk) <= SFTP_CHUNK_BYTES:
            error = ManualSftpError(
                "SFTP_CHUNK_INVALID", "Upload chunks must contain 1..262144 bytes."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        if state.expected_offset + len(chunk) > state.source_byte_count:
            error = ManualSftpError(
                "SFTP_SOURCE_SIZE_MISMATCH",
                "The upload bytes exceed the frozen source size.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error
        try:
            async with asyncio.timeout(CHUNK_REQUEST_TIMEOUT_SECONDS):
                await state.handle.write(chunk)
        except TimeoutError as exc:
            error = ManualSftpError(
                "SFTP_TRANSFER_TIMEOUT", "The upload chunk made no progress."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except Exception as exc:
            error = ManualSftpError(
                "SFTP_UPLOAD_WRITE_FAILED", "The remote upload chunk failed."
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        state.digest.update(chunk)
        state.expected_sequence += 1
        state.expected_offset += len(chunk)
        return UploadChunkAck(
            operation_id=operation_id,
            sequence=sequence,
            offset=offset,
            accepted_bytes=len(chunk),
        )

    async def finish(self, operation_id: UUID) -> OperationTerminalProjection:
        """Verify complete bytes/temp/target and perform one flagged atomic rename."""

        state = self._require_upload(operation_id)
        if (
            state.expected_offset != state.source_byte_count
            or state.digest.hexdigest() != state.source_sha256
        ):
            error = ManualSftpError(
                "SFTP_SOURCE_HASH_MISMATCH",
                "The uploaded bytes do not match the frozen local source.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        self._put_upload_state(state, "verifying")
        await _close_handle(state)
        try:
            temp_snapshot = await _snapshot(
                state.lease.client, state.temp_path, include_hash=True
            )
        except ManualSftpError as exc:
            await self._fail_upload(state, exc, remove_temp=True)
            raise
        if (
            temp_snapshot.entry_type != "file"
            or temp_snapshot.size != state.source_byte_count
            or temp_snapshot.sha256 != state.source_sha256
        ):
            error = ManualSftpError(
                "SFTP_REMOTE_TEMP_VERIFICATION_FAILED",
                "The remote upload temporary file failed verification.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        try:
            current_target = await _snapshot(
                state.lease.client,
                state.target_path,
                include_hash=state.target_snapshot.exists,
            )
        except ManualSftpError as exc:
            await self._fail_upload(state, exc, remove_temp=True)
            raise
        if current_target != state.target_snapshot:
            error = ManualSftpError(
                "SFTP_TARGET_CHANGED",
                "The remote upload target changed before commit.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error

        self._put_upload_state(state, "committing")
        # AsyncSSH maps any non-zero v3 flags to OpenSSH posix-rename, whose overwrite
        # semantics are unsafe when the frozen target was absent. Standard v3 rename is
        # the protocol's no-clobber form; v5+ can express atomic no-overwrite directly.
        flags = (
            FXR_ATOMIC | FXR_OVERWRITE
            if state.target_snapshot.exists
            else (FXR_ATOMIC if state.lease.client.version >= 5 else 0)
        )
        try:
            await _metadata_await(
                state.lease.client.rename(
                    state.temp_path.encode("utf-8"),
                    state.target_path.encode("utf-8"),
                    flags=flags,
                )
            )
        except asyncssh.SFTPOpUnsupported as exc:
            error = ManualSftpError(
                "SFTP_ATOMIC_REPLACE_UNSUPPORTED",
                "The server does not support the required atomic rename.",
            )
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except (
            asyncssh.SFTPFileAlreadyExists,
            asyncssh.SFTPNoSuchFile,
            asyncssh.SFTPNoSuchPath,
            asyncssh.SFTPPermissionDenied,
        ) as exc:
            error = map_typed_sftp_status(
                exc,
                exists_code="SFTP_TARGET_CHANGED",
            )
            assert error is not None
            await self._fail_upload(state, error, remove_temp=True)
            raise error from exc
        except (asyncssh.SFTPError, ManualSftpError) as exc:
            self._active.pop(operation_id, None)
            receipt = _terminal(
                operation_id,
                "outcome_unknown",
                "SFTP_COMMIT_OUTCOME_UNKNOWN",
                "The remote atomic commit outcome could not be confirmed.",
                recovery_id=operation_id,
            )
            self._put_upload_state(state, "outcome_unknown", receipt)
            await state.lease.close()
            raise ManualSftpError(
                "SFTP_COMMIT_OUTCOME_UNKNOWN",
                "The remote atomic commit outcome could not be confirmed.",
                operation_state="outcome_unknown",
            ) from exc

        self._active.pop(operation_id, None)
        receipt = _terminal(
            operation_id,
            "succeeded",
            None,
            "The remote upload completed atomically.",
            sha256=state.source_sha256,
            byte_count=state.source_byte_count,
        )
        self._put_upload_state(state, "succeeded", receipt)
        await state.lease.close()
        return receipt

    async def abort(self, operation_id: UUID) -> OperationTerminalProjection:
        """Close and remove an active temporary upload without committing it."""

        state = self._require_upload(operation_id)
        receipt = _terminal(
            operation_id, "cancelled", None, "The remote upload was cancelled."
        )
        await self._close_upload(state, remove_temp=True, receipt=receipt)
        return receipt

    async def close_all(self) -> None:
        """Abort every active upload and preserve the first cleanup failure."""

        first_error: BaseException | None = None
        for operation_id in tuple(self._active):
            try:
                await self.abort(operation_id)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _validate_source(source_sha256: str, source_byte_count: int) -> None:
        """Reject untrusted direct callers before opening a remote channel."""

        if (
            len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
            or type(source_byte_count) is not int
            or not 0 <= source_byte_count <= MAX_JS_SAFE_INTEGER
        ):
            raise ManualSftpError(
                "SFTP_SOURCE_SNAPSHOT_INVALID",
                "The frozen upload source snapshot is invalid.",
            )

    def _require_upload(self, operation_id: UUID) -> _UploadState:
        """Return an active upload or distinguish finalized/unknown IDs."""

        state = self._active.get(operation_id)
        if state is not None:
            return state
        record = self._operations.get(operation_id)
        code = (
            "SFTP_OPERATION_ALREADY_FINALIZED"
            if record is not None
            else "SFTP_OPERATION_NOT_FOUND"
        )
        raise ManualSftpError(code, "The remote upload operation is not active.")

    async def _fail_upload(
        self,
        state: _UploadState,
        error: ManualSftpError,
        *,
        remove_temp: bool,
    ) -> None:
        """Persist a failure receipt after deterministic cleanup is attempted."""

        receipt = _terminal(
            state.operation_id,
            "failed",
            error.error_code,
            "The remote upload failed.",
        )
        await self._close_upload(state, remove_temp=remove_temp, receipt=receipt)

    async def _close_upload(
        self,
        state: _UploadState,
        *,
        remove_temp: bool,
        receipt: OperationTerminalProjection,
    ) -> None:
        """Remove active ownership, then close handle/temp/channel in order."""

        self._active.pop(state.operation_id, None)
        first_error: BaseException | None = None
        try:
            await _close_handle(state)
        except BaseException as exc:
            first_error = exc
        if remove_temp:
            try:
                await _metadata_await(
                    state.lease.client.remove(state.temp_path.encode("utf-8"))
                )
            except asyncssh.SFTPNoSuchFile:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            await state.lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is None:
            terminal_state = receipt.state
            self._put_upload_state(state, terminal_state, receipt)
            return
        cleanup_receipt = _terminal(
            state.operation_id,
            "cleanup_required",
            "SFTP_UPLOAD_CLEANUP_REQUIRED",
            "The remote upload temporary file requires manual cleanup.",
            recovery_id=state.operation_id,
        )
        self._put_upload_state(state, "cleanup_required", cleanup_receipt)
        raise ManualSftpError(
            "SFTP_UPLOAD_CLEANUP_REQUIRED",
            "The remote upload temporary file requires manual cleanup.",
            operation_state="cleanup_required",
        ) from first_error

    def _put_upload_state(
        self,
        state: _UploadState,
        record_state: str,
        receipt: OperationTerminalProjection | None = None,
    ) -> None:
        """Persist one complete plaintext upload state transition."""

        self._operations.put(
            RemoteOperationRecord(
                operation_id=state.operation_id,
                kind="upload",
                state=record_state,
                connection_id=state.lease.owner.connection_id,
                connection_profile_version=state.lease.owner.connection_profile_version,
                host_label=state.lease.owner.host_label,
                target_host_key_fingerprint=state.lease.owner.target_host_key_fingerprint,
                jump_connection_id=state.lease.owner.jump_connection_id,
                jump_profile_version=state.lease.owner.jump_profile_version,
                jump_host_key_fingerprint=state.lease.owner.jump_host_key_fingerprint,
                remote_path=state.target_path,
                temp_path=state.temp_path,
                expected_sha256=state.source_sha256,
                target_snapshot=state.target_snapshot,
                terminal_receipt=receipt,
                created_at=state.created_at,
            )
        )


class DownloadManager:
    """Own pull-based remote reads and verify the source before terminal success."""

    def __init__(self, channels: SftpChannelFactory) -> None:
        """Bind isolated channel creation and initialize operation registries."""

        self._channels = channels
        self._active: dict[UUID, _DownloadState] = {}
        self._finalized: set[UUID] = set()

    async def begin(
        self, *, operation_id: UUID, ssh_session_id: UUID, path: str
    ) -> DownloadReady:
        """Hash and freeze a regular source before opening its pull handle."""

        if operation_id in self._active:
            raise ManualSftpError(
                "SFTP_OPERATION_DUPLICATE",
                "The manual SFTP operation ID cannot be reused.",
            )
        if operation_id in self._finalized:
            raise ManualSftpError(
                "SFTP_OPERATION_ALREADY_FINALIZED",
                "The manual SFTP operation ID is already finalized.",
            )
        source_path = validate_remote_path(path)
        lease = await self._channels.open(ssh_session_id)
        try:
            snapshot = await _snapshot(
                lease.client, source_path, include_hash=True
            )
            if snapshot.entry_type != "file" or snapshot.sha256 is None:
                raise ManualSftpError(
                    "SFTP_NOT_A_REGULAR_FILE",
                    "The selected remote entry is not a regular file.",
                )
            if snapshot.size is None:
                raise ManualSftpError(
                    "SFTP_ATTRIBUTES_INCOMPLETE",
                    "The remote file size is required for download.",
                )
            handle = await _metadata_await(
                lease.client.open(source_path.encode("utf-8"), "rb")
            )
        except BaseException:
            await lease.close()
            raise
        state = _DownloadState(
            operation_id=operation_id,
            lease=lease,
            handle=handle,
            source_path=source_path,
            source_snapshot=snapshot,
            expected_sequence=0,
            expected_offset=0,
            digest=hashlib.sha256(),
            eof=snapshot.size == 0,
        )
        self._active[operation_id] = state
        return DownloadReady(
            operation_id=operation_id,
            path=source_path,
            snapshot=snapshot,
            sha256=snapshot.sha256,
            byte_count=snapshot.size,
            next_sequence=0,
            next_offset=0,
        )

    async def read_chunk(
        self, operation_id: UUID, *, sequence: int, offset: int
    ) -> DownloadChunk:
        """Return exactly one sequential bounded canonical Base64 chunk."""

        state = self._require_download(operation_id)
        if state.eof:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_OPERATION_ALREADY_FINALIZED",
                "The remote download has no additional chunk.",
            )
        if sequence != state.expected_sequence or offset != state.expected_offset:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_PROTOCOL_SEQUENCE_INVALID",
                "The download sequence or offset is invalid.",
            )
        try:
            async with asyncio.timeout(CHUNK_REQUEST_TIMEOUT_SECONDS):
                chunk = await state.handle.read(SFTP_CHUNK_BYTES)
        except TimeoutError as exc:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_TRANSFER_TIMEOUT", "The download chunk made no progress."
            ) from exc
        if not isinstance(chunk, bytes) or len(chunk) > SFTP_CHUNK_BYTES:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_REMOTE_READ_INVALID",
                "The remote file returned an invalid read chunk.",
            )
        if not chunk:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_DOWNLOAD_VERIFICATION_FAILED",
                "The remote file ended before its frozen size.",
            )
        current_sequence = state.expected_sequence
        current_offset = state.expected_offset
        state.expected_sequence += 1
        state.expected_offset += len(chunk)
        state.digest.update(chunk)
        expected_size = state.source_snapshot.size
        state.eof = expected_size is not None and state.expected_offset >= expected_size
        return DownloadChunk(
            operation_id=operation_id,
            sequence=current_sequence,
            offset=current_offset,
            data=chunk,
            next_offset=state.expected_offset,
            eof=state.eof,
        )

    async def finish(self, operation_id: UUID) -> OperationTerminalProjection:
        """Require EOF, matching stream hash, and unchanged final metadata."""

        state = self._require_download(operation_id)
        expected_size = state.source_snapshot.size
        if (
            not state.eof
            or expected_size is None
            or state.expected_offset != expected_size
            or state.digest.hexdigest() != state.source_snapshot.sha256
        ):
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_DOWNLOAD_VERIFICATION_FAILED",
                "The downloaded bytes failed source verification.",
            )
        final = await _snapshot(
            state.lease.client, state.source_path, include_hash=False
        )
        initial_without_hash = state.source_snapshot.model_copy(update={"sha256": None})
        if final != initial_without_hash:
            await self._close_download(state)
            raise ManualSftpError(
                "SFTP_TARGET_CHANGED", "The remote download source changed."
            )
        receipt = _terminal(
            operation_id,
            "succeeded",
            None,
            "The remote download source was verified.",
            sha256=state.source_snapshot.sha256,
            byte_count=state.expected_offset,
        )
        await self._close_download(state)
        return receipt

    async def abort(self, operation_id: UUID) -> OperationTerminalProjection:
        """Close only the remote read handle and channel."""

        state = self._require_download(operation_id)
        receipt = _terminal(
            operation_id, "cancelled", None, "The remote download was cancelled."
        )
        await self._close_download(state)
        return receipt

    async def close_all(self) -> None:
        """Close every active remote download while preserving first failure."""

        first_error: BaseException | None = None
        for state in tuple(self._active.values()):
            try:
                await self._close_download(state)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _require_download(self, operation_id: UUID) -> _DownloadState:
        """Return an active download or distinguish finalized/unknown IDs."""

        state = self._active.get(operation_id)
        if state is not None:
            return state
        code = (
            "SFTP_OPERATION_ALREADY_FINALIZED"
            if operation_id in self._finalized
            else "SFTP_OPERATION_NOT_FOUND"
        )
        raise ManualSftpError(code, "The remote download operation is not active.")

    async def _close_download(self, state: _DownloadState) -> None:
        """Remove registry ownership and deterministically close read resources."""

        self._active.pop(state.operation_id, None)
        self._finalized.add(state.operation_id)
        first_error: BaseException | None = None
        if state.handle is not None:
            try:
                await _metadata_await(state.handle.close())
            except BaseException as exc:
                first_error = exc
            finally:
                state.handle = None
        try:
            await state.lease.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise ManualSftpError(
                "SFTP_DOWNLOAD_CLEANUP_FAILED",
                "The remote download resources could not be closed.",
            ) from first_error


async def _snapshot(
    client: Any, path: str, *, include_hash: bool
) -> TransferSnapshot:
    """Capture no-follow metadata and optional mandatory regular-file hash."""

    try:
        attrs = await _metadata_await(client.lstat(path.encode("utf-8")))
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
        return TransferSnapshot(
            path=path,
            exists=False,
            entry_type=None,
            size=None,
            mtime_ns=None,
            sha256=None,
        )
    entry = remote_entry(path, attrs)
    snapshot = TransferSnapshot(
        path=path,
        exists=True,
        entry_type=entry.entry_type,
        size=entry.size,
        mtime_ns=entry.mtime_ns,
        sha256=None,
    )
    if not include_hash or entry.entry_type != "file":
        return snapshot
    digest, byte_count = await _hash_file(client, path)
    final_attrs = await _metadata_await(client.lstat(path.encode("utf-8")))
    final_entry = remote_entry(path, final_attrs)
    final_snapshot = TransferSnapshot(
        path=path,
        exists=True,
        entry_type=final_entry.entry_type,
        size=final_entry.size,
        mtime_ns=final_entry.mtime_ns,
        sha256=None,
    )
    if final_snapshot != snapshot or (
        snapshot.size is not None and snapshot.size != byte_count
    ):
        raise ManualSftpError(
            "SFTP_TARGET_CHANGED", "The remote file changed while being hashed."
        )
    return snapshot.model_copy(update={"sha256": digest})


async def _hash_file(client: Any, path: str) -> tuple[str, int]:
    """Hash a complete regular file through bounded no-progress reads."""

    digest = hashlib.sha256()
    byte_count = 0
    handle = await _metadata_await(client.open(path.encode("utf-8"), "rb"))
    async with handle:
        while True:
            try:
                async with asyncio.timeout(NO_PROGRESS_TIMEOUT_SECONDS):
                    chunk = await handle.read(HASH_CHUNK_BYTES)
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
    return digest.hexdigest(), byte_count


async def _close_handle(state: _UploadState) -> None:
    """Close an upload handle exactly once before verification or cleanup."""

    if state.handle is not None:
        handle = state.handle
        state.handle = None
        await _metadata_await(handle.close())


async def _metadata_await(awaitable: Any) -> Any:
    """Apply the fixed single-request deadline without retrying remote I/O."""

    try:
        async with asyncio.timeout(METADATA_TIMEOUT_SECONDS):
            return await awaitable
    except TimeoutError as exc:
        raise ManualSftpError(
            "SFTP_OPERATION_TIMEOUT",
            "The SFTP metadata or mutation request timed out.",
        ) from exc


def _upload_temp_path(target_path: str, operation_id: UUID) -> str:
    """Place one unguessable exclusive upload temp beside its final target."""

    parent = posixpath.dirname(target_path) or "/"
    return posixpath.join(parent, f".harness-shell-upload-{operation_id}.part")


def _terminal(
    operation_id: UUID,
    state: str,
    error_code: str | None,
    message: str,
    *,
    sha256: str | None = None,
    byte_count: int | None = None,
    recovery_id: UUID | None = None,
) -> OperationTerminalProjection:
    """Build one strict, bounded terminal projection."""

    return OperationTerminalProjection(
        operation_id=operation_id,
        state=state,
        error_code=error_code,
        message=message,
        sha256=sha256,
        byte_count=byte_count,
        recovery_id=recovery_id,
    )


def _utc_now() -> str:
    """Return a stable RFC 3339 UTC timestamp for plaintext records."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
