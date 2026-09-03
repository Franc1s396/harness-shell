from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from harness_shell_sidecar.agent.contracts import (
    AgentRunStatus,
    AgentTurnInput,
    ApiType,
    ModelApiConfigInput,
)


def test_api_config_rejects_unknown_api_type() -> None:
    with pytest.raises(ValidationError):
        ModelApiConfigInput(
            display_name="local",
            api_type="AUTO",
            base_url="http://127.0.0.1:8000/v1",
            model="test-model",
            api_key_credential_id=uuid4(),
            enabled=True,
        )


def test_api_config_is_strict_and_normalizes_http_url() -> None:
    value = ModelApiConfigInput(
        display_name="  local  ",
        api_type=ApiType.RESPONSES,
        base_url="https://example.test/v1",
        model="  gpt-test  ",
        api_key_credential_id=uuid4(),
        enabled=True,
    )

    assert value.display_name == "local"
    assert value.base_url == "https://example.test/v1"
    assert value.model == "gpt-test"

    with pytest.raises(ValidationError):
        ModelApiConfigInput.model_validate(
            {**value.model_dump(), "enabled": 1, "unknown": "forbidden"}
        )


def test_api_config_rejects_non_http_base_url() -> None:
    with pytest.raises(ValidationError):
        ModelApiConfigInput(
            display_name="local",
            api_type=ApiType.CHAT_COMPLETIONS,
            base_url="file:///tmp/provider",
            model="test-model",
            api_key_credential_id=uuid4(),
            enabled=True,
        )


def test_agent_turn_input_and_status_are_strict() -> None:
    turn = AgentTurnInput(
        conversation_id=None,
        ssh_session_id=uuid4(),
        api_config_id=uuid4(),
        user_message="inspect the host",
    )

    assert turn.conversation_id is None
    assert AgentRunStatus.LIMIT_REACHED.value == "LIMIT_REACHED"
    with pytest.raises(ValidationError):
        AgentTurnInput.model_validate(
            {**turn.model_dump(), "user_message": "", "extra": True}
        )
