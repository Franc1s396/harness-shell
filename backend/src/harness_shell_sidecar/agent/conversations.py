"""Encrypted LangChain conversation history and Agent run persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID, uuid4

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from harness_shell_sidecar.storage import (
    EncryptedRecord,
    EncryptedRecordStore,
    RuntimeDatabase,
)

from .contracts import AgentRun, AgentRunStatus


class ConversationRepositoryError(RuntimeError):
    """Expose a stable persistence error without message or output plaintext."""

    def __init__(self, error_code: str, message: str) -> None:
        """Store a stable code and bounded non-sensitive diagnostic message."""

        super().__init__(message)
        self.error_code = error_code


class ConversationRepository:
    """Own Agent metadata transactions while borrowing the encrypted record store."""

    _database: RuntimeDatabase
    _record_store: EncryptedRecordStore

    def __init__(
        self,
        database: RuntimeDatabase,
        record_store: EncryptedRecordStore,
    ) -> None:
        """Bind runtime-owned storage collaborators without taking their cleanup."""

        self._database = database
        self._record_store = record_store

    def create_conversation(self) -> UUID:
        """Create an empty durable conversation and return its opaque identity."""

        conversation_id = uuid4()
        now = _utc_now()
        self._database.execute(
            "INSERT INTO agent_conversations(conversation_id, created_at, updated_at) VALUES (?, ?, ?)",
            (str(conversation_id), now, now),
        )
        return conversation_id

    def conversation_exists(self, conversation_id: UUID) -> bool:
        """Return whether one opaque conversation identity exists."""

        row = self._database.execute(
            "SELECT 1 FROM agent_conversations WHERE conversation_id = ?",
            (str(conversation_id),),
        ).fetchone()
        return row is not None

    def start_run(
        self,
        conversation_id: UUID,
        ssh_session_id: UUID,
        api_config_id: UUID,
    ) -> AgentRun:
        """Persist a new running Agent execution bound to one SSH Session and API config."""

        agent_run_id = uuid4()
        started_at = _utc_now()
        try:
            self._database.execute(
                """
                INSERT INTO agent_runs(
                    agent_run_id, conversation_id, ssh_session_id, api_config_id,
                    status, react_iteration, error_code, started_at, ended_at
                ) VALUES (?, ?, ?, ?, 'RUNNING', 0, NULL, ?, NULL)
                """,
                (
                    str(agent_run_id),
                    str(conversation_id),
                    str(ssh_session_id),
                    str(api_config_id),
                    started_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConversationRepositoryError(
                "AGENT_RUN_REFERENCE_INVALID",
                "Agent run references an unknown conversation or API configuration",
            ) from exc
        run = self._get_run(agent_run_id)
        if run is None:
            raise ConversationRepositoryError(
                "AGENT_RUN_PERSISTENCE_FAILED",
                "created Agent run was not found",
            )
        return run

    def append_message(
        self,
        agent_run_id: UUID,
        conversation_id: UUID,
        message: AnyMessage,
    ) -> int:
        """Atomically encrypt and append one LangChain message, returning its sequence."""

        return self.append_messages_atomic(
            agent_run_id,
            conversation_id,
            [message],
        )[0]

    def append_messages_atomic(
        self,
        agent_run_id: UUID,
        conversation_id: UUID,
        messages: Sequence[AnyMessage],
    ) -> tuple[int, ...]:
        """Append messages and ciphertext in one immediate transaction or roll back all."""

        if not messages:
            return ()
        connection = self._database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM agent_messages WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            next_sequence = int(row[0]) + 1
            sequences: list[int] = []
            now = _utc_now()
            for offset, message in enumerate(messages):
                sequence = next_sequence + offset
                message_id = uuid4()
                record_id = str(message_id)
                self._record_store.put(
                    EncryptedRecord(
                        record_type="agent_message",
                        record_id=record_id,
                        schema_version=1,
                        payload=_serialize_message(message),
                    )
                )
                connection.execute(
                    """
                    INSERT INTO agent_messages(
                        message_id, conversation_id, sequence, message_type,
                        encrypted_record_id, tool_call_id, agent_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(message_id),
                        str(conversation_id),
                        sequence,
                        _message_type(message),
                        record_id,
                        message.tool_call_id if isinstance(message, ToolMessage) else None,
                        str(agent_run_id),
                        now,
                    ),
                )
                sequences.append(sequence)
            updated = connection.execute(
                "UPDATE agent_conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, str(conversation_id)),
            )
            if updated.rowcount != 1:
                raise ConversationRepositoryError(
                    "AGENT_CONVERSATION_NOT_FOUND",
                    "conversation was not found while appending messages",
                )
            connection.execute("COMMIT")
            return tuple(sequences)
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def load_messages(self, conversation_id: UUID) -> list[AnyMessage]:
        """Authenticate and restore the complete ordered history for one conversation."""

        rows = self._database.execute(
            """
            SELECT message_type, encrypted_record_id, tool_call_id
            FROM agent_messages
            WHERE conversation_id = ?
            ORDER BY sequence
            """,
            (str(conversation_id),),
        ).fetchall()
        messages: list[AnyMessage] = []
        for message_type, record_id, tool_call_id in rows:
            record = self._record_store.get("agent_message", record_id)
            if record is None:
                raise ConversationRepositoryError(
                    "AGENT_MESSAGE_RECORD_MISSING",
                    "encrypted Agent message record is missing",
                )
            message = _deserialize_message(record.payload)
            if _message_type(message) != message_type or (
                isinstance(message, ToolMessage)
                and message.tool_call_id != tool_call_id
            ):
                raise ConversationRepositoryError(
                    "AGENT_MESSAGE_METADATA_MISMATCH",
                    "Agent message metadata does not match authenticated content",
                )
            messages.append(message)
        return messages

    def increment_iteration(self, agent_run_id: UUID) -> AgentRun:
        """Atomically increment one running Run without crossing the 128-loop limit."""

        cursor = self._database.execute(
            """
            UPDATE agent_runs SET react_iteration = react_iteration + 1
            WHERE agent_run_id = ? AND status = 'RUNNING' AND react_iteration < 128
            """,
            (str(agent_run_id),),
        )
        if cursor.rowcount != 1:
            current = self._get_run(agent_run_id)
            if current is not None and current.react_iteration >= 128:
                raise ConversationRepositoryError(
                    "REACT_LIMIT_REACHED",
                    "Agent run reached the ReAct iteration limit",
                )
            raise ConversationRepositoryError(
                "AGENT_RUN_NOT_RUNNING",
                "Agent run is absent or no longer running",
            )
        updated = self._get_run(agent_run_id)
        if updated is None:
            raise ConversationRepositoryError(
                "AGENT_RUN_PERSISTENCE_FAILED",
                "updated Agent run was not found",
            )
        return updated

    def finish_run(
        self,
        agent_run_id: UUID,
        status: AgentRunStatus,
        error_code: str | None,
    ) -> AgentRun:
        """Apply exactly one terminal transition to a currently running Agent run."""

        if status is AgentRunStatus.RUNNING:
            raise ValueError("finish_run requires a terminal status")
        cursor = self._database.execute(
            """
            UPDATE agent_runs SET status = ?, error_code = ?, ended_at = ?
            WHERE agent_run_id = ? AND status = 'RUNNING'
            """,
            (status.value, error_code, _utc_now(), str(agent_run_id)),
        )
        if cursor.rowcount != 1:
            raise ConversationRepositoryError(
                "AGENT_RUN_NOT_RUNNING",
                "Agent run is absent or already terminal",
            )
        finished = self._get_run(agent_run_id)
        if finished is None:
            raise ConversationRepositoryError(
                "AGENT_RUN_PERSISTENCE_FAILED",
                "finished Agent run was not found",
            )
        return finished

    def get_run(self, agent_run_id: UUID) -> AgentRun | None:
        """Return the current strict Run snapshot for lifecycle coordination."""

        return self._get_run(agent_run_id)

    def _get_run(self, agent_run_id: UUID) -> AgentRun | None:
        """Load one strict run model from trusted schema-v4 metadata."""

        row = self._database.execute(
            """
            SELECT agent_run_id, conversation_id, ssh_session_id, api_config_id,
                   status, react_iteration, error_code, started_at, ended_at
            FROM agent_runs WHERE agent_run_id = ?
            """,
            (str(agent_run_id),),
        ).fetchone()
        if row is None:
            return None
        return AgentRun(
            agent_run_id=UUID(row[0]),
            conversation_id=UUID(row[1]),
            ssh_session_id=UUID(row[2]),
            api_config_id=UUID(row[3]),
            status=AgentRunStatus(row[4]),
            react_iteration=row[5],
            error_code=row[6],
            started_at=_parse_time(row[7]),
            ended_at=None if row[8] is None else _parse_time(row[8]),
        )


def _serialize_message(message: AnyMessage) -> bytes:
    """Serialize one LangChain message without provider-specific guessing."""

    payload = {"schema_version": 1, "message": message_to_dict(message)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _deserialize_message(payload: bytes) -> AnyMessage:
    """Restore one authenticated message or fail on an unsupported record."""

    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported Agent message schema")
        return messages_from_dict([value["message"]])[0]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ConversationRepositoryError(
            "AGENT_MESSAGE_SCHEMA_UNSUPPORTED",
            "encrypted Agent message payload is unsupported",
        ) from exc


def _message_type(message: AnyMessage) -> str:
    """Map supported LangChain message classes to stable metadata roles."""

    if isinstance(message, SystemMessage):
        return "SYSTEM"
    if isinstance(message, HumanMessage):
        return "HUMAN"
    if isinstance(message, AIMessage):
        return "AI"
    if isinstance(message, ToolMessage):
        return "TOOL"
    raise ConversationRepositoryError(
        "AGENT_MESSAGE_TYPE_UNSUPPORTED",
        "LangChain message type is not supported",
    )


def _utc_now() -> str:
    """Return a canonical UTC timestamp for SQLite metadata."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """Parse a canonical UTC SQLite timestamp."""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
