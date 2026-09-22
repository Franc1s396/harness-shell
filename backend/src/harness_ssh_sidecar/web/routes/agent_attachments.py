"""固定图片上传与读取协议；有界 multipart 只在内存解析。"""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from ..dependencies import dispatch_application, require_request_id, runtime_owner
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner
from ..models import DeleteResponse

router = APIRouter()
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


class AttachmentMetadata(BaseModel):
    """附件公开字段，不包含原图字节或存储路径。"""
    model_config = ConfigDict(extra='forbid')
    attachment_id: UUID = Field(description='Backend image identity')
    draft_id: UUID = Field(description='Original upload draft identity')
    filename: str = Field(max_length=255, description='Display name only')
    media_type: Literal['image/png', 'image/jpeg', 'image/webp', 'image/gif'] = Field(description='Decoded media type')
    byte_size: int = Field(ge=1, le=10_485_760, description='Original byte length')
    width: int = Field(gt=0, description='Pixel width')
    height: int = Field(gt=0, description='Pixel height')


class AttachmentResponse(BaseModel):
    """上传完成后返回稳定 ID 和校验后的元数据。"""
    model_config = ConfigDict(extra='forbid')
    request_id: UUID = Field(description='Request correlation')
    attachment: AttachmentMetadata = Field(description='Validated image metadata')


def _parse_upload(content_type: str, body: bytes) -> tuple[UUID, str, bytes]:
    """只接受两个 part；回调不创建临时文件，所有缓冲都有硬界限。"""
    media, options = parse_options_header(content_type)
    boundary = options.get(b'boundary')
    if media != b'multipart/form-data' or not boundary or len(boundary) > 70:
        raise ValueError('multipart boundary invalid')
    parts: dict[bytes, tuple[bytes | None, bytes]] = {}
    headers: dict[bytes, bytes] = {}
    field, value, data = bytearray(), bytearray(), bytearray()
    name: bytes | None = None
    filename: bytes | None = None
    ended = False

    def begin() -> None:
        """每个 part 独占有界 header 和 data 缓冲。"""
        headers.clear()
        data.clear()
        field.clear()
        value.clear()

    def header_field(raw: bytes, start: int, end: int) -> None:
        """累加 header 名称并立即限制。"""
        field.extend(raw[start:end])
        if len(field) > 128:
            raise ValueError('header name too large')

    def header_value(raw: bytes, start: int, end: int) -> None:
        """累加有限 header 值，不允许无限 metadata。"""
        value.extend(raw[start:end])
        if len(value) > 2048:
            raise ValueError('header value too large')

    def header_end() -> None:
        """拒绝重复或未知 part header。"""
        key = bytes(field).lower()
        if key in headers or key not in (b'content-disposition', b'content-type'):
            raise ValueError('unexpected header')
        headers[key] = bytes(value)
        field.clear()
        value.clear()

    def headers_done() -> None:
        """验证本 part 身份，未知/重复字段一律失败。"""
        nonlocal name, filename
        kind, attributes = parse_options_header(headers.get(b'content-disposition', b''))
        name, filename = attributes.get(b'name'), attributes.get(b'filename')
        if kind != b'form-data' or name not in (b'draft_id', b'file') or name in parts:
            raise ValueError('unexpected part')
        if (name == b'file') != (filename is not None):
            raise ValueError('invalid file part')

    def part_data(raw: bytes, start: int, end: int) -> None:
        """在复制前校验 part 的实际 byte budget。"""
        limit = 10_485_760 if name == b'file' else 36
        if len(data) + end - start > limit:
            raise ValueError('part too large')
        data.extend(raw[start:end])

    def part_end() -> None:
        """只记录完整 part，不接受半张图片。"""
        parts[name] = (filename, bytes(data))

    def end() -> None:
        """要求完整终止 boundary。"""
        nonlocal ended
        ended = True

    parser = MultipartParser(boundary, {'on_part_begin': begin, 'on_header_field': header_field,
        'on_header_value': header_value, 'on_header_end': header_end, 'on_headers_finished': headers_done,
        'on_part_data': part_data, 'on_part_end': part_end, 'on_end': end})
    parser.write(body)
    parser.finalize()
    if not ended or set(parts) != {b'draft_id', b'file'}:
        raise ValueError('incomplete multipart')
    return UUID(parts[b'draft_id'][1].decode('ascii')), parts[b'file'][0].decode('utf-8'), parts[b'file'][1]


@router.post('/v1/agent/attachments', response_model=AttachmentResponse, status_code=201)
async def upload_image(request: Request, response: Response, request_id: CorrelationId, owner: Owner) -> AttachmentResponse:
    """校验 multipart 后由 dispatcher 完成图片校验与原子存储。"""
    try:
        draft_id, filename, data = _parse_upload(request.headers.get('content-type', ''), await request.body())
    except (ValueError, UnicodeError) as error:
        raise HttpProblem(build_problem(request_id=request_id, status=422, error_code='INVALID_REQUEST_PAYLOAD',
            title='Invalid image upload', message='Image upload requires one draft ID and one bounded image file')) from error
    result = await dispatch_application(owner, request_id, 'agent.attachments.upload',
        {'draft_id': str(draft_id), 'filename': filename, 'data': data})
    response.headers['X-Request-ID'] = str(request_id)
    return AttachmentResponse(request_id=request_id, attachment=result['attachment'])


@router.get('/v1/agent/attachments/{attachment_id}/content')
async def read_image(attachment_id: UUID, request_id: CorrelationId, owner: Owner) -> Response:
    """返回原图；不经过 JSON response limit，不返回路径或文件名。"""
    result = await dispatch_application(owner, request_id, 'agent.attachments.read', {'attachment_id': str(attachment_id)})
    return Response(content=result['data'], media_type=result['media_type'], headers={
        'X-Request-ID': str(request_id), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.delete('/v1/agent/attachments/{attachment_id}', response_model=DeleteResponse)
async def delete_image(attachment_id: UUID, draft_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> DeleteResponse:
    """仅移除匹配草稿的未发送图片。"""
    result = await dispatch_application(owner, request_id, 'agent.attachments.delete',
        {'attachment_id': str(attachment_id), 'draft_id': str(draft_id)})
    response.headers['X-Request-ID'] = str(request_id)
    return DeleteResponse(request_id=request_id, deleted=result['deleted'])


@router.delete('/v1/agent/attachment-drafts/{draft_id}', response_model=DeleteResponse)
async def clear_draft(draft_id: UUID, response: Response, request_id: CorrelationId, owner: Owner) -> DeleteResponse:
    """关闭草稿时清理未绑定内容，已发送图片不受影响。"""
    result = await dispatch_application(owner, request_id, 'agent.attachments.clear', {'draft_id': str(draft_id)})
    response.headers['X-Request-ID'] = str(request_id)
    return DeleteResponse(request_id=request_id, deleted=result['deleted'])
