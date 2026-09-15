"""摘要写入保留权威消息，并拒绝过期覆盖边界。"""

from ..storage_support import RepositoryClient, sql
from uuid import uuid4
import pytest
from langchain_core.messages import HumanMessage, AIMessage
from harness_shell_sidecar.agent.contracts import AgentRunStatus
from harness_shell_sidecar.agent.context_models import ContextError
from .conftest import AgentStorage, valid_api_config_input


def test_summary_commit_preserves_history_and_rejects_stale_writer(agent_storage: AgentStorage) -> None:
    from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    old = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(old.agent_run_id, conversation,
        [HumanMessage(content="inspect"), AIMessage(content="done")])
    repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="next"))
    before = repo.load_messages(conversation)
    summaries = RepositoryClient(agent_storage.database, ContextSummaryRepository)
    args = dict(conversation_id=conversation, expected_revision=0,
        covered_through_sequence=2, summary_text="Completed inspection.", source_run_id=current.agent_run_id)
    saved = summaries.commit_candidate(**args)
    assert saved.revision == 1
    assert summaries.load(conversation) == (saved,)
    assert repo.load_messages(conversation) == before
    with pytest.raises(ContextError):
        summaries.commit_candidate(**args)
    assert summaries.load(conversation) == (saved,)


def test_summary_cannot_cover_current_user(agent_storage: AgentStorage) -> None:
    from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    run = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(run.agent_run_id, conversation, HumanMessage(content="current"))
    with pytest.raises(ContextError):
        RepositoryClient(agent_storage.database, ContextSummaryRepository).commit_candidate(
            conversation_id=conversation, expected_revision=0, covered_through_sequence=1,
            summary_text="bad", source_run_id=run.agent_run_id)


def test_summary_source_cannot_cover_its_own_completed_turn(agent_storage: AgentStorage) -> None:
    from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    old = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(old.agent_run_id, conversation,
        [HumanMessage(content="first"), AIMessage(content="done")])
    repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="next"))
    with pytest.raises(ContextError, match="source turn"):
        RepositoryClient(agent_storage.database, ContextSummaryRepository).commit_candidate(
            conversation_id=conversation, expected_revision=0, covered_through_sequence=2,
            summary_text="invalid source", source_run_id=old.agent_run_id)
