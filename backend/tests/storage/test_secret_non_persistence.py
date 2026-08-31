from pathlib import Path
from uuid import uuid4

from langchain_core.messages import ToolMessage

from harness_shell_sidecar.agent.api_configs import ApiConfigRepository
from harness_shell_sidecar.agent.contracts import ApiType, ModelApiConfigInput
from harness_shell_sidecar.agent.conversations import ConversationRepository
from harness_shell_sidecar.storage import (
    EncryptedRecord,
    EncryptedRecordStore,
    RuntimeDatabase,
)


PLAINTEXT_MARKER = b"M1-PLAINTEXT-SECRET-6f047bd2"


def test_plaintext_never_reaches_sqlite_files(tmp_path: Path) -> None:
    path = (tmp_path / "runtime.sqlite3").resolve()
    database = RuntimeDatabase.open(path)
    store = EncryptedRecordStore(database, b"d" * 32)
    store.put(EncryptedRecord("secret-test", "marker", 1, PLAINTEXT_MARKER))
    database.close()

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )

    assert PLAINTEXT_MARKER not in persisted


def test_agent_output_plaintext_never_reaches_sqlite_or_wal(tmp_path: Path) -> None:
    marker = "AGENT-OUTPUT-PLAINTEXT-7d60"
    path = (tmp_path / "agent-runtime.sqlite3").resolve()
    database = RuntimeDatabase.open(path)
    store = EncryptedRecordStore(database, b"e" * 32)
    configs = ApiConfigRepository(database)
    conversations = ConversationRepository(database, store)
    config = configs.create(
        ModelApiConfigInput(
            display_name="test",
            api_type=ApiType.CHAT_COMPLETIONS,
            base_url="https://example.test/v1",
            model="test-model",
            api_key_secret_ref=uuid4(),
            enabled=True,
        )
    )
    conversation_id = conversations.create_conversation()
    run = conversations.start_run(conversation_id, uuid4(), config.api_config_id)
    conversations.append_message(
        run.agent_run_id,
        conversation_id,
        ToolMessage(content=marker, tool_call_id="call-marker"),
    )
    store.zeroize()
    database.close()

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    assert marker.encode() not in persisted
