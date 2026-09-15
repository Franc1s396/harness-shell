"""真实 AgentService 的图片绑定、模型请求、重试与会话清理。"""
import asyncio
import base64
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from harness_shell_sidecar.agent.attachments import AttachmentRepository
from harness_shell_sidecar.agent.image_validation import validate_image
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.agent.contracts import ApiType
from .conftest import valid_api_config_input
from .fakes import FakeModelSequence, RecordingTurnSink, make_turn_input
from .test_graph import RecordingExecutor
from .test_image_validation import picture
from .test_service import _run_turn, _service
from ..storage_support import sql


@pytest.mark.parametrize('lost_started', [False, True])
@pytest.mark.parametrize('api_type', list(ApiType))
def test_image_retry_followup_and_delete(agent_storage, lost_started, api_type):
    """原图进入 SDK，稳定 ID 留在 canonical；重试与后续轮不丢图片。"""
    class LostStartedSink(RecordingTurnSink):
        """模拟首帧丢失，UI 尚不知道会话 ID。"""
        async def started(self, run):
            """让真实取消路径保留已绑定原图。"""
            raise asyncio.CancelledError()

    async def scenario():
        config = agent_storage.api_configs.create(valid_api_config_input().model_copy(update={'api_type': api_type}))
        raw, draft = picture('PNG'), uuid4()
        with agent_storage.database.write_session() as session:
            image = AttachmentRepository(session).create(draft, validate_image('photo.png', raw))
        model = FakeModelSequence([AIMessage(content='answer') for _ in range(3)])
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(update={
            'api_config_id': config.api_config_id, 'user_message_id': uuid4(),
            'user_message': '', 'draft_id': draft, 'attachment_ids': (image.attachment_id,),
        })
        if lost_started:
            with pytest.raises(asyncio.CancelledError):
                await _run_turn(agent_storage, service, turn, 'key', asyncio.Event(), LostStartedSink())
        else:
            await _run_turn(agent_storage, service, turn, 'key', asyncio.Event())
        result = await _run_turn(agent_storage, service, turn.model_copy(update={'retry': True}), 'key', asyncio.Event())
        assert result.final_text == 'answer'
        canonical = agent_storage.conversations.load_messages(result.conversation_id)
        assert canonical[0].content == [{'type': 'harness_image', 'attachment_id': str(image.attachment_id)}]
        wire = json.dumps(model.message_calls[-1])
        assert 'data:image/png;base64,' + base64.b64encode(raw).decode() in wire
        assert 'harness_image' not in wire
        with pytest.raises(AgentServiceError):
            await _run_turn(agent_storage, service, turn.model_copy(update={'retry': True, 'attachment_ids': ()}), 'key', asyncio.Event())
        await _run_turn(agent_storage, service, turn.model_copy(update={
            'conversation_id': result.conversation_id, 'user_message_id': uuid4(),
            'user_message': 'more detail', 'attachment_ids': (), 'draft_id': None,
        }), 'key', asyncio.Event())
        assert 'data:image/png;base64,' in json.dumps(model.message_calls[-1])
        await service.delete_conversation(result.conversation_id)
        for table in ('agent_attachments', 'agent_attachment_contents', 'agent_runs', 'agent_messages', 'agent_conversations'):
            assert sql(agent_storage.database, f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)
    asyncio.run(scenario())


def test_summary_sdk_sees_images_and_keeps_blob(agent_storage):
    """摘要使用真实 SDK mapper，覆盖后主模型投影不再重复注入旧图。"""
    from pydantic import SecretStr
    from harness_shell_sidecar.agent.context import ContextService
    from harness_shell_sidecar.agent.context_compaction import ContextCompactor
    from .test_context_compaction import ControlledBudget
    from .fakes import instant_sleep, RecordingSequenceClientBuilder
    from harness_shell_sidecar.agent.model_gateway import ModelGateway

    async def scenario():
        """使用真实 Run/消息/BLOB，只替代远程结果与触发阈值。"""
        config = agent_storage.api_configs.create(valid_api_config_input())
        draft = uuid4()
        with agent_storage.database.write_session() as session:
            image = AttachmentRepository(session).create(draft, validate_image('photo', picture('PNG')))
        model = FakeModelSequence([AIMessage(content='answer') for _ in range(5)])
        service = _service(agent_storage, model, RecordingExecutor())
        turn = make_turn_input().model_copy(update={'api_config_id': config.api_config_id,
            'user_message_id': uuid4(), 'draft_id': draft, 'attachment_ids': (image.attachment_id,)})
        first = await _run_turn(agent_storage, service, turn, 'key', asyncio.Event())
        for _ in range(2):
            result = await _run_turn(agent_storage, service, turn.model_copy(update={
                'conversation_id': first.conversation_id, 'user_message_id': uuid4(), 'draft_id': None, 'attachment_ids': (),
            }), 'key', asyncio.Event())
        gateway = ModelGateway(client_builder=RecordingSequenceClientBuilder(model), sleep=instant_sleep)
        compactor = ContextCompactor(agent_storage.database, ControlledBudget(), gateway, sleep=instant_sleep)
        records = agent_storage.conversations.load_context_messages(first.conversation_id)
        summaries = await compactor.compact(config=config, api_key=SecretStr('key'), records=records, summaries=(),
            conversation_id=first.conversation_id, source_run_id=result.agent_run_id, cancelled=asyncio.Event())
        assert summaries
        assert 'data:image/png;base64,' in json.dumps(model.message_calls[-1])
        assert 'harness_image' not in json.dumps(model.message_calls[-1])
        assert 'harness_image' not in str(ContextService.project(records, summaries))
        assert agent_storage.conversations.load_context_messages(first.conversation_id) == records
        with agent_storage.database.read_session() as session:
            assert AttachmentRepository(session).read_payload(image.attachment_id).data == picture('PNG')
    asyncio.run(scenario())


def test_delete_rejects_active_run_and_rolls_back_partial_cleanup(agent_storage, monkeypatch):
    """活动轮次不可删除；消息删除中途失败必须保留整个会话。"""
    from harness_shell_sidecar.storage import PlaintextRecordStore
    from harness_shell_sidecar.agent.contracts import AgentRunStatus
    from langchain_core.messages import HumanMessage
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    run = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(run.agent_run_id, conversation, [HumanMessage(content='question'), AIMessage(content='answer')])
    service = _service(agent_storage, FakeModelSequence([]), RecordingExecutor())
    with pytest.raises(AgentServiceError, match='active'):
        asyncio.run(service.delete_conversation(conversation))
    repo.finish_run(run.agent_run_id, AgentRunStatus.COMPLETED, None)
    before = repo.load_messages(conversation)
    original = PlaintextRecordStore.delete
    calls = 0
    def fail_second(store, record_type, record_id):
        """真实删除第一条后注入第二条故障，验证事务而非提前校验。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('injected cleanup failure')
        return original(store, record_type, record_id)
    monkeypatch.setattr(PlaintextRecordStore, 'delete', fail_second)
    with pytest.raises(RuntimeError, match='injected'):
        asyncio.run(service.delete_conversation(conversation))
    assert repo.load_messages(conversation) == before
    assert repo.conversation_exists(conversation)
