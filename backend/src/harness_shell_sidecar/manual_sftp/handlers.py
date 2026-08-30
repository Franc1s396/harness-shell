"""Strict Protocol v1 handlers for manual SFTP browse and metadata methods."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.protocol import FrameEnvelope, Sensitivity
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher

from .errors import ManualSftpError
from .models import ChunkSequence, JsSafeInt, Sha256Hex, TransferSnapshot
from .transfers import SFTP_CHUNK_BYTES


class _ManualSftpServiceProtocol(Protocol):
    """Minimal service interface consumed by the browse handlers."""

    async def open(self, ssh_session_id: UUID): ...
    async def list_begin(self, ssh_session_id: UUID, path: str): ...
    async def list_next(self, listing_id: UUID, sequence: int): ...
    async def list_close(self, listing_id: UUID) -> None: ...
    async def lstat(self, ssh_session_id: UUID, path: str): ...
    async def readlink(self, ssh_session_id: UUID, path: str): ...
    async def realpath(self, ssh_session_id: UUID, path: str): ...
    async def sha256(
        self,
        ssh_session_id: UUID,
        path: str,
        *,
        cancelled: asyncio.Event | None = None,
    ): ...
    async def upload_preflight(self, ssh_session_id: UUID, path: str): ...
    async def upload_begin(self, **kwargs): ...
    async def upload_chunk(self, operation_id: UUID, **kwargs): ...
    async def upload_finish(self, operation_id: UUID): ...
    async def upload_abort(self, operation_id: UUID): ...
    async def download_begin(self, **kwargs): ...
    async def download_chunk(self, operation_id: UUID, **kwargs): ...
    async def download_finish(self, operation_id: UUID): ...
    async def download_abort(self, operation_id: UUID): ...
    async def mkdir(self, **kwargs): ...
    async def rename(self, **kwargs): ...
    async def remove(self, **kwargs): ...
    async def delete_preflight(
        self, operation_id: UUID, ssh_session_id: UUID, path: str
    ): ...
    async def delete_execute(self, delete_plan_id: UUID): ...
    async def recovery_inspect(self, recovery_id: UUID): ...
    async def recovery_execute(
        self, recovery_id: UUID, action: str, operation_id: UUID
    ): ...


class _StrictParams(BaseModel):
    """Reject unknown fields and coercion in manual SFTP requests."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _SessionParams(_StrictParams):
    """Select one explicit live SSH session."""

    ssh_session_id: UUID


class _SessionPathParams(_SessionParams):
    """Select one absolute remote path on a live SSH session."""

    path: str


class _ListingNextParams(_StrictParams):
    """Continue one listing at its exact next sequence."""

    listing_id: UUID
    sequence: ChunkSequence


class _ListingCloseParams(_StrictParams):
    """Select one listing cursor for explicit close."""

    listing_id: UUID


class _OperationParams(_StrictParams):
    """Select one transfer operation by stable identity."""

    operation_id: UUID


class _UploadBeginParams(_SessionPathParams):
    """Begin an upload from canonical privileged source/target snapshots."""

    operation_id: UUID
    source_sha256: Sha256Hex
    source_byte_count: JsSafeInt
    target_snapshot: TransferSnapshot


class _UploadChunkParams(_OperationParams):
    """Carry one sequential upload chunk in canonical Base64."""

    sequence: ChunkSequence
    offset: JsSafeInt
    chunk_b64: str


class _DownloadBeginParams(_SessionPathParams):
    """Begin a pull-based download from one remote path."""

    operation_id: UUID


class _DownloadChunkParams(_OperationParams):
    """Request one sequential download chunk by exact position."""

    sequence: ChunkSequence
    offset: JsSafeInt


class _MkdirParams(_SessionParams):
    """Create one basename under one validated parent path."""

    operation_id: UUID
    parent_path: str
    name: str


class _RenameParams(_SessionParams):
    """Carry canonical snapshots for one atomic remote rename."""

    operation_id: UUID
    source_path: str
    target_path: str
    overwrite: bool
    source_snapshot: TransferSnapshot | None = None
    target_snapshot: TransferSnapshot | None = None


class _RemoveParams(_SessionPathParams):
    """Remove one entry only when its canonical snapshot still matches."""

    operation_id: UUID
    expected_snapshot: TransferSnapshot


class _DeleteExecuteParams(_StrictParams):
    """Consume one encrypted recursive-delete plan."""

    delete_plan_id: UUID


class _DeletePreflightParams(_SessionPathParams):
    """Build one delete plan under the caller-selected durable identity."""

    operation_id: UUID


