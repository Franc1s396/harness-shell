from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from harness_shell_sidecar.agent.streaming import (
    AgentTurnCompletedEvent,
    AgentTurnFailedEvent,
    AgentTurnStartedEvent,
    AgentTurnStreamEvent,
    AgentTurnTextDeltaEvent,
)


EVENT_ADAPTER = TypeAdapter(AgentTurnStreamEvent)


def test_started_event_is_strict_and_correlated() -> None:
    """拒绝改变流开始事件的 schema 或标识。"""

    request_id = uuid4()
    conversation_id = uuid4()
    run_id = uuid4()

    event = AgentTurnStartedEvent(
        request_id=request_id,
        sequence=0,
        conversation_id=conversation_id,
        agent_run_id=run_id,
        status="RUNNING",
        react_iteration=0,
    )

    assert event.model_dump(mode="json") == {
        "schema_version": 1,
        "type": "agent.turn.started",
        "request_id": str(request_id),
        "sequence": 0,
        "conversation_id": str(conversation_id),
        "agent_run_id": str(run_id),
        "status": "RUNNING",
        "react_iteration": 0,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 2},
        {"sequence": -1},
        {"request_id": "bad"},
        {"unknown": True},
    ],
)
def test_stream_event_rejects_schema_drift(mutation: dict[str, object]) -> None:
    """事件到达传输层前拒绝格式错误的公共字段。"""

    value: dict[str, object] = {
        "schema_version": 1,
        "type": "agent.turn.text_delta",
        "request_id": str(uuid4()),
        "sequence": 1,
        "conversation_id": str(uuid4()),
        "agent_run_id": str(uuid4()),
        "delta": "hello",
    }
    value.update(mutation)

    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(value)


def test_text_delta_rejects_empty_text() -> None:
    """防止空事件在无可见输出时改变序号。"""

    with pytest.raises(ValidationError):
        AgentTurnTextDeltaEvent(
            request_id=uuid4(),
            sequence=1,
            conversation_id=uuid4(),
            agent_run_id=uuid4(),
            delta="",
        )


def test_completed_event_accepts_only_durable_success_shape() -> None:
    """最终文本不放入终止事件，且要求错误码为 null。"""

    event = AgentTurnCompletedEvent(
        request_id=uuid4(),
        sequence=2,
        conversation_id=uuid4(),
        agent_run_id=uuid4(),
        status="COMPLETED",
        react_iteration=128,
        error_code=None,
    )

    assert event.error_code is None
    assert "final_text" not in event.model_dump()

    with pytest.raises(ValidationError):
        AgentTurnCompletedEvent.model_validate(
            {**event.model_dump(), "react_iteration": 129}
        )


@pytest.mark.parametrize("status", ["RUNNING", "COMPLETED"])
def test_failed_event_rejects_non_failure_status(status: str) -> None:
    """防止失败帧与持久化 Run 状态矛盾。"""

    with pytest.raises(ValidationError):
        AgentTurnFailedEvent.model_validate(
            {
                "request_id": uuid4(),
                "sequence": 2,
                "conversation_id": uuid4(),
                "agent_run_id": uuid4(),
                "status": status,
                "react_iteration": 0,
                "error_code": "MODEL_REQUEST_FAILED",
                "message": "Model request failed",
            }
        )


@pytest.mark.parametrize("text", ["", "revised answer"])
def test_text_replace_roundtrip_and_strict_fields(text: str) -> None:
    from harness_shell_sidecar.agent.streaming import AgentTurnTextReplaceEvent
    event = AgentTurnTextReplaceEvent(request_id=uuid4(), sequence=2,
        conversation_id=uuid4(), agent_run_id=uuid4(), text=text)
    assert EVENT_ADAPTER.validate_json(event.model_dump_json()) == event
    for mutation in ({"text": None}, {"text": 1}, {"delta": "extra"}):
        with pytest.raises(ValidationError):
            EVENT_ADAPTER.validate_python({**event.model_dump(), **mutation})
