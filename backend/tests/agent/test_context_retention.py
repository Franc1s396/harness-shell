"""消息比例保留必须守住工具原子性和累计摘要。"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from harness_shell_sidecar.agent.context import ContextService
from harness_shell_sidecar.agent.context_models import AgentContextPolicy, ContextError, ContextMessage, ContextSummary
from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
from harness_shell_sidecar.agent.contracts import AgentRunStatus
from ..storage_support import RepositoryClient, sql
from .conftest import AgentStorage, valid_api_config_input
from .fakes import make_tool_call


@pytest.mark.parametrize("count, expected", [(1, 0), (2, 1), (3, 1), (7, 4), (10, 6), (25, 15)])
def test_retention_rounds_up_recent_forty_percent(count: int, expected: int) -> None:
    records = [ContextMessage(i + 1, uuid4(), HumanMessage(content=str(i))) for i in range(count)]
    assert len(ContextService.compactable_prefix(records)) == expected


@pytest.mark.parametrize("percent", [0, 100, -1, True, 40.5])
def test_retention_rejects_invalid_percentage(percent: object) -> None:
    with pytest.raises(ValueError, match="retention percent"):
        AgentContextPolicy(context_retention_percent=percent)


def test_custom_retention_percentage_uses_complete_units() -> None:
    records = [ContextMessage(i + 1, uuid4(), HumanMessage(content=str(i))) for i in range(7)]
    assert [record.sequence for record in ContextService.compactable_prefix(records, 50)] == [1, 2, 3]


def test_tool_group_counts_once_and_can_split_a_historical_turn() -> None:
    messages = [SystemMessage(content="ignored"), HumanMessage(content="first"),
        AIMessage(content="", tool_calls=[make_tool_call("a", "pwd"), make_tool_call("b", "ls")]),
        ToolMessage(content="b", tool_call_id="b"), ToolMessage(content="a", tool_call_id="a"),
        AIMessage(content="finished"), HumanMessage(content="current")]
    records = [ContextMessage(i + 1, uuid4(), message) for i, message in enumerate(messages)]
    # 四个单位保留两个；System 不参与计数，AI 和两个结果一起进入摘要。
    prefix = ContextService.compactable_prefix(records)
    assert [record.sequence for record in prefix] == [2, 3, 4, 5]


@pytest.mark.parametrize("messages", [
    [ToolMessage(content="orphan", tool_call_id="a")],
    [AIMessage(content="", tool_calls=[make_tool_call("a", "pwd")]), HumanMessage(content="next")],
    [AIMessage(content="", tool_calls=[make_tool_call("a", "pwd")]), ToolMessage(content="wrong", tool_call_id="b")],
])
def test_invalid_tool_units_fail_explicitly(messages: list[AnyMessage]) -> None:
    records = [ContextMessage(i + 1, uuid4(), message) for i, message in enumerate(messages)]
    with pytest.raises(ContextError):
        ContextService.compactable_prefix(records)


def test_projection_preserves_every_summary_in_order() -> None:
    now = datetime.now(timezone.utc)
    conversation, run = uuid4(), uuid4()
    summaries = (ContextSummary(conversation, 7, 2, "first summary", run, now, now),
                 ContextSummary(conversation, 8, 4, "second summary", run, now, now))
    records = [ContextMessage(i + 1, run, HumanMessage(content=str(i))) for i in range(6)]
    projected = ContextService.project(records, summaries)
    assert len(projected) == 5
    assert "first summary" in projected[1].content
    assert "second summary" in projected[2].content
    assert [message.content for message in projected[3:]] == ["4", "5"]


def test_summary_append_keeps_old_text_and_allows_human_boundary(agent_storage: AgentStorage) -> None:
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    old = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(old.agent_run_id, conversation,
        [HumanMessage(content="first"), AIMessage(content="answer"), HumanMessage(content="second"), AIMessage(content="done")])
    repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="current"))
    summaries = RepositoryClient(agent_storage.database, ContextSummaryRepository)
    first = summaries.commit_candidate(conversation_id=conversation, expected_revision=0,
        covered_through_sequence=1, summary_text="first summary", source_run_id=current.agent_run_id)
    second = summaries.commit_candidate(conversation_id=conversation, expected_revision=1,
        covered_through_sequence=3, summary_text="second summary", source_run_id=current.agent_run_id)
    assert summaries.load(conversation) == (first, second)
    assert first.summary_text == "first summary"
    assert second.revision == 2
    with pytest.raises(ContextError, match="stale"):
        summaries.commit_candidate(conversation_id=conversation, expected_revision=1,
            covered_through_sequence=4, summary_text="stale", source_run_id=current.agent_run_id)
    assert summaries.load(conversation) == (first, second)


def test_legacy_first_revision_can_be_greater_than_one(agent_storage: AgentStorage) -> None:
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    old = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(old.agent_run_id, conversation,
        [HumanMessage(content="first"), AIMessage(content="answer")])
    repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="current"))
    summaries = RepositoryClient(agent_storage.database, ContextSummaryRepository)
    summaries.commit_candidate(conversation_id=conversation, expected_revision=0,
        covered_through_sequence=1, summary_text="legacy", source_run_id=current.agent_run_id)
    sql(agent_storage.database, "UPDATE agent_context_summaries SET revision=7")
    legacy = summaries.load(conversation)[0]
    assert legacy.revision == 7
    appended = summaries.commit_candidate(conversation_id=conversation, expected_revision=7,
        covered_through_sequence=2, summary_text="new", source_run_id=current.agent_run_id)
    assert appended.revision == 8
    assert summaries.load(conversation) == (legacy, appended)
    sql(agent_storage.database, "UPDATE agent_context_summaries SET revision=9 WHERE revision=8")
    with pytest.raises(ContextError, match="order"):
        summaries.load(conversation)


def test_retry_keeps_summaries_from_earlier_runs(agent_storage: AgentStorage) -> None:
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    summaries = RepositoryClient(agent_storage.database, ContextSummaryRepository)
    saved = []
    for turn in range(3):
        run = repo.start_run(conversation, uuid4(), config.api_config_id)
        repo.append_messages_atomic(run.agent_run_id, conversation,
            [HumanMessage(content=f"user-{turn}"), AIMessage(content=f"answer-{turn}")])
        if turn:
            saved.append(summaries.commit_candidate(conversation_id=conversation,
                expected_revision=turn - 1, covered_through_sequence=turn * 2,
                summary_text=f"summary-{turn}", source_run_id=run.agent_run_id))
        finished = repo.finish_run(run.agent_run_id, AgentRunStatus.COMPLETED, None)
    assert summaries.load(conversation) == tuple(saved)
    repo.remove_last_turn(finished, "user-2")
    assert summaries.load(conversation) == (saved[0],)
    assert [message.content for message in repo.load_messages(conversation)] == ["user-0", "answer-0", "user-1", "answer-1"]


@pytest.mark.parametrize("cut", [2, 3])
def test_summary_rejects_partial_multi_tool_group(agent_storage: AgentStorage, cut: int) -> None:
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    old = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_messages_atomic(old.agent_run_id, conversation, [HumanMessage(content="first"),
        AIMessage(content="", tool_calls=[make_tool_call("a", "pwd"), make_tool_call("b", "ls")]),
        ToolMessage(content="a", tool_call_id="a"), ToolMessage(content="b", tool_call_id="b")])
    repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="current"))
    summaries = RepositoryClient(agent_storage.database, ContextSummaryRepository)
    with pytest.raises(ContextError, match="incomplete tool"):
        summaries.commit_candidate(conversation_id=conversation, expected_revision=0,
            covered_through_sequence=cut, summary_text="invalid cut", source_run_id=current.agent_run_id)
    assert summaries.load(conversation) == ()
