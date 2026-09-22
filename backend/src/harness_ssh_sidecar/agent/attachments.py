"""附件持久化、归属和回收；借用调用者短 Session。"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from harness_ssh_sidecar.storage.orm import AgentAttachmentRow, AgentAttachmentContentRow
from .image_models import AttachmentError, AttachmentInfo, ImagePayload, ValidatedImage, MAX_MESSAGE_IMAGES


class AttachmentRepository:
    """不独立提交或关闭 Session；所有写操作由外层同一事务保护。"""

    def __init__(self, session: Session) -> None:
        """借用当前操作的数据库上下文。"""
        self._session = session  # 不跨网络或 decoder await 保持。

    def create(self, draft_id: UUID, image: ValidatedImage) -> AttachmentInfo:
        """先写元数据再写 BLOB，任一失败由调用者整体回滚。"""
        identity = str(uuid4())
        row = AgentAttachmentRow(attachment_id=identity, draft_id=str(draft_id), filename=image.filename,
            media_type=image.media_type, byte_size=len(image.data), width=image.width, height=image.height,
            created_at=datetime.now(timezone.utc).isoformat())
        self._session.add(row)
        self._session.flush()
        self._session.add(AgentAttachmentContentRow(attachment_id=identity, data=image.data))
        self._session.flush()
        return self.get_info(UUID(identity))

    def _row(self, attachment_id: UUID) -> AgentAttachmentRow:
        """只加载元数据；未知 ID 不返回伪附件。"""
        row = self._session.get(AgentAttachmentRow, str(attachment_id))
        if row is None:
            raise AttachmentError("AGENT_ATTACHMENT_NOT_FOUND", "Image attachment does not exist")
        return row

    def get_info(self, attachment_id: UUID) -> AttachmentInfo:
        """返回脱离 Session 的公开元数据。"""
        row = self._row(attachment_id)
        return AttachmentInfo(attachment_id, UUID(row.draft_id), row.filename, row.media_type,
                              row.byte_size, row.width, row.height)

    def read_payload(self, attachment_id: UUID) -> ImagePayload:
        """校验元数据与 BLOB 一致性，不修补存储损坏。"""
        row = self._row(attachment_id)
        content = self._session.get(AgentAttachmentContentRow, str(attachment_id))
        if content is None or len(content.data) != row.byte_size:
            raise AttachmentError("AGENT_ATTACHMENT_CORRUPT", "Stored image content is missing or inconsistent")
        return ImagePayload(row.media_type, content.data)

    def require_conversation(self, attachment_id: UUID, conversation_id: UUID) -> None:
        """读取模型输入前校验绑定会话，禁止跨会话引用。"""
        if self._row(attachment_id).conversation_id != str(conversation_id):
            raise AttachmentError("AGENT_ATTACHMENT_CONFLICT", "Image belongs to a different conversation")

    def bind(self, draft_id: UUID, conversation_id: UUID, user_message_id: UUID,
             attachment_ids: tuple[UUID, ...]) -> None:
        """按请求顺序原子绑定，全部校验通过后才修改行。"""
        if len(attachment_ids) > MAX_MESSAGE_IMAGES or len(set(attachment_ids)) != len(attachment_ids):
            raise AttachmentError("AGENT_ATTACHMENT_CONFLICT", "Message must contain at most five distinct images")
        rows = [self._row(identity) for identity in attachment_ids]
        if any(row.draft_id != str(draft_id) or row.user_message_id is not None for row in rows):
            raise AttachmentError("AGENT_ATTACHMENT_CONFLICT", "Images must belong to the original unbound draft")
        for position, row in enumerate(rows):
            row.conversation_id, row.user_message_id, row.position = str(conversation_id), str(user_message_id), position
        self._session.flush()

    def bound_ids(self, user_message_id: UUID) -> tuple[UUID, ...]:
        """读取稳定用户消息的图片顺序，拒绝损坏或多会话绑定。"""
        rows = list(self._session.scalars(select(AgentAttachmentRow).where(
            AgentAttachmentRow.user_message_id == str(user_message_id)).order_by(AgentAttachmentRow.position)))
        if ([row.position for row in rows] != list(range(len(rows)))
                or len({row.conversation_id for row in rows}) > 1):
            raise AttachmentError("AGENT_ATTACHMENT_CORRUPT", "Stored image ordering is inconsistent")
        return tuple(UUID(row.attachment_id) for row in rows)

    def delete_unbound(self, draft_id: UUID, attachment_id: UUID) -> None:
        """只能移除原草稿的未发送图片，已绑定图片跟随会话。"""
        row = self._row(attachment_id)
        if row.draft_id != str(draft_id) or row.user_message_id is not None:
            raise AttachmentError("AGENT_ATTACHMENT_CONFLICT", "Only unbound images from this draft can be removed")
        self._session.delete(row)
        self._session.flush()

    def delete_draft(self, draft_id: UUID) -> None:
        """删除该草稿所有未绑定图片，保留已发送的引用。"""
        self._session.execute(delete(AgentAttachmentRow).where(
            AgentAttachmentRow.draft_id == str(draft_id), AgentAttachmentRow.user_message_id.is_(None)))

    def delete_all_unbound(self) -> None:
        """仅启动阶段回收上次 Runtime 遗留的临时图片。"""
        self._session.execute(delete(AgentAttachmentRow).where(AgentAttachmentRow.user_message_id.is_(None)))
