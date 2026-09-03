"""Typed JSON and raw-binary HTTP routes for user-operated Manual SFTP."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from pydantic import Field

from harness_shell_sidecar.manual_sftp.errors import ManualSftpError
from harness_shell_sidecar.manual_sftp.models import (
    ChunkSequence,
    DeletePlanSummary,
    DownloadChunk,
    DownloadReady,
    JsSafeInt,
    ListingBatch,
    ManualSftpContext,
    OperationTerminalProjection,
    RecoverySummary,
    RemoteEntry,
    RemoteFileHash,
    Sha256Hex,
    TransferSnapshot,
    UploadChunkAck,
    UploadReady,
)
from harness_shell_sidecar.manual_sftp.transfers import SFTP_CHUNK_BYTES
from harness_shell_sidecar.runtime.dispatcher import DispatchError

from ..dependencies import (
    dispatch_application,
    dispatch_error_problem,
    model_from_result,
    require_ready_resources,
    require_request_id,
    runtime_owner,
    set_correlation,
    validate_json_model,
)
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner
from ..limits import ResponseLimitRoute
from ..models import StrictHttpModel


_CANONICAL_UINT = re.compile(r"(?:0|[1-9][0-9]*)\Z")
HttpChunkSequence = Annotated[int, Path(ge=0, le=2**31 - 1)]


class SftpSessionRequest(StrictHttpModel):
    """Select one explicitly connected SSH session."""

    #: Live SSH session that owns the SFTP channel.
    ssh_session_id: UUID


class SftpPathRequest(SftpSessionRequest):
    """Select one remote path inside a strict JSON body."""

    #: Absolute remote POSIX path; never encoded in the URL path.
    path: str


class SftpUploadBeginRequest(SftpPathRequest):
    """Freeze the local source and remote target identity for an upload."""

    #: React-selected fresh operation identity.
    operation_id: UUID
    #: Complete local source SHA-256.
    source_sha256: Sha256Hex
    #: Complete local source size.
    source_byte_count: JsSafeInt
    #: Remote target snapshot returned by preflight.
    target_snapshot: TransferSnapshot


class SftpDownloadBeginRequest(SftpPathRequest):
    """Begin a download under one fresh durable operation identity."""

    #: React-selected fresh operation identity.
    operation_id: UUID


class SftpMkdirRequest(SftpSessionRequest):
    """Create one basename under a previously validated parent."""

    #: React-selected fresh operation identity.
    operation_id: UUID
    #: Absolute parent directory path.
    parent_path: str
    #: Single child basename.
    name: str


class SftpRenameRequest(SftpSessionRequest):
    """Rename one unchanged source to one validated target."""

    #: React-selected fresh operation identity.
    operation_id: UUID
    #: Absolute source path.
    source_path: str
    #: Absolute target path.
    target_path: str
    #: Whether an unchanged target may be atomically replaced.
    overwrite: bool
    #: Optional source snapshot frozen by the UI flow.
    source_snapshot: TransferSnapshot | None = None
    #: Optional target snapshot frozen by the UI flow.
    target_snapshot: TransferSnapshot | None = None


class SftpRemoveRequest(SftpPathRequest):
    """Remove one entry only when its complete snapshot still matches."""

    #: React-selected fresh operation identity.
    operation_id: UUID
    #: Snapshot that authorizes the mutation.
    expected_snapshot: TransferSnapshot


class SftpDeletePreflightRequest(SftpPathRequest):
    """Build a complete recursive-delete plan without deleting anything."""

    #: React-selected fresh operation identity.
    operation_id: UUID


class SftpRecoveryActionRequest(StrictHttpModel):
    """Execute one allowlisted recovery action under a fresh identity."""

    #: React-selected fresh recovery mutation identity.
    operation_id: UUID
    #: Explicit user-confirmed recovery action.
    action: Literal[
        "verify",
        "delete_temp",
        "continue_delete",
        "restore_tombstone",
        "keep",
    ]


class SftpContextResponse(StrictHttpModel):
    """Return one safe Manual SFTP context."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Safe remote context projection.
    context: ManualSftpContext


