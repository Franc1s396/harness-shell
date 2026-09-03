"""Strict transport-independent handlers for manual SFTP domain operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .errors import ManualSftpError
from .models import (
    ChunkSequence,
    DownloadChunk,
    JsSafeInt,
    Sha256Hex,
    TransferSnapshot,
    UploadChunkAck,
)


class _ManualSftpServiceProtocol(Protocol):
    """Minimal service interface consumed by the browse handlers."""

    async def open(self, ssh_session_id: UUID): ...
    async def list_begin(self, ssh_session_id: UUID, path: str): ...
    async def list_next(self, listing_id: UUID, sequence: int): ...
    async def list_close(self, listing_id: UUID) -> None: ...
    async def lstat(self, ssh_session_id: UUID, path: str): ...
    async def readlink(self, ssh_session_id: UUID, path: str): ...
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
    def list_recoveries(self) -> tuple[RecoverySummary, ...]: ...
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
    """Begin an upload from React File metadata and a remote target snapshot."""

    operation_id: UUID
    source_sha256: Sha256Hex
    source_byte_count: JsSafeInt
    target_snapshot: TransferSnapshot


class _DownloadBeginParams(_SessionPathParams):
    """Begin a pull-based download from one remote path."""

    operation_id: UUID


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
    """Consume one durable plaintext recursive-delete plan."""

    delete_plan_id: UUID


class _DeletePreflightParams(_SessionPathParams):
    """Build one delete plan under the caller-selected durable identity."""

    operation_id: UUID


class _RecoveryParams(_StrictParams):
    """Select one remote-only recovery record."""

    recovery_id: UUID


class _RecoveryListParams(_StrictParams):
    """Require an empty object for recovery listing."""


class _RecoveryExecuteParams(_RecoveryParams):
    """Select one allowlisted new recovery action."""

    operation_id: UUID
    action: Literal[
        "verify",
        "delete_temp",
        "continue_delete",
        "restore_tombstone",
        "keep",
    ]


class ManualSftpApplication:
    """Expose binary SFTP chunks without choosing a wire encoding."""

    def __init__(self, service: _ManualSftpServiceProtocol) -> None:
        """Bind the application boundary to the sole SFTP domain owner."""

        self._service = service

    async def upload_chunk(
        self,
        context: RequestContext,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
        chunk: bytes,
    ) -> UploadChunkAck:
        """Accept one bounded binary chunk for the owned upload operation."""

        context.require_active()
        return await self._service.upload_chunk(
            operation_id,
            sequence=sequence,
            offset=offset,
            chunk=chunk,
        )

    async def download_chunk(
        self,
        context: RequestContext,
        operation_id: UUID,
        *,
        sequence: int,
        offset: int,
    ) -> DownloadChunk:
        """Return one raw bounded binary chunk from the owned download."""

        context.require_active()
        return await self._service.download_chunk(
            operation_id,
            sequence=sequence,
            offset=offset,
        )


def register_manual_sftp_handlers(
    dispatcher: RequestDispatcher, service: _ManualSftpServiceProtocol
) -> None:
    """Register typed JSON SFTP operations on the shared dispatcher."""

    async def open_context(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _SessionParams)
        context.require_active()
        try:
            context = await service.open(params.ssh_session_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"context": context.model_dump(mode="json")}

    async def list_begin(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _SessionPathParams)
        context.require_active()
        try:
            batch = await service.list_begin(params.ssh_session_id, params.path)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"batch": batch.model_dump(mode="json")}

    async def list_next(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ListingNextParams)
        context.require_active()
        try:
            batch = await service.list_next(params.listing_id, params.sequence)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"batch": batch.model_dump(mode="json")}

    async def list_close(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ListingCloseParams)
        context.require_active()
        try:
            await service.list_close(params.listing_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"closed": True}

    async def lstat(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _entry_result(context, raw_params, service.lstat)

    async def readlink(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _entry_result(context, raw_params, service.readlink)

    async def sha256(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _SessionPathParams)
        context.require_active()
        try:
            result = await service.sha256(
                params.ssh_session_id,
                params.path,
                cancelled=context.cancelled,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"hash": result.model_dump(mode="json")}

    async def upload_preflight(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _SessionPathParams)
        context.require_active()
        try:
            snapshot = await service.upload_preflight(
                params.ssh_session_id, params.path
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"snapshot": snapshot.model_dump(mode="json")}

    async def upload_begin(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _UploadBeginParams)
        context.require_active()
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

    async def upload_finish(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _terminal_result(context, raw_params, service.upload_finish)

    async def upload_abort(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _terminal_result(context, raw_params, service.upload_abort)

    async def download_begin(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _DownloadBeginParams)
        context.require_active()
        try:
            download = await service.download_begin(
                operation_id=params.operation_id,
                ssh_session_id=params.ssh_session_id,
                path=params.path,
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"download": download.model_dump(mode="json")}

    async def download_finish(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _terminal_result(context, raw_params, service.download_finish)

    async def download_abort(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        return await _terminal_result(context, raw_params, service.download_abort)

    async def mkdir(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _MkdirParams)
        context.require_active()
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

    async def rename(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _RenameParams)
        context.require_active()
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

    async def remove(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _RemoveParams)
        context.require_active()
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
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _DeletePreflightParams)
        context.require_active()
        try:
            plan = await service.delete_preflight(
                params.operation_id, params.ssh_session_id, params.path
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"delete_plan": plan.model_dump(mode="json")}

    async def delete_execute(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _DeleteExecuteParams)
        context.require_active()
        try:
            terminal = await service.delete_execute(params.delete_plan_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"terminal": terminal.model_dump(mode="json")}

    async def recovery_list(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        _params(raw_params, _RecoveryListParams)
        context.require_active()
        try:
            recoveries = service.list_recoveries()
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {
            "recoveries": [value.model_dump(mode="json") for value in recoveries]
        }

    async def recovery_inspect(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _RecoveryParams)
        context.require_active()
        try:
            result = await service.recovery_inspect(params.recovery_id)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"recovery": result.model_dump(mode="json")}

    async def recovery_execute(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _RecoveryExecuteParams)
        context.require_active()
        try:
            result = await service.recovery_execute(
                params.recovery_id, params.action, params.operation_id
            )
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"recovery": result.model_dump(mode="json")}

    async def _entry_result(
        context: RequestContext,
        raw_params: Mapping[str, object],
        operation,
    ) -> dict[str, object]:
        params = _params(raw_params, _SessionPathParams)
        context.require_active()
        try:
            entry = await operation(params.ssh_session_id, params.path)
        except ManualSftpError as exc:
            raise _dispatch_error(exc) from exc
        return {"entry": entry.model_dump(mode="json")}

    async def _terminal_result(
        context: RequestContext,
        raw_params: Mapping[str, object],
        operation,
    ) -> dict[str, object]:
        params = _params(raw_params, _OperationParams)
        context.require_active()
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
    dispatcher.register("manual_sftp.sha256", sha256)
    dispatcher.register("manual_sftp.upload.preflight", upload_preflight)
    dispatcher.register("manual_sftp.upload.begin", upload_begin)
    dispatcher.register("manual_sftp.upload.finish", upload_finish)
    dispatcher.register("manual_sftp.upload.abort", upload_abort)
    dispatcher.register("manual_sftp.download.begin", download_begin)
    dispatcher.register("manual_sftp.download.finish", download_finish)
    dispatcher.register("manual_sftp.download.abort", download_abort)
    dispatcher.register("manual_sftp.mkdir", mkdir)
    dispatcher.register("manual_sftp.rename", rename)
    dispatcher.register("manual_sftp.remove", remove)
    dispatcher.register("manual_sftp.delete.preflight", delete_preflight)
    dispatcher.register("manual_sftp.delete.execute", delete_execute)
    dispatcher.register("manual_sftp.recovery.list", recovery_list)
    dispatcher.register("manual_sftp.recovery.inspect", recovery_inspect)
    dispatcher.register("manual_sftp.recovery.execute", recovery_execute)


def _params(raw_params: Mapping[str, object], model: type[BaseModel]):
    """Parse one strict params object without Python-side coercion."""

    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(dict(raw_params)))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


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
