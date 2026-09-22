"""canonical 引用、SDK 投影与图片预算隔离。"""
from uuid import uuid4
from langchain_core.messages import HumanMessage, AIMessage
from harness_ssh_sidecar.agent.image_messages import build_user_message, user_content_identity, resolve_image_messages
from harness_ssh_sidecar.agent.attachments import AttachmentRepository
from harness_ssh_sidecar.agent.image_validation import validate_image
from harness_ssh_sidecar.agent.model_gateway import _serialize_one_chat_message, _serialize_one_responses_message
from harness_ssh_sidecar.agent.context_budget import ContextBudget
from .test_image_validation import picture
from harness_ssh_sidecar.agent.contracts import AgentTurnInput
from harness_ssh_sidecar.agent.conversations import _serialize_message, _deserialize_message
from harness_ssh_sidecar.agent.context_compaction import build_summary_messages
from harness_ssh_sidecar.agent.context_models import ContextMessage


def test_image_only_turn_and_canonical_roundtrip():
    image = uuid4()
    request = AgentTurnInput(user_message='', user_message_id=uuid4(), draft_id=uuid4(),
        attachment_ids=(image,), ssh_session_id=uuid4(), api_config_id=uuid4())
    message = build_user_message(request.user_message, request.attachment_ids)
    assert user_content_identity(_deserialize_message(_serialize_message(message))) == ('', (image,))


def test_summary_contains_real_image_reference_instead_of_json_text():
    image = uuid4()
    source = build_summary_messages([ContextMessage(1, uuid4(), build_user_message('inspect', (image,)))])
    assert {'type': 'harness_image', 'attachment_id': str(image)} in source[1].content


def test_image_summary_keeps_old_ai_blocks_as_untrusted_history_text():
    """旧 AI Responses 内容不能被误认为用户图片协议块。"""
    from harness_ssh_sidecar.agent.image_messages import budget_image_messages
    source = build_summary_messages([
        ContextMessage(1, uuid4(), build_user_message('inspect', (uuid4(),))),
        ContextMessage(2, uuid4(), AIMessage(content=[{'type': 'text', 'text': 'old answer', 'annotations': []}])),
    ])
    projected = budget_image_messages(source)
    assert 'old answer' in projected[1].content[-1]['text']


def test_canonical_identity_and_formal_image_mapping(agent_storage):
    database = agent_storage.database
    conversation = agent_storage.conversations.create_conversation()
    draft, user = uuid4(), uuid4()
    with database.write_session() as session:
        repository = AttachmentRepository(session)
        info = repository.create(draft, validate_image('image', picture('PNG')))
        repository.bind(draft, conversation, user, (info.attachment_id,))
    canonical = build_user_message('', (info.attachment_id,))
    assert user_content_identity(canonical) == ('', (info.attachment_id,))
    assert user_content_identity(HumanMessage(content='legacy')) == ('legacy', ())
    projected = resolve_image_messages(database, conversation, [canonical])[0]
    block = _serialize_one_chat_message(projected)['content'][0]
    assert block['type'] == 'image_url'
    assert block['image_url']['url'].startswith('data:image/png;base64,')
    response = _serialize_one_responses_message(None, projected)[0]['content'][0]
    assert response['type'] == 'input_image'
    assert response['image_url'] == block['image_url']['url']
    assert canonical.content[0]['type'] == 'harness_image'


def test_budget_counts_images_without_tokenizing_bytes():
    class Encoding:
        """保留实际收到的文本，证明图片 bytes 不进入 tokenizer。"""
        def encode(self, value, **kwargs):
            assert 'SECRET_IMAGE_BYTES' not in value
            return list(value)
    budget = ContextBudget(Encoding(), None)
    plain = {'messages': [{'role': 'user', 'content': []}]}
    with_image = {'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'SECRET_IMAGE_BYTES'}}]}]}
    assert budget.estimate_payload(with_image) == budget.estimate_payload(plain) + 1000