class SftpListingResponse(StrictHttpModel):
    """Return one bounded listing batch."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Bounded listing page.
    batch: ListingBatch


class SftpClosedResponse(StrictHttpModel):
    """Acknowledge explicit listing cursor closure."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Determined close result.
    closed: bool


class SftpEntryResponse(StrictHttpModel):
    """Return one safe no-follow remote metadata projection."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Remote entry projection.
    entry: RemoteEntry


class SftpHashResponse(StrictHttpModel):
    """Return one complete remote file hash."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Hash tied to a verified snapshot.
    hash: RemoteFileHash


class SftpSnapshotResponse(StrictHttpModel):
    """Return one remote target preflight snapshot."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Complete preflight snapshot.
    snapshot: TransferSnapshot


class SftpUploadResponse(StrictHttpModel):
    """Return one ready upload owner."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Ready upload projection.
    upload: UploadReady


class SftpUploadChunkResponse(StrictHttpModel):
    """Acknowledge an exact raw upload chunk without Base64."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Transfer operation identity.
    operation_id: UUID
    #: Accepted request sequence.
    sequence: ChunkSequence
    #: Accepted request offset.
    offset: JsSafeInt
    #: Number of raw bytes durably accepted by the domain owner.
    accepted_bytes: Annotated[int, Field(ge=1, le=SFTP_CHUNK_BYTES, strict=True)]


class SftpDownloadResponse(StrictHttpModel):
    """Return one ready download owner."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Ready download projection.
    download: DownloadReady


class SftpTerminalResponse(StrictHttpModel):
    """Return one trustworthy terminal or recovery-required outcome."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Durable operation terminal projection.
    terminal: OperationTerminalProjection


class SftpDeletePlanResponse(StrictHttpModel):
    """Return one complete recursive-delete preflight summary."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Complete one-shot delete plan summary.
    delete_plan: DeletePlanSummary


class SftpRecoveryResponse(StrictHttpModel):
    """Return one safe recovery inspection or action result."""

    #: HTTP correlation identity.
    request_id: UUID
    #: Safe recovery summary.
    recovery: RecoverySummary


class SftpRecoveryListResponse(StrictHttpModel):
    """Return all remote-only recovery summaries in stable creation order."""

    request_id: UUID
    recoveries: list[RecoverySummary]


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


def _params(payload: dict[str, object], model, request_id: UUID) -> dict[str, object]:
    """Validate one JSON body and return its dispatcher representation."""

    return validate_json_model(payload, model, request_id).model_dump(mode="json")


def _problem(
    request_id: UUID,
    *,
    status_code: int,
    error_code: str,
    title: str,
    message: str,
) -> HttpProblem:
    """Build one fixed binary-contract problem without request-derived text."""

    return HttpProblem(
        build_problem(
            request_id=request_id,
            status=status_code,
            error_code=error_code,
            title=title,
            message=message,
        )
    )


def _single_header(request: Request, name: str) -> str | None:
    """Return one raw header value and reject duplicate wire fields."""

    encoded_name = name.lower().encode("ascii")
    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == encoded_name
    ]
    if len(values) > 1:
        return None
    return values[0] if values else None


