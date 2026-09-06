"""用户手动 SFTP 的 typed JSON 与原始二进制 HTTP 路由。"""

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
    """选择显式连接的 SSH 会话。"""

    #: 拥有 SFTP 通道的活动 SSH 会话。
    ssh_session_id: UUID


class SftpPathRequest(SftpSessionRequest):
    """在严格 JSON 请求体中选择远程路径。"""

    #: 远程 POSIX 绝对路径，不编码到 URL 路径中。
    path: str


class SftpUploadBeginRequest(SftpPathRequest):
    """为上传冻结本地源和远程目标标识。"""

    #: React 选择的新操作标识。
    operation_id: UUID
    #: 完整本地源文件 SHA-256。
    source_sha256: Sha256Hex
    #: 完整本地源文件大小。
    source_byte_count: JsSafeInt
    #: 预检返回的远程目标快照。
    target_snapshot: TransferSnapshot


class SftpDownloadBeginRequest(SftpPathRequest):
    """使用新的持久化操作标识开始下载。"""

    #: React 选择的新操作标识。
    operation_id: UUID


class SftpMkdirRequest(SftpSessionRequest):
    """在此前已校验父目录下创建单个名称。"""

    #: React 选择的新操作标识。
    operation_id: UUID
    #: 父目录绝对路径。
    parent_path: str
    #: 单个子名称。
    name: str


class SftpRenameRequest(SftpSessionRequest):
    """将未改变的源重命名为已校验目标。"""

    #: React 选择的新操作标识。
    operation_id: UUID
    #: 源绝对路径。
    source_path: str
    #: 目标绝对路径。
    target_path: str
    #: 是否允许原子替换未改变的目标。
    overwrite: bool
    #: UI 流程冻结的可选源快照。
    source_snapshot: TransferSnapshot | None = None
    #: UI 流程冻结的可选目标快照。
    target_snapshot: TransferSnapshot | None = None


class SftpRemoveRequest(SftpPathRequest):
    """完整快照仍匹配时才删除条目。"""

    #: React 选择的新操作标识。
    operation_id: UUID
    #: 授权此次变更的快照。
    expected_snapshot: TransferSnapshot


class SftpDeletePreflightRequest(SftpPathRequest):
    """构建完整递归删除计划，不执行删除。"""

    #: React 选择的新操作标识。
    operation_id: UUID


class SftpRecoveryActionRequest(StrictHttpModel):
    """使用新标识执行允许列表内的恢复操作。"""

    #: React 选择的新恢复变更标识。
    operation_id: UUID
    #: 用户显式确认的恢复操作。
    action: Literal[
        "verify",
        "delete_temp",
        "continue_delete",
        "restore_tombstone",
        "keep",
    ]


class SftpContextResponse(StrictHttpModel):
    """返回安全手动 SFTP 上下文。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 安全远程上下文投影。
    context: ManualSftpContext


class SftpListingResponse(StrictHttpModel):
    """返回有界列表批次。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 有界列表分页。
    batch: ListingBatch


class SftpClosedResponse(StrictHttpModel):
    """确认显式列表游标关闭。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 已确定的关闭结果。
    closed: bool


class SftpEntryResponse(StrictHttpModel):
    """返回安全、不跟随链接的远程元数据投影。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 远程条目投影。
    entry: RemoteEntry


class SftpHashResponse(StrictHttpModel):
    """返回完整远程文件哈希。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 与已验证快照关联的哈希。
    hash: RemoteFileHash


class SftpSnapshotResponse(StrictHttpModel):
    """返回远程目标预检快照。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 完整预检快照。
    snapshot: TransferSnapshot


class SftpUploadResponse(StrictHttpModel):
    """返回就绪上传状态。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 就绪上传投影。
    upload: UploadReady


class SftpUploadChunkResponse(StrictHttpModel):
    """确认精确原始上传分块，不使用 Base64。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 传输操作标识。
    operation_id: UUID
    #: 已接受请求序号。
    sequence: ChunkSequence
    #: 已接受请求偏移量。
    offset: JsSafeInt
    #: 领域管理者已持久接收的原始字节数。
    accepted_bytes: Annotated[int, Field(ge=1, le=SFTP_CHUNK_BYTES, strict=True)]


