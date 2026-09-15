"""附件短事务、归属、绑定和级联删除测试。"""
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, func

from harness_shell_sidecar.agent.attachments import AttachmentRepository
from harness_shell_sidecar.agent.image_models import AttachmentError
from harness_shell_sidecar.agent.image_validation import validate_image
from harness_shell_sidecar.storage.orm import AgentAttachmentRow, AgentAttachmentContentRow, AgentConversationRow
from .test_image_validation import picture


def test_upload_read_delete_is_atomic(agent_storage):
    database = agent_storage.database
    draft = uuid4()
    image = validate_image("photo", picture("PNG"))
    with database.write_session() as session:
        info = AttachmentRepository(session).create(draft, image)
    with database.read_session() as session:
        assert AttachmentRepository(session).read_payload(info.attachment_id).data == image.data
    with database.write_session() as session:
        AttachmentRepository(session).delete_unbound(draft, info.attachment_id)
    with database.read_session() as session:
        assert session.get(AgentAttachmentContentRow, str(info.attachment_id)) is None


def test_binding_conflict_rolls_back_and_conversation_delete_cascades(agent_storage):
    database = agent_storage.database
    conversation = agent_storage.conversations.create_conversation()
    draft, user = uuid4(), uuid4()
    image = validate_image("photo", picture("PNG"))
    with database.write_session() as session:
        repository = AttachmentRepository(session)
        first = repository.create(draft, image)
        second = repository.create(uuid4(), image)
    with pytest.raises(AttachmentError):
        with database.write_session() as session:
            AttachmentRepository(session).bind(draft, conversation, user, (first.attachment_id, second.attachment_id))
    with database.write_session() as session:
        repository = AttachmentRepository(session)
        repository.bind(draft, conversation, user, (first.attachment_id,))
        assert repository.bound_ids(user) == (first.attachment_id,)
    with pytest.raises(AttachmentError):
        with database.write_session() as session:
            AttachmentRepository(session).delete_unbound(draft, first.attachment_id)
    with database.write_session() as session:
        session.execute(delete(AgentConversationRow).where(AgentConversationRow.conversation_id == str(conversation)))
    with database.read_session() as session:
        assert session.get(AgentAttachmentContentRow, str(first.attachment_id)) is None
        assert session.get(AgentAttachmentContentRow, str(second.attachment_id)) is not None


def test_missing_blob_is_not_an_empty_image(agent_storage):
    database = agent_storage.database
    with database.write_session() as session:
        info = AttachmentRepository(session).create(uuid4(), validate_image("photo", picture("PNG")))
        session.execute(delete(AgentAttachmentContentRow))
    with database.read_session() as session, pytest.raises(AttachmentError) as error:
        AttachmentRepository(session).read_payload(info.attachment_id)
    assert error.value.error_code == "AGENT_ATTACHMENT_CORRUPT"


def test_purge_keeps_bound_images(agent_storage):
    database = agent_storage.database
    conversation = agent_storage.conversations.create_conversation()
    draft = uuid4()
    with database.write_session() as session:
        repository = AttachmentRepository(session)
        first = repository.create(draft, validate_image("photo", picture("PNG")))
        repository.create(draft, validate_image("other", picture("PNG")))
        repository.bind(draft, conversation, uuid4(), (first.attachment_id,))
        repository.delete_all_unbound()
    with database.read_session() as session:
        assert session.scalar(select(func.count()).select_from(AgentAttachmentRow)) == 1
        assert session.scalar(select(func.count()).select_from(AgentAttachmentContentRow)) == 1
