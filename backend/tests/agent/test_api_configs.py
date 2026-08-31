from __future__ import annotations

from uuid import uuid4

import pytest

from harness_shell_sidecar.agent.api_configs import ApiConfigRepositoryError
from harness_shell_sidecar.agent.contracts import ApiType

from .conftest import AgentStorage, valid_api_config_input


def test_api_config_repository_round_trips_crud(agent_storage: AgentStorage) -> None:
    created = agent_storage.api_configs.create(valid_api_config_input())

    assert agent_storage.api_configs.get(created.api_config_id) == created
    assert agent_storage.api_configs.list() == [created]

    replacement = valid_api_config_input().model_copy(
        update={
            "display_name": "responses provider",
            "api_type": ApiType.RESPONSES,
            "enabled": False,
        }
    )
    updated = agent_storage.api_configs.update(created.api_config_id, replacement)

    assert updated.api_config_id == created.api_config_id
    assert updated.created_at == created.created_at
    assert updated.display_name == "responses provider"
    assert updated.api_type is ApiType.RESPONSES
    assert updated.enabled is False
    assert agent_storage.api_configs.delete(created.api_config_id) is True
    assert agent_storage.api_configs.delete(created.api_config_id) is False


def test_api_config_repository_rejects_missing_update(
    agent_storage: AgentStorage,
) -> None:
    with pytest.raises(ApiConfigRepositoryError) as error:
        agent_storage.api_configs.update(uuid4(), valid_api_config_input())

    assert error.value.error_code == "MODEL_API_CONFIG_NOT_FOUND"


def test_api_config_repository_rejects_delete_while_run_references_config(
    agent_storage: AgentStorage,
) -> None:
    created = agent_storage.api_configs.create(valid_api_config_input())
    conversation_id = uuid4()
    agent_storage.database.execute(
        "INSERT INTO agent_conversations(conversation_id, created_at, updated_at) VALUES (?, ?, ?)",
        (str(conversation_id), "2026-08-30T00:00:00.000000Z", "2026-08-30T00:00:00.000000Z"),
    )
    agent_storage.database.execute(
        """
        INSERT INTO agent_runs(
            agent_run_id, conversation_id, ssh_session_id, api_config_id,
            status, react_iteration, error_code, started_at, ended_at
        ) VALUES (?, ?, ?, ?, 'RUNNING', 0, NULL, ?, NULL)
        """,
        (
            str(uuid4()),
            str(conversation_id),
            str(uuid4()),
            str(created.api_config_id),
            "2026-08-30T00:00:00.000000Z",
        ),
    )

    with pytest.raises(ApiConfigRepositoryError) as error:
        agent_storage.api_configs.delete(created.api_config_id)

    assert error.value.error_code == "MODEL_API_CONFIG_IN_USE"
