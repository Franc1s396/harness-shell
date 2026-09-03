from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from harness_shell_sidecar.agent.contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.agent.conversations import ConversationRepositoryError
from harness_shell_sidecar.storage import PlaintextRecord

from .conftest import AgentStorage, valid_api_config_input


def _started_run(agent_storage: AgentStorage) -> tuple[UUID, AgentRun]:
    """Create the minimum persisted configuration, conversation, and running Run."""

    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation_id = agent_storage.conversations.create_conversation()
    run = agent_storage.conversations.start_run(
        conversation_id,
        uuid4(),
        config.api_config_id,
    )
    return conversation_id, run


def test_tool_message_round_trips_through_plaintext_record(
    agent_storage: AgentStorage,
) -> None:
    marker = "agent-stdout-secret-marker-98af"
    conversation_id, run = _started_run(agent_storage)
    message = ToolMessage(
        content=json.dumps({"stdout": marker, "stderr": ""}),
        tool_call_id="call-1",
    )

    sequence = agent_storage.conversations.append_message(
        run.agent_run_id,
        conversation_id,
        message,
    )

    assert sequence == 1
    assert agent_storage.conversations.load_messages(conversation_id) == [message]
    rows = agent_storage.database.execute(
        "SELECT message_type, sequence, tool_call_id FROM agent_messages"
    ).fetchall()
    assert rows == [("TOOL", 1, "call-1")]
    assert agent_storage.record_store.get(
        "agent_message",
        agent_storage.database.execute("SELECT record_id FROM agent_messages").fetchone()[0],
    ).payload.find(marker.encode()) >= 0


def test_ai_tool_calls_and_responses_content_blocks_round_trip(
    agent_storage: AgentStorage,
) -> None:
    conversation_id, run = _started_run(agent_storage)
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute_command",
                    "args": {"command": "pwd"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content=[
                {
                    "type": "text",
                    "text": "done",
                    "annotations": [],
                }
            ]
        ),
    ]

    sequences = agent_storage.conversations.append_messages_atomic(
        run.agent_run_id,
        conversation_id,
        messages,
    )

    assert sequences == (1, 2)
    assert agent_storage.conversations.load_messages(conversation_id) == messages


def test_append_messages_rolls_back_metadata_and_records_on_write_failure(
    agent_storage: AgentStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, run = _started_run(agent_storage)
    real_put = agent_storage.record_store.put
    calls = 0

    def fail_second_put(record: PlaintextRecord) -> None:
        """Persist the first record, then simulate a second-record write failure."""

        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected record failure")
        real_put(record)

    monkeypatch.setattr(agent_storage.record_store, "put", fail_second_put)

    with pytest.raises(RuntimeError, match="injected record failure"):
        agent_storage.conversations.append_messages_atomic(
            run.agent_run_id,
            conversation_id,
            [HumanMessage(content="one"), HumanMessage(content="two")],
        )

    assert agent_storage.database.execute(
        "SELECT COUNT(*) FROM agent_messages"
    ).fetchone() == (0,)
    assert agent_storage.database.execute(
        "SELECT COUNT(*) FROM runtime_records WHERE record_type = 'agent_message'"
    ).fetchone() == (0,)


def test_load_messages_fails_closed_when_record_is_missing(
    agent_storage: AgentStorage,
) -> None:
    conversation_id, run = _started_run(agent_storage)
    agent_storage.conversations.append_message(
        run.agent_run_id,
        conversation_id,
        HumanMessage(content="inspect"),
    )
    record_id = agent_storage.database.execute(
        "SELECT record_id FROM agent_messages"
    ).fetchone()[0]
    assert agent_storage.record_store.delete("agent_message", record_id) is True

    with pytest.raises(ConversationRepositoryError) as error:
        agent_storage.conversations.load_messages(conversation_id)

    assert error.value.error_code == "AGENT_MESSAGE_RECORD_MISSING"


def test_run_iteration_and_terminal_state_are_single_transition(
    agent_storage: AgentStorage,
) -> None:
    _, run = _started_run(agent_storage)

    incremented = agent_storage.conversations.increment_iteration(run.agent_run_id)
    finished = agent_storage.conversations.finish_run(
        run.agent_run_id,
        AgentRunStatus.COMPLETED,
        None,
    )

    assert incremented.react_iteration == 1
    assert finished.status is AgentRunStatus.COMPLETED
    assert finished.ended_at is not None
    with pytest.raises(ConversationRepositoryError) as error:
        agent_storage.conversations.finish_run(
            run.agent_run_id,
            AgentRunStatus.FAILED,
            "LATE_FAILURE",
        )
    assert error.value.error_code == "AGENT_RUN_NOT_RUNNING"