class _RecoveryParams(_StrictParams):
    """Select one encrypted recovery record."""

    recovery_id: UUID


class _RecoveryExecuteParams(_RecoveryParams):
    """Select one allowlisted new recovery action."""

    operation_id: UUID
    action: Literal[
        "verify",
        "delete_temp",
        "continue_delete",
        "restore_tombstone",
        "open_local_folder",
        "keep",
    ]


def register_manual_sftp_handlers(
    dispatcher: RequestDispatcher, service: _ManualSftpServiceProtocol
) -> None:
    """Register exactly the eight user-operated read methods from Task 2."""

    async def open_context(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _SessionParams)
        _require_active(cancelled)
        try:
            context = await service.open(params.ssh_session_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"context": context.model_dump(mode="json")}

    async def list_begin(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _SessionPathParams)
        _require_active(cancelled)
        try:
            batch = await service.list_begin(params.ssh_session_id, params.path)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"batch": batch.model_dump(mode="json")}

    async def list_next(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _ListingNextParams)
        _require_active(cancelled)
        try:
            batch = await service.list_next(params.listing_id, params.sequence)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"batch": batch.model_dump(mode="json")}

    async def list_close(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _ListingCloseParams)
        _require_active(cancelled)
        try:
            await service.list_close(params.listing_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"closed": True}

    async def lstat(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _entry_result(frame, cancelled, service.lstat)

    async def readlink(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _entry_result(frame, cancelled, service.readlink)

    async def realpath(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _entry_result(frame, cancelled, service.realpath)

    async def sha256(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _SessionPathParams)
        _require_active(cancelled)
        try:
            result = await service.sha256(
                params.ssh_session_id, params.path, cancelled=cancelled
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"hash": result.model_dump(mode="json")}

    async def upload_preflight(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _SessionPathParams)
        _require_active(cancelled)
        try:
            snapshot = await service.upload_preflight(
                params.ssh_session_id, params.path
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"snapshot": snapshot.model_dump(mode="json")}

    async def upload_begin(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _UploadBeginParams)
        _require_active(cancelled)
        try:
            upload = await service.upload_begin(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                path=params.path,
                source_sha256=params.source_sha256,
                source_byte_count=params.source_byte_count,
                target_snapshot=params.target_snapshot,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"upload": upload.model_dump(mode="json")}

    async def upload_chunk(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        _require_secret(frame, "manual_sftp.upload.chunk")
        params = _params(frame, _UploadChunkParams)
        chunk = _decode_chunk(params.chunk_b64)
        _require_active(cancelled)
        try:
            result = await service.upload_chunk(
                params.operation_id,
                sequence=params.sequence,
                offset=params.offset,
                chunk=chunk,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"chunk": result.model_dump(mode="json")}

    async def upload_finish(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _terminal_result(frame, cancelled, service.upload_finish)

    async def upload_abort(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _terminal_result(frame, cancelled, service.upload_abort)

    async def download_begin(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _DownloadBeginParams)
        _require_active(cancelled)
        try:
            download = await service.download_begin(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                path=params.path,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"download": download.model_dump(mode="json")}

    async def download_chunk(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        _require_secret(frame, "manual_sftp.download.chunk")
        params = _params(frame, _DownloadChunkParams)
        _require_active(cancelled)
        try:
            chunk = await service.download_chunk(
                params.operation_id,
                sequence=params.sequence,
                offset=params.offset,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"chunk": chunk.model_dump(mode="json")}

    async def download_finish(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        return await _terminal_result(frame, cancelled, service.download_finish)

    async def download_abort(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        return await _terminal_result(frame, cancelled, service.download_abort)

    async def mkdir(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _MkdirParams)
        _require_active(cancelled)
        try:
            terminal = await service.mkdir(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                parent_path=params.parent_path,
                name=params.name,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    async def rename(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _RenameParams)
        _require_active(cancelled)
        try:
            terminal = await service.rename(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                source_path=params.source_path,
                target_path=params.target_path,
                overwrite=params.overwrite,
                source_snapshot=params.source_snapshot,
                target_snapshot=params.target_snapshot,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    async def remove(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        params = _params(frame, _RemoveParams)
        _require_active(cancelled)
        try:
            terminal = await service.remove(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                path=params.path,
                expected_snapshot=params.expected_snapshot,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    async def delete_preflight(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _DeletePreflightParams)
        _require_active(cancelled)
        try:
            plan = await service.delete_preflight(
                params.operation_id, params.ssh_session_id, params.path
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"delete_plan": plan.model_dump(mode="json")}

    async def delete_execute(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _DeleteExecuteParams)
        _require_active(cancelled)
        try:
            terminal = await service.delete_execute(params.delete_plan_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    async def recovery_inspect(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _RecoveryParams)
        _require_active(cancelled)
        try:
            result = await service.recovery_inspect(params.recovery_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"recovery": result.model_dump(mode="json")}

    async def recovery_execute(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _RecoveryExecuteParams)
        _require_active(cancelled)
        try:
            result = await service.recovery_execute(
                params.recovery_id, params.action, params.operation_id
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"recovery": result.model_dump(mode="json")}

    async def _entry_result(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
        operation,
    ) -> dict:
        params = _params(frame, _SessionPathParams)
        _require_active(cancelled)
        try:
            entry = await operation(params.ssh_session_id, params.path)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"entry": entry.model_dump(mode="json")}

    async def _terminal_result(
        frame: FrameEnvelope,
        cancelled: asyncio.Event,
        operation,
    ) -> dict:
        params = _params(frame, _OperationParams)
        _require_active(cancelled)
        try:
            terminal = await operation(params.operation_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    dispatcher.register("manual_sftp.open", open_context)
    dispatcher.register("manual_sftp.list.begin", list_begin)
    dispatcher.register("manual_sftp.list.next", list_next)
    dispatcher.register("manual_sftp.list.close", list_close)
    dispatcher.register("manual_sftp.lstat", lstat)
    dispatcher.register("manual_sftp.readlink", readlink)
    dispatcher.register("manual_sftp.realpath", realpath)
    dispatcher.register("manual_sftp.sha256", sha256)
    dispatcher.register("manual_sftp.upload.preflight", upload_preflight)
    dispatcher.register("manual_sftp.upload.begin", upload_begin)
    dispatcher.register("manual_sftp.upload.chunk", upload_chunk)
    dispatcher.register("manual_sftp.upload.finish", upload_finish)
    dispatcher.register("manual_sftp.upload.abort", upload_abort)
    dispatcher.register("manual_sftp.download.begin", download_begin)
    dispatcher.register("manual_sftp.download.chunk", download_chunk)
    dispatcher.register("manual_sftp.download.finish", download_finish)
    dispatcher.register("manual_sftp.download.abort", download_abort)
    dispatcher.register("manual_sftp.mkdir", mkdir)
    dispatcher.register("manual_sftp.rename", rename)
    dispatcher.register("manual_sftp.remove", remove)
    dispatcher.register("manual_sftp.delete.preflight", delete_preflight)
    dispatcher.register("manual_sftp.delete.execute", delete_execute)
    dispatcher.register("manual_sftp.recovery.inspect", recovery_inspect)
    dispatcher.register("manual_sftp.recovery.execute", recovery_execute)


def _params(frame: FrameEnvelope, model: type[BaseModel]):
    """Parse one strict params object without Python-side coercion."""

    params = frame.payload.get("params")
    if not isinstance(params, dict):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(params))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _require_active(cancelled: asyncio.Event) -> None:
    """Reject a request already marked cancelled before remote I/O."""

    if cancelled.is_set():
        raise DispatchError("REQUEST_CANCELLED", "request was cancelled")


def _require_secret(frame: FrameEnvelope, method: str) -> None:
    """Require a redacted Protocol frame for any request/response carrying bytes."""

    if frame.sensitivity is not Sensitivity.SECRET:
        raise DispatchError(
            "SENSITIVE_FRAME_REQUIRED", f"{method} requires a secret frame"
        )


def _decode_chunk(encoded: str) -> bytes:
    """Decode only non-empty canonical Base64 within the fixed chunk limit."""

    try:
        chunk = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DispatchError(
            "SFTP_CHUNK_INVALID", "SFTP chunk must use canonical base64"
        ) from exc
    if not chunk or base64.b64encode(chunk).decode("ascii") != encoded:
        raise DispatchError(
            "SFTP_CHUNK_INVALID", "SFTP chunk must contain 1..262144 bytes"
        )
    if len(chunk) > SFTP_CHUNK_BYTES:
        raise DispatchError(
            "SFTP_CHUNK_LIMIT_EXCEEDED", "SFTP chunk must contain at most 262144 bytes"
        )
    return chunk


def _dispatch_error(error: ManualSftpError) -> DispatchError:
    """Map a bounded domain error without exposing its internal exception."""

    details = (
        {"operation_state": error.operation_state}
        if error.operation_state is not None
        else None
    )
    return DispatchError(
        error.error_code,
        "Manual SFTP operation failed",
        details=details,
    )
