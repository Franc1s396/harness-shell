"""明文 LangChain 对话历史与 Agent Run 持久化。"""

from __future__ import annotations

from .context_models import ContextMessage

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

from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore, RuntimeDatabase

from .contracts import AgentRun, AgentRunStatus


class ConversationRepositoryError(RuntimeError):
    """暴露稳定持久化错误，不包含消息或输出明文。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定错误码与有界、非敏感诊断消息。"""

        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class ConversationRepository:
    """负责 Agent 元数据事务，借用明文记录存储。"""

    _database: RuntimeDatabase
    _record_store: PlaintextRecordStore

    def __init__(
        self,
        database: RuntimeDatabase,
        record_store: PlaintextRecordStore,
    ) -> None:
        """绑定运行时拥有的存储协作者，不接管其清理责任。"""

        self._database = database
        self._record_store = record_store

    def create_conversation(self) -> UUID:
        """创建空的持久化会话并返回不透明标识。"""

        conversation_id = uuid4()
        now = _utc_now()
        self._database.execute(
            "INSERT INTO agent_conversations(conversation_id, created_at, updated_at) VALUES (?, ?, ?)",
            (str(conversation_id), now, now),
        )
        return conversation_id

    def conversation_exists(self, conversation_id: UUID) -> bool:
        """返回指定不透明会话标识是否存在。"""

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
        """持久化绑定一个 SSH Session 和 API 配置的新运行中 Agent 执行。"""

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
        """原子持久化并追加一条 LangChain 消息，返回其序号。"""

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
        """在同一事务中追加消息与明文记录，失败则全部回滚。"""

        if not messages:
            return ()
        connection = self._database.connection
        # 1. 用立即事务串行分配会话序号，消息正文和索引必须共同提交。
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM agent_messages WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            next_sequence = int(row[0]) + 1
            sequences: list[int] = []
            now = _utc_now()
            # 2. 逐条写入明文正文及消息元数据，按本批顺序分配连续序号。
            for offset, message in enumerate(messages):
                sequence = next_sequence + offset
                message_id = uuid4()
                record_id = str(message_id)
                self._record_store.put(
                    PlaintextRecord(
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
                        record_id, tool_call_id, agent_run_id, created_at
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
            # 3. 更新会话时间并检查所属会话仍存在，再提交整批消息。
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
        # 4. 任一步失败或取消都回滚整批记录，不能留下半组工具消息。
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def load_messages(self, conversation_id: UUID) -> list[AnyMessage]:
        """还原一个会话完整且有序的历史。"""

        rows = self._database.execute(
            """
            SELECT message_type, record_id, tool_call_id
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
                    "Agent message record is missing",
                )
            message = _deserialize_message(record.payload)
            if _message_type(message) != message_type or (
                isinstance(message, ToolMessage)
                and message.tool_call_id != tool_call_id
            ):
                raise ConversationRepositoryError(
                    "AGENT_MESSAGE_METADATA_MISMATCH",
                    "Agent message metadata does not match its record",
                )
            messages.append(message)
        return messages

    @property
    def database(self) -> RuntimeDatabase:
        """向同级仓库提供借用的事务管理者。"""
        return self._database

    def load_context_messages(self, conversation_id: UUID) -> list[ContextMessage]:
        """读取带持久化序号和实际 Run 标识的权威历史。"""
        messages = self.load_messages(conversation_id)
        rows = self._database.execute(
            "SELECT sequence, agent_run_id FROM agent_messages WHERE conversation_id = ? ORDER BY sequence",
            (str(conversation_id),),
        ).fetchall()
        if len(rows) != len(messages):
            raise ConversationRepositoryError("AGENT_MESSAGE_METADATA_MISMATCH", "history changed while loading context")
        return [ContextMessage(row[0], UUID(row[1]), message)
                for row, message in zip(rows, messages, strict=True)]

    def increment_iteration(self, agent_run_id: UUID) -> AgentRun:
        """原子递增运行中 Run 的循环计数，不超过 128 次上限。"""

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
        """对当前运行中的 Agent Run 执行且仅执行一次终态转换。"""

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
        """返回当前严格 Run 快照，用于协调生命周期。"""

        return self._get_run(agent_run_id)

    def _get_run(self, agent_run_id: UUID) -> AgentRun | None:
        """从 schema v7 元数据加载严格 Run 模型。"""

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
    """序列化一条 LangChain 消息，不猜测 Provider 特定格式。"""

    payload = {"schema_version": 1, "message": message_to_dict(message)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _deserialize_message(payload: bytes) -> AnyMessage:
    """还原一条明文消息；不支持的记录明确失败。"""

    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported Agent message schema")
        return messages_from_dict([value["message"]])[0]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ConversationRepositoryError(
            "AGENT_MESSAGE_SCHEMA_UNSUPPORTED",
            "Agent message payload is unsupported",
        ) from exc


def _message_type(message: AnyMessage) -> str:
    """将支持的 LangChain 消息类映射为稳定元数据角色。"""

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
    """为 SQLite 元数据返回标准 UTC 时间戳。"""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    """解析 SQLite 中的标准 UTC 时间戳。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
