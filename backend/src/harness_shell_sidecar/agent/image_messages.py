"""用户消息的严格图片引用和短生命周期模型投影。"""
import base64
from collections.abc import Callable, Sequence
from uuid import UUID
from langchain_core.messages import AnyMessage, HumanMessage

from harness_shell_sidecar.storage import RuntimeDatabase
from .attachments import AttachmentRepository
from .image_models import AttachmentError


def build_user_message(text: str, attachment_ids: tuple[UUID, ...]) -> HumanMessage:
    """保留纯文本历史格式；新图片只存 ID，不存 Base64。"""
    if not attachment_ids:
        return HumanMessage(content=text)
    blocks = ([{'type': 'text', 'text': text}] if text else [])
    blocks.extend({'type': 'harness_image', 'attachment_id': str(identity)} for identity in attachment_ids)
    return HumanMessage(content=blocks)


def user_content_identity(message: HumanMessage) -> tuple[str, tuple[UUID, ...]]:
    """校验 canonical 内容并返回用于原消息重试的完整身份。"""
    if isinstance(message.content, str):
        return message.content, ()
    text, identities = '', []
    try:
        for index, block in enumerate(message.content):
            if not isinstance(block, dict):
                raise ValueError('invalid block')
            if block.get('type') == 'text' and index == 0 and set(block) == {'type', 'text'} and type(block['text']) is str:
                text = block['text']
            elif block.get('type') == 'harness_image' and set(block) == {'type', 'attachment_id'}:
                identities.append(UUID(block['attachment_id']))
            else:
                raise ValueError('invalid block')
        if not identities or len(identities) > 5 or len(set(identities)) != len(identities):
            raise ValueError('invalid image identity')
    except (ValueError, TypeError, AttributeError) as error:
        raise AttachmentError('AGENT_ATTACHMENT_CORRUPT', 'Stored user image references are invalid') from error
    return text, tuple(identities)


def project_image_blocks(message: HumanMessage, resolve: Callable[[UUID], str]) -> HumanMessage:
    """逐块映射引用；摘要可交替包含多个历史文本和图片。"""
    blocks = []
    for block in message.content:
        if not isinstance(block, dict):
            raise AttachmentError('AGENT_ATTACHMENT_CORRUPT', 'Image input contains an unsupported block')
        if block.get('type') == 'harness_image' and set(block) == {'type', 'attachment_id'}:
            blocks.append({'type': 'image_url', 'image_url': {'url': resolve(UUID(block['attachment_id']))}})
        elif block.get('type') == 'text' and set(block) == {'type', 'text'} and type(block['text']) is str:
            blocks.append(dict(block))
        else:
            raise AttachmentError('AGENT_ATTACHMENT_CORRUPT', 'Image input contains an unsupported block')
    return message.model_copy(update={'content': blocks})


def budget_image_messages(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """预算专用投影，不加载 BLOB，也不将内部 ID 注入 tokenizer。"""
    return [project_image_blocks(message, lambda identity: '')
            if isinstance(message, HumanMessage) and isinstance(message.content, list) else message
            for message in messages]


def resolve_image_messages(database: RuntimeDatabase, conversation_id: UUID,
                           messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """正式请求前在短读事务内验证归属并读取原图，离开事务才调用模型。"""
    if not any(isinstance(message, HumanMessage) and isinstance(message.content, list) for message in messages):
        return list(messages)
    with database.read_session() as session:
        repository = AttachmentRepository(session)
        def resolve(identity: UUID) -> str:
            """该请求局部生成 Provider 图片输入，禁止全局缓存。"""
            repository.require_conversation(identity, conversation_id)
            payload = repository.read_payload(identity)
            return 'data:' + payload.media_type + ';base64,' + base64.b64encode(payload.data).decode('ascii')
        return [project_image_blocks(message, resolve)
                if isinstance(message, HumanMessage) and isinstance(message.content, list) else message
                for message in messages]
