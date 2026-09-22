"""附件操作通过现有 dispatcher 使用唯一 Runtime 数据库。"""
from collections.abc import Mapping
from dataclasses import asdict
from uuid import UUID

from harness_ssh_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher, Handler
from harness_ssh_sidecar.runtime.request_context import RequestContext
from harness_ssh_sidecar.storage import RuntimeDatabase
from .attachments import AttachmentRepository
from .image_models import AttachmentError
from .image_validation import validate_image


def register_attachment_handlers(dispatcher: RequestDispatcher, database: RuntimeDatabase) -> None:
    """注册固定操作；同步 decoder/事务期间无 await，不留下半次 mutation。"""
    async def handle(context: RequestContext, params: Mapping[str, object]) -> dict[str, object]:
        """内部已路由操作仍验证参数；字节不进入 JSON 或异常日志。"""
        try:
            operation = params['operation']
            if operation == 'upload':
                if set(params) != {'operation', 'draft_id', 'filename', 'data'} or type(params['data']) is not bytes or type(params['filename']) is not str:
                    raise ValueError('invalid upload')
                draft_id = UUID(str(params['draft_id']))
                image = validate_image(params['filename'], params['data'])
                with database.write_session() as session:
                    info = AttachmentRepository(session).create(draft_id, image)
                result = asdict(info)
                result['attachment_id'], result['draft_id'] = str(info.attachment_id), str(info.draft_id)
                return {'attachment': result}
            if operation == 'read':
                if set(params) != {'operation', 'attachment_id'}:
                    raise ValueError('invalid read')
                with database.read_session() as session:
                    payload = AttachmentRepository(session).read_payload(UUID(str(params['attachment_id'])))
                return {'media_type': payload.media_type, 'data': payload.data}
            if operation == 'delete':
                if set(params) != {'operation', 'attachment_id', 'draft_id'}:
                    raise ValueError('invalid delete')
                with database.write_session() as session:
                    AttachmentRepository(session).delete_unbound(UUID(str(params['draft_id'])), UUID(str(params['attachment_id'])))
                return {'deleted': True}
            if operation == 'clear':
                if set(params) != {'operation', 'draft_id'}:
                    raise ValueError('invalid clear')
                with database.write_session() as session:
                    AttachmentRepository(session).delete_draft(UUID(str(params['draft_id'])))
                return {'deleted': True}
            raise ValueError('invalid operation')
        except AttachmentError as error:
            raise DispatchError(error.error_code, error.safe_message) from error
        except (ValueError, KeyError, TypeError) as error:
            raise DispatchError('INVALID_REQUEST_PAYLOAD', 'Attachment request is invalid') from error

    for operation in ('upload', 'read', 'delete', 'clear'):
        def bind(selected: str) -> Handler:
            """固定 dispatcher method 到操作，拒绝外部替换操作字段。"""
            async def invoke(context: RequestContext, params: Mapping[str, object]) -> dict[str, object]:
                """向共用实现传递已固定的动作身份。"""
                if 'operation' in params:
                    raise DispatchError('INVALID_REQUEST_PAYLOAD', 'Unexpected attachment request field')
                return await handle(context, {**params, 'operation': selected})
            return invoke
        dispatcher.register('agent.attachments.' + operation, bind(operation))
