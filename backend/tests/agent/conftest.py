from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.agent.api_configs import ApiConfigRepository
from harness_shell_sidecar.agent.contracts import ApiType, ModelApiConfigInput
from harness_shell_sidecar.agent.conversations import ConversationRepository
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


@dataclass(frozen=True, slots=True)
class AgentStorage:
    """Own one isolated Agent database and its plaintext record store for a test."""

    database: RuntimeDatabase
    record_store: PlaintextRecordStore
    api_configs: ApiConfigRepository
    conversations: ConversationRepository


@pytest.fixture
def agent_storage(tmp_path: Path) -> AgentStorage:
    """Create and deterministically close one schema-v6 Agent storage fixture."""

    database = RuntimeDatabase.open_plaintext((tmp_path / "agent.sqlite3").resolve())
    record_store = PlaintextRecordStore(database)
    storage = AgentStorage(
        database=database,
        record_store=record_store,
        api_configs=ApiConfigRepository(database),
        conversations=ConversationRepository(database, record_store),
    )
    try:
        yield storage
    finally:
        database.close()


def valid_api_config_input() -> ModelApiConfigInput:
    """Build one enabled Chat Completions configuration with an opaque key reference."""

    return ModelApiConfigInput(
        display_name="test provider",
        api_type=ApiType.CHAT_COMPLETIONS,
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key_credential_id=uuid4(),
        enabled=True,
    )