async def read_exact_binary_body(
    request: Request,
    *,
    request_id: UUID,
    minimum: int,
    maximum: int,
    required_content_type: str,
) -> bytes:
    """Read a canonical length-delimited binary body exactly once."""

    content_type = _single_header(request, "content-type")
    if content_type != required_content_type:
        raise _problem(
            request_id,
            status_code=415,
            error_code="SFTP_CONTENT_TYPE_INVALID",
            title="Invalid SFTP content type",
            message="SFTP chunks require application/octet-stream",
        )
    raw_length = _single_header(request, "content-length")
    if raw_length is None:
        raise _problem(
            request_id,
            status_code=400,
            error_code="SFTP_CONTENT_LENGTH_REQUIRED",
            title="Content length required",
            message="SFTP chunks require one canonical Content-Length",
        )
    if _CANONICAL_UINT.fullmatch(raw_length) is None:
        raise _problem(
            request_id,
            status_code=400,
            error_code="SFTP_CONTENT_LENGTH_INVALID",
            title="Invalid content length",
            message="Content-Length must be one canonical non-negative integer",
        )
    declared = int(raw_length)
    if declared > maximum:
        raise _problem(
            request_id,
            status_code=413,
            error_code="SFTP_CHUNK_TOO_LARGE",
            title="SFTP chunk too large",
            message="SFTP chunks must contain at most 262144 bytes",
        )
    body = await request.body()
    if len(body) != declared:
        raise _problem(
            request_id,
            status_code=400,
            error_code="SFTP_CONTENT_LENGTH_MISMATCH",
            title="Content length mismatch",
            message="Content-Length does not match the received SFTP chunk",
        )
    if len(body) < minimum:
        raise _problem(
            request_id,
            status_code=422,
            error_code="SFTP_CHUNK_EMPTY",
            title="Empty SFTP chunk",
            message="SFTP upload chunks must contain at least one byte",
        )
    return body


def _parse_chunk_offset(request: Request, request_id: UUID) -> int:
    """Parse one canonical bounded X-Chunk-Offset header."""

    raw = _single_header(request, "x-chunk-offset")
    if raw is None:
        raise _problem(
            request_id,
            status_code=400,
            error_code="SFTP_CHUNK_OFFSET_REQUIRED",
            title="Chunk offset required",
            message="Upload chunks require one X-Chunk-Offset header",
        )
    if _CANONICAL_UINT.fullmatch(raw) is None or int(raw) > 2**53 - 1:
        raise _problem(
            request_id,
            status_code=422,
            error_code="SFTP_CHUNK_OFFSET_INVALID",
            title="Invalid chunk offset",
            message="X-Chunk-Offset must be a JS-safe non-negative integer",
        )
    return int(raw)


def _validate_upload_ack(
    ack: UploadChunkAck,
    *,
    request_id: UUID,
    operation_id: UUID,
    sequence: int,
    offset: int,
    byte_count: int,
) -> None:
    """Reject application identity drift before acknowledging accepted bytes."""

    if (
        ack.operation_id != operation_id
        or ack.sequence != sequence
        or ack.offset != offset
        or ack.accepted_bytes != byte_count
    ):
        raise _problem(
            request_id,
            status_code=502,
            error_code="SIDECAR_RESPONSE_INVALID",
            title="Invalid application response",
            message="The Manual SFTP application response identity is invalid",
        )


def _validate_download_chunk(
    chunk: DownloadChunk,
    *,
    request_id: UUID,
    operation_id: UUID,
    sequence: int,
    offset: int,
) -> None:
    """Reject invalid download state before any raw response bytes are exposed."""

    valid = (
        chunk.operation_id == operation_id
        and chunk.sequence == sequence
        and chunk.offset == offset
        and len(chunk.data) <= SFTP_CHUNK_BYTES
        and chunk.next_offset == offset + len(chunk.data)
        and (bool(chunk.data) or chunk.eof)
    )
    if not valid:
        raise _problem(
            request_id,
            status_code=502,
            error_code="SIDECAR_RESPONSE_INVALID",
            title="Invalid application response",
            message="The Manual SFTP application response identity is invalid",
        )


async def _binary_application_call(owner: RuntimeOwner, request_id: UUID, work):
    """Run raw-byte application work under the shared dispatcher owner."""

    resources = require_ready_resources(owner, request_id)
    try:
        return await resources.dispatcher.execute(request_id, work)
    except ManualSftpError as error:
        details = (
            {"operation_state": error.operation_state}
            if error.operation_state is not None
            else None
        )
        raise dispatch_error_problem(
            request_id,
            DispatchError(error.error_code, error.safe_message, details=details),
        ) from None
    except DispatchError as error:
        raise dispatch_error_problem(request_id, error) from None