class SftpDownloadResponse(StrictHttpModel):
    """返回就绪下载状态。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 就绪下载投影。
    download: DownloadReady


class SftpTerminalResponse(StrictHttpModel):
    """返回可信终态或需要恢复的结果。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 持久化操作终态投影。
    terminal: OperationTerminalProjection


class SftpDeletePlanResponse(StrictHttpModel):
    """返回完整递归删除预检摘要。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 完整一次性删除计划摘要。
    delete_plan: DeletePlanSummary


class SftpRecoveryResponse(StrictHttpModel):
    """返回安全恢复检查或操作结果。"""

    #: HTTP 关联标识。
    request_id: UUID
    #: 安全恢复摘要。
    recovery: RecoverySummary


class SftpRecoveryListResponse(StrictHttpModel):
    """按稳定创建顺序返回全部仅含远程状态的恢复摘要。"""

    request_id: UUID
    recoveries: list[RecoverySummary]


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


def _params(payload: dict[str, object], model, request_id: UUID) -> dict[str, object]:
    """校验 JSON 请求体并返回其 dispatcher 表示。"""

    return validate_json_model(payload, model, request_id).model_dump(mode="json")


def _problem(
    request_id: UUID,
    *,
    status_code: int,
    error_code: str,
    title: str,
    message: str,
) -> HttpProblem:
    """构建固定二进制契约问题，不包含请求派生文本。"""

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
    """返回一个原始请求头值，并拒绝重复传输字段。"""

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
    """仅读取一次带标准长度界限的二进制正文。"""

    # 1. 严格验证媒体类型，重复请求头不能影响解释结果。
    content_type = _single_header(request, "content-type")
    if content_type != required_content_type:
        raise _problem(
            request_id,
            status_code=415,
            error_code="SFTP_CONTENT_TYPE_INVALID",
            title="Invalid SFTP content type",
            message="SFTP chunks require application/octet-stream",
        )
    # 2. 要求标准 Content-Length 并预检最大值，读取前拒绝已知超限。
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
    # 3. 只读取一次正文，核对声明长度与最小长度，返回原始字节。
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
    """解析标准且有界的 X-Chunk-Offset 请求头。"""

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
    """确认接受字节前拒绝应用标识偏移。"""

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
    """暴露任何原始响应字节前拒绝非法下载状态。"""

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
    """在共享 dispatcher 所有权下执行原始字节应用操作。"""

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
    """在活动 SSH 会话上打开手动 SFTP 上下文。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.open", _params(payload, SftpSessionRequest, request_id))
    set_correlation(response, request_id)
    return SftpContextResponse(request_id=request_id, context=model_from_result(result["context"], ManualSftpContext))


