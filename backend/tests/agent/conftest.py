from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from ..storage_support import RepositoryClient

from harness_shell_sidecar.agent.api_configs import ApiConfigRepository
from harness_shell_sidecar.agent.contracts import ApiType, ModelApiConfigInput
from harness_shell_sidecar.agent.conversations import ConversationRepository
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


@dataclass(frozen=True, slots=True)
class AgentStorage:
    """为测试拥有独立 Agent 数据库及其明文记录存储。"""

    database: RuntimeDatabase
    record_store: RepositoryClient
    api_configs: RepositoryClient
    conversations: RepositoryClient


@pytest.fixture
def agent_storage(tmp_path: Path) -> AgentStorage:
    """创建并确定性关闭 Agent 存储 fixture。"""

    database = RuntimeDatabase.open((tmp_path / "agent.sqlite3").resolve())
    try:
        yield AgentStorage(database,
            RepositoryClient(database, PlaintextRecordStore),
            RepositoryClient(database, ApiConfigRepository),
            RepositoryClient(database, lambda session: ConversationRepository(session, PlaintextRecordStore(session))))
    finally:
        database.close()


def valid_api_config_input() -> ModelApiConfigInput:
    """构建已启用且带不透明密钥引用的 Chat Completions 配置。"""

    return ModelApiConfigInput(
        display_name="test provider",
        api_type=ApiType.CHAT_COMPLETIONS,
        base_url="http://127.0.0.1:8000/v1",
        model="test-model",
        api_key_credential_id=uuid4(),
        enabled=True,
    )