@router.post(
    "/v1/sftp/contexts",
    response_model=SftpContextResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_context(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpContextResponse:
    """Open one Manual SFTP context on an active SSH session."""

    result = await dispatch_application(owner, request_id, "manual_sftp.open", _params(payload, SftpSessionRequest, request_id))
    set_correlation(response, request_id)
    return SftpContextResponse(request_id=request_id, context=model_from_result(result["context"], ManualSftpContext))


@router.post("/v1/sftp/listings", response_model=SftpListingResponse, status_code=status.HTTP_201_CREATED)
async def begin_listing(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpListingResponse:
    """Begin one bounded remote directory listing."""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.begin", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpListingResponse(request_id=request_id, batch=model_from_result(result["batch"], ListingBatch))


@router.get("/v1/sftp/listings/{listing_id}/batches/{sequence}", response_model=SftpListingResponse)
async def next_listing(listing_id: UUID, sequence: HttpChunkSequence, response: Response, request_id: CorrelationId, owner: Owner) -> SftpListingResponse:
    """Read the exact next sequence from one listing cursor."""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.next", {"listing_id": str(listing_id), "sequence": sequence})
    set_correlation(response, request_id)
    return SftpListingResponse(request_id=request_id, batch=model_from_result(result["batch"], ListingBatch))


@router.delete("/v1/sftp/listings/{listing_id}", response_model=SftpClosedResponse)
async def close_listing(listing_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpClosedResponse:
    """Close one listing cursor explicitly."""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.close", {"listing_id": str(listing_id)})
    set_correlation(response, request_id)
    return SftpClosedResponse(request_id=request_id, closed=bool(result["closed"]))


async def _entry_operation(operation: str, payload: dict[str, object], response: Response, request_id: UUID, owner: RuntimeOwner) -> SftpEntryResponse:
    """Dispatch one typed remote metadata operation."""

    result = await dispatch_application(owner, request_id, operation, _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpEntryResponse(request_id=request_id, entry=model_from_result(result["entry"], RemoteEntry))


@router.post("/v1/sftp/metadata/lstat", response_model=SftpEntryResponse)
async def lstat(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpEntryResponse:
    """Return no-follow metadata for one remote path."""

    return await _entry_operation("manual_sftp.lstat", payload, response, request_id, owner)


@router.post("/v1/sftp/metadata/readlink", response_model=SftpEntryResponse)
async def readlink(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpEntryResponse:
    """Return one explicitly resolved symlink target projection."""

    return await _entry_operation("manual_sftp.readlink", payload, response, request_id, owner)


@router.post("/v1/sftp/hashes/sha256", response_model=SftpHashResponse)
async def sha256(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpHashResponse:
    """Hash one unchanged remote regular file completely."""

    result = await dispatch_application(owner, request_id, "manual_sftp.sha256", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpHashResponse(request_id=request_id, hash=model_from_result(result["hash"], RemoteFileHash))


@router.post("/v1/sftp/uploads/preflight", response_model=SftpSnapshotResponse)
async def upload_preflight(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpSnapshotResponse:
    """Freeze the current remote upload target before mutation."""

    result = await dispatch_application(owner, request_id, "manual_sftp.upload.preflight", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpSnapshotResponse(request_id=request_id, snapshot=model_from_result(result["snapshot"], TransferSnapshot))


@router.post("/v1/sftp/uploads", response_model=SftpUploadResponse, status_code=status.HTTP_201_CREATED)
async def begin_upload(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpUploadResponse:
    """Begin one no-clobber upload to an exclusive temporary file."""

    result = await dispatch_application(owner, request_id, "manual_sftp.upload.begin", _params(payload, SftpUploadBeginRequest, request_id))
    set_correlation(response, request_id)
    return SftpUploadResponse(request_id=request_id, upload=model_from_result(result["upload"], UploadReady))


@router.put("/v1/sftp/uploads/{operation_id}/chunks/{sequence}", response_model=SftpUploadChunkResponse)
async def upload_chunk(operation_id: UUID, sequence: HttpChunkSequence, request: Request, response: Response, request_id: CorrelationId, owner: Owner) -> SftpUploadChunkResponse:
    """Accept one canonical raw upload chunk under dispatcher ownership."""

    offset = _parse_chunk_offset(request, request_id)
    chunk = await read_exact_binary_body(request, request_id=request_id, minimum=1, maximum=SFTP_CHUNK_BYTES, required_content_type="application/octet-stream")
    resources = require_ready_resources(owner, request_id)

    async def work(context):
        return await resources.manual_sftp_application.upload_chunk(context, operation_id, sequence=sequence, offset=offset, chunk=chunk)

    ack = await _binary_application_call(owner, request_id, work)
    _validate_upload_ack(ack, request_id=request_id, operation_id=operation_id, sequence=sequence, offset=offset, byte_count=len(chunk))
    set_correlation(response, request_id)
    return SftpUploadChunkResponse(request_id=request_id, **ack.model_dump())


async def _terminal_operation(operation: str, operation_id: UUID, response: Response, request_id: UUID, owner: RuntimeOwner) -> SftpTerminalResponse:
    """Finish or abort one transfer and return its durable outcome."""

    result = await dispatch_application(owner, request_id, operation, {"operation_id": str(operation_id)})
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.post("/v1/sftp/uploads/{operation_id}/finish", response_model=SftpTerminalResponse)
async def finish_upload(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Verify and atomically commit one complete upload."""

    return await _terminal_operation("manual_sftp.upload.finish", operation_id, response, request_id, owner)


@router.post("/v1/sftp/uploads/{operation_id}/abort", response_model=SftpTerminalResponse)
async def abort_upload(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Abort one upload and report any required cleanup."""

    return await _terminal_operation("manual_sftp.upload.abort", operation_id, response, request_id, owner)


@router.post("/v1/sftp/downloads", response_model=SftpDownloadResponse, status_code=status.HTTP_201_CREATED)
async def begin_download(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpDownloadResponse:
    """Begin one verified pull-based remote download."""

    result = await dispatch_application(owner, request_id, "manual_sftp.download.begin", _params(payload, SftpDownloadBeginRequest, request_id))
    set_correlation(response, request_id)
    return SftpDownloadResponse(request_id=request_id, download=model_from_result(result["download"], DownloadReady))


@router.get("/v1/sftp/downloads/{operation_id}/chunks/{sequence}", response_class=Response)
async def download_chunk(operation_id: UUID, sequence: HttpChunkSequence, request: Request, request_id: CorrelationId, owner: Owner, offset: Annotated[int, Query(ge=0, le=2**53 - 1)]) -> Response:
    """Return one validated raw download chunk with strict identity headers."""

    if await request.body():
        raise _problem(request_id, status_code=400, error_code="UNEXPECTED_REQUEST_BODY", title="Unexpected request body", message="Download chunk requests do not accept a body")
    resources = require_ready_resources(owner, request_id)

    async def work(context):
        return await resources.manual_sftp_application.download_chunk(context, operation_id, sequence=sequence, offset=offset)

    chunk = await _binary_application_call(owner, request_id, work)
    _validate_download_chunk(chunk, request_id=request_id, operation_id=operation_id, sequence=sequence, offset=offset)
    return Response(
        content=chunk.data,
        media_type="application/octet-stream",
        headers={
            "X-Request-ID": str(request_id),
            "X-Chunk-Sequence": str(chunk.sequence),
            "X-Chunk-Offset": str(chunk.offset),
            "X-Chunk-Byte-Count": str(len(chunk.data)),
            "X-Chunk-EOF": "true" if chunk.eof else "false",
        },
    )


@router.post("/v1/sftp/downloads/{operation_id}/finish", response_model=SftpTerminalResponse)
async def finish_download(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Verify the complete download and unchanged remote source."""

    return await _terminal_operation("manual_sftp.download.finish", operation_id, response, request_id, owner)


@router.post("/v1/sftp/downloads/{operation_id}/abort", response_model=SftpTerminalResponse)
async def abort_download(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Abort one download and report its durable terminal outcome."""

    return await _terminal_operation("manual_sftp.download.abort", operation_id, response, request_id, owner)


async def _mutation(operation: str, payload: dict[str, object], request_model, response: Response, request_id: UUID, owner: RuntimeOwner) -> SftpTerminalResponse:
    """Dispatch one snapshot-protected remote mutation."""

    result = await dispatch_application(owner, request_id, operation, _params(payload, request_model, request_id))
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.post("/v1/sftp/directories", response_model=SftpTerminalResponse, status_code=status.HTTP_201_CREATED)
async def mkdir(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Create one remote directory without embedding its path in the URL."""

    return await _mutation("manual_sftp.mkdir", payload, SftpMkdirRequest, response, request_id, owner)


@router.post("/v1/sftp/renames", response_model=SftpTerminalResponse)
async def rename(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Perform one snapshot-protected atomic remote rename."""

    return await _mutation("manual_sftp.rename", payload, SftpRenameRequest, response, request_id, owner)


@router.post("/v1/sftp/removals", response_model=SftpTerminalResponse)
async def remove(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Remove one unchanged remote entry."""

    return await _mutation("manual_sftp.remove", payload, SftpRemoveRequest, response, request_id, owner)


@router.post("/v1/sftp/deletions/preflight", response_model=SftpDeletePlanResponse)
async def delete_preflight(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpDeletePlanResponse:
    """Build one complete no-follow recursive-delete plan."""

    result = await dispatch_application(owner, request_id, "manual_sftp.delete.preflight", _params(payload, SftpDeletePreflightRequest, request_id))
    set_correlation(response, request_id)
    return SftpDeletePlanResponse(request_id=request_id, delete_plan=model_from_result(result["delete_plan"], DeletePlanSummary))


@router.post("/v1/sftp/deletions/{operation_id}/execute", response_model=SftpTerminalResponse)
async def delete_execute(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """Consume the one-shot delete plan identified by the endpoint owner key."""

    result = await dispatch_application(owner, request_id, "manual_sftp.delete.execute", {"delete_plan_id": str(operation_id)})
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.get("/v1/sftp/recoveries", response_model=SftpRecoveryListResponse)
async def list_recoveries(response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryListResponse:
    """List every remote-only recovery through the dispatcher owner."""

    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.list", {})
    set_correlation(response, request_id)
    return SftpRecoveryListResponse(
        request_id=request_id,
        recoveries=[model_from_result(value, RecoverySummary) for value in result["recoveries"]],
    )


@router.get("/v1/sftp/recoveries/{recovery_id}", response_model=SftpRecoveryResponse)
async def inspect_recovery(recovery_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryResponse:
    """Inspect one remote recovery record without mutating it."""

    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.inspect", {"recovery_id": str(recovery_id)})
    set_correlation(response, request_id)
    return SftpRecoveryResponse(request_id=request_id, recovery=model_from_result(result["recovery"], RecoverySummary))


@router.post("/v1/sftp/recoveries/{recovery_id}/actions", response_model=SftpRecoveryResponse)
async def execute_recovery(recovery_id: UUID, payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryResponse:
    """Execute one explicitly selected recovery action."""

    params = _params(payload, SftpRecoveryActionRequest, request_id)
    params["recovery_id"] = str(recovery_id)
    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.execute", params)
    set_correlation(response, request_id)
    return SftpRecoveryResponse(request_id=request_id, recovery=model_from_result(result["recovery"], RecoverySummary))


__all__ = ["read_exact_binary_body", "router"]