@router.post("/v1/sftp/listings", response_model=SftpListingResponse, status_code=status.HTTP_201_CREATED)
async def begin_listing(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpListingResponse:
    """开始有界远程目录列表。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.begin", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpListingResponse(request_id=request_id, batch=model_from_result(result["batch"], ListingBatch))


@router.get("/v1/sftp/listings/{listing_id}/batches/{sequence}", response_model=SftpListingResponse)
async def next_listing(listing_id: UUID, sequence: HttpChunkSequence, response: Response, request_id: CorrelationId, owner: Owner) -> SftpListingResponse:
    """从列表游标读取精确下一序号。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.next", {"listing_id": str(listing_id), "sequence": sequence})
    set_correlation(response, request_id)
    return SftpListingResponse(request_id=request_id, batch=model_from_result(result["batch"], ListingBatch))


@router.delete("/v1/sftp/listings/{listing_id}", response_model=SftpClosedResponse)
async def close_listing(listing_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpClosedResponse:
    """显式关闭列表游标。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.list.close", {"listing_id": str(listing_id)})
    set_correlation(response, request_id)
    return SftpClosedResponse(request_id=request_id, closed=bool(result["closed"]))


async def _entry_operation(operation: str, payload: dict[str, object], response: Response, request_id: UUID, owner: RuntimeOwner) -> SftpEntryResponse:
    """派发 typed 远程元数据操作。"""

    result = await dispatch_application(owner, request_id, operation, _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpEntryResponse(request_id=request_id, entry=model_from_result(result["entry"], RemoteEntry))


@router.post("/v1/sftp/metadata/lstat", response_model=SftpEntryResponse)
async def lstat(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpEntryResponse:
    """返回远程路径的不跟随链接元数据。"""

    return await _entry_operation("manual_sftp.lstat", payload, response, request_id, owner)


@router.post("/v1/sftp/metadata/readlink", response_model=SftpEntryResponse)
async def readlink(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpEntryResponse:
    """返回显式解析的符号链接目标投影。"""

    return await _entry_operation("manual_sftp.readlink", payload, response, request_id, owner)


@router.post("/v1/sftp/hashes/sha256", response_model=SftpHashResponse)
async def sha256(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpHashResponse:
    """完整计算未改变的远程普通文件哈希。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.sha256", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpHashResponse(request_id=request_id, hash=model_from_result(result["hash"], RemoteFileHash))


@router.post("/v1/sftp/uploads/preflight", response_model=SftpSnapshotResponse)
async def upload_preflight(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpSnapshotResponse:
    """变更前冻结当前远程上传目标。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.upload.preflight", _params(payload, SftpPathRequest, request_id))
    set_correlation(response, request_id)
    return SftpSnapshotResponse(request_id=request_id, snapshot=model_from_result(result["snapshot"], TransferSnapshot))


@router.post("/v1/sftp/uploads", response_model=SftpUploadResponse, status_code=status.HTTP_201_CREATED)
async def begin_upload(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpUploadResponse:
    """开始向独占临时文件执行不覆盖上传。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.upload.begin", _params(payload, SftpUploadBeginRequest, request_id))
    set_correlation(response, request_id)
    return SftpUploadResponse(request_id=request_id, upload=model_from_result(result["upload"], UploadReady))


@router.put("/v1/sftp/uploads/{operation_id}/chunks/{sequence}", response_model=SftpUploadChunkResponse)
async def upload_chunk(operation_id: UUID, sequence: HttpChunkSequence, request: Request, response: Response, request_id: CorrelationId, owner: Owner) -> SftpUploadChunkResponse:
    """在 dispatcher 所有权下接收标准原始上传分块。"""

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
    """完成或中止传输并返回持久化结果。"""

    result = await dispatch_application(owner, request_id, operation, {"operation_id": str(operation_id)})
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.post("/v1/sftp/uploads/{operation_id}/finish", response_model=SftpTerminalResponse)
async def finish_upload(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """验证并原子提交完整上传。"""

    return await _terminal_operation("manual_sftp.upload.finish", operation_id, response, request_id, owner)


@router.post("/v1/sftp/uploads/{operation_id}/abort", response_model=SftpTerminalResponse)
async def abort_upload(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """中止上传并报告所需清理。"""

    return await _terminal_operation("manual_sftp.upload.abort", operation_id, response, request_id, owner)


@router.post("/v1/sftp/downloads", response_model=SftpDownloadResponse, status_code=status.HTTP_201_CREATED)
async def begin_download(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpDownloadResponse:
    """开始经过验证的拉取式远程下载。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.download.begin", _params(payload, SftpDownloadBeginRequest, request_id))
    set_correlation(response, request_id)
    return SftpDownloadResponse(request_id=request_id, download=model_from_result(result["download"], DownloadReady))


@router.get("/v1/sftp/downloads/{operation_id}/chunks/{sequence}", response_class=Response)
async def download_chunk(operation_id: UUID, sequence: HttpChunkSequence, request: Request, request_id: CorrelationId, owner: Owner, offset: Annotated[int, Query(ge=0, le=2**53 - 1)]) -> Response:
    """返回已校验原始下载分块，携带严格标识响应头。"""

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
    """验证完整下载及远程源未发生改变。"""

    return await _terminal_operation("manual_sftp.download.finish", operation_id, response, request_id, owner)


@router.post("/v1/sftp/downloads/{operation_id}/abort", response_model=SftpTerminalResponse)
async def abort_download(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """中止下载并报告持久化终态结果。"""

    return await _terminal_operation("manual_sftp.download.abort", operation_id, response, request_id, owner)


async def _mutation(operation: str, payload: dict[str, object], request_model, response: Response, request_id: UUID, owner: RuntimeOwner) -> SftpTerminalResponse:
    """派发受快照保护的远程变更。"""

    result = await dispatch_application(owner, request_id, operation, _params(payload, request_model, request_id))
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.post("/v1/sftp/directories", response_model=SftpTerminalResponse, status_code=status.HTTP_201_CREATED)
async def mkdir(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """创建远程目录，不将路径嵌入 URL。"""

    return await _mutation("manual_sftp.mkdir", payload, SftpMkdirRequest, response, request_id, owner)


@router.post("/v1/sftp/renames", response_model=SftpTerminalResponse)
async def rename(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """执行受快照保护的原子远程重命名。"""

    return await _mutation("manual_sftp.rename", payload, SftpRenameRequest, response, request_id, owner)


@router.post("/v1/sftp/removals", response_model=SftpTerminalResponse)
async def remove(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """删除未改变的远程条目。"""

    return await _mutation("manual_sftp.remove", payload, SftpRemoveRequest, response, request_id, owner)


@router.post("/v1/sftp/deletions/preflight", response_model=SftpDeletePlanResponse)
async def delete_preflight(payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpDeletePlanResponse:
    """构建完整、不跟随链接的递归删除计划。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.delete.preflight", _params(payload, SftpDeletePreflightRequest, request_id))
    set_correlation(response, request_id)
    return SftpDeletePlanResponse(request_id=request_id, delete_plan=model_from_result(result["delete_plan"], DeletePlanSummary))


@router.post("/v1/sftp/deletions/{operation_id}/execute", response_model=SftpTerminalResponse)
async def delete_execute(operation_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpTerminalResponse:
    """消费端点所属键标识的一次性删除计划。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.delete.execute", {"delete_plan_id": str(operation_id)})
    set_correlation(response, request_id)
    return SftpTerminalResponse(request_id=request_id, terminal=model_from_result(result["terminal"], OperationTerminalProjection))


@router.get("/v1/sftp/recoveries", response_model=SftpRecoveryListResponse)
async def list_recoveries(response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryListResponse:
    """通过 dispatcher 管理者列出全部远程恢复记录。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.list", {})
    set_correlation(response, request_id)
    return SftpRecoveryListResponse(
        request_id=request_id,
        recoveries=[model_from_result(value, RecoverySummary) for value in result["recoveries"]],
    )


@router.get("/v1/sftp/recoveries/{recovery_id}", response_model=SftpRecoveryResponse)
async def inspect_recovery(recovery_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryResponse:
    """检查远程恢复记录，不进行变更。"""

    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.inspect", {"recovery_id": str(recovery_id)})
    set_correlation(response, request_id)
    return SftpRecoveryResponse(request_id=request_id, recovery=model_from_result(result["recovery"], RecoverySummary))


@router.post("/v1/sftp/recoveries/{recovery_id}/actions", response_model=SftpRecoveryResponse)
async def execute_recovery(recovery_id: UUID, payload: dict[str, object], response: Response, request_id: CorrelationId, owner: Owner) -> SftpRecoveryResponse:
    """执行显式选择的恢复操作。"""

    params = _params(payload, SftpRecoveryActionRequest, request_id)
    params["recovery_id"] = str(recovery_id)
    result = await dispatch_application(owner, request_id, "manual_sftp.recovery.execute", params)
    set_correlation(response, request_id)
    return SftpRecoveryResponse(request_id=request_id, recovery=model_from_result(result["recovery"], RecoverySummary))


__all__ = ["read_exact_binary_body", "router"]
