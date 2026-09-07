"""明文 LangChain 对话历史与 Agent Run 持久化。"""

from __future__ import annotations

from .context_models import ContextMessage

import json
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
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

from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore

from .contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.storage.orm import AgentConversationRow, AgentRunRow, AgentMessageRow


class ConversationRepositoryError(RuntimeError):
    """暴露稳定持久化错误，不包含消息或输出明文。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定错误码与有界、非敏感诊断消息。"""

        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class ConversationRepository:
    """负责 Agent 元数据事务，借用明文记录存储。"""


    def __init__(self, session: Session, record_store: PlaintextRecordStore) -> None:
        """借用同一操作的 Session 和正文仓库，不独立提交。"""
        self._session = session  # 当前短事务的数据库上下文。
        self._record_store = record_store  # 与元数据共用同一 Session 的明文存储。

    def create_conversation(self) -> UUID:
        """创建空会话，供同一事务创建首个 Run。"""
        identity = uuid4()
        now = _utc_now()
        self._session.add(AgentConversationRow(conversation_id=str(identity), created_at=now, updated_at=now))
        self._session.flush()
        return identity

    def conversation_exists(self, conversation_id: UUID) -> bool:
        """查询会话是否仍存在。"""
        return self._session.scalar(select(AgentConversationRow.conversation_id).where(
            AgentConversationRow.conversation_id == str(conversation_id))) is not None

    def start_run(self, conversation_id: UUID, ssh_session_id: UUID, api_config_id: UUID) -> AgentRun:
        """持久化 RUNNING，引用无效时立即失败。"""
        identity = uuid4()
        try:
            self._session.add(AgentRunRow(agent_run_id=str(identity), conversation_id=str(conversation_id),
                ssh_session_id=str(ssh_session_id), api_config_id=str(api_config_id), status="RUNNING",
                react_iteration=0, error_code=None, started_at=_utc_now(), ended_at=None))
            self._session.flush()
        except IntegrityError as error:
            raise ConversationRepositoryError("AGENT_RUN_REFERENCE_INVALID", "Agent run references an unknown conversation or API configuration") from error
        result = self._get_run(identity)
        if result is None:
            raise ConversationRepositoryError("AGENT_RUN_PERSISTENCE_FAILED", "created Agent run was not found")
        return result

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

    def append_messages_atomic(self, agent_run_id: UUID, conversation_id: UUID,
                               messages: Sequence[AnyMessage]) -> tuple[int, ...]:
        """在调用者立即事务中分配序号、写正文与索引；不提交。"""
        if not messages:
            return ()
        # 1. 调用者持有写锁，整个序号分配与正文批次共同提交或回滚。
        last = self._session.scalar(select(func.coalesce(func.max(AgentMessageRow.sequence), 0)).where(
            AgentMessageRow.conversation_id == str(conversation_id)))
        sequences = []
        now = _utc_now()
        for offset, message in enumerate(messages):
            sequence = int(last) + 1 + offset
            identity = str(uuid4())
            self._record_store.put(PlaintextRecord("agent_message", identity, 1, _serialize_message(message)))
            self._session.add(AgentMessageRow(message_id=identity, conversation_id=str(conversation_id),
                sequence=sequence, message_type=_message_type(message), record_id=identity,
                tool_call_id=message.tool_call_id if isinstance(message, ToolMessage) else None,
                agent_run_id=str(agent_run_id), created_at=now))
            sequences.append(sequence)
        # 2. flush 使本批次约束错误在返回前暴露，应用层负责整体回滚。
        self._session.flush()
        result = self._session.execute(update(AgentConversationRow).where(
            AgentConversationRow.conversation_id == str(conversation_id)).values(updated_at=now))
        if result.rowcount != 1:
            raise ConversationRepositoryError("AGENT_CONVERSATION_NOT_FOUND", "conversation was not found while appending messages")
        return tuple(sequences)

    def load_messages(self, conversation_id: UUID) -> list[AnyMessage]:
        """还原一个会话完整且有序的历史。"""

        rows = self._session.execute(select(AgentMessageRow.message_type, AgentMessageRow.record_id,
            AgentMessageRow.tool_call_id).where(AgentMessageRow.conversation_id == str(conversation_id))
            .order_by(AgentMessageRow.sequence)).all()
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


    def load_context_messages(self, conversation_id: UUID) -> list[ContextMessage]:
        """在同一读取快照中关联消息与元数据。"""
        messages = self.load_messages(conversation_id)
        rows = self._session.execute(select(AgentMessageRow.sequence, AgentMessageRow.agent_run_id).where(
            AgentMessageRow.conversation_id == str(conversation_id)).order_by(AgentMessageRow.sequence)).all()
        if len(rows) != len(messages):
            raise ConversationRepositoryError("AGENT_MESSAGE_METADATA_MISMATCH", "history changed while loading context")
        return [ContextMessage(row[0], UUID(row[1]), message) for row, message in zip(rows, messages, strict=True)]

    def increment_iteration(self, agent_run_id: UUID) -> AgentRun:
        """原子递增运行中 Run 的循环计数，不超过 128 次上限。"""

        cursor = self._session.execute(update(AgentRunRow).where(
            AgentRunRow.agent_run_id == str(agent_run_id), AgentRunRow.status == "RUNNING",
            AgentRunRow.react_iteration < 128).values(react_iteration=AgentRunRow.react_iteration + 1))
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
        cursor = self._session.execute(update(AgentRunRow).where(
            AgentRunRow.agent_run_id == str(agent_run_id), AgentRunRow.status == "RUNNING")
            .values(status=status.value, error_code=error_code, ended_at=_utc_now()))
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
        """从 ORM 元数据加载严格 Run 模型。"""

        row = self._session.execute(select(AgentRunRow.agent_run_id, AgentRunRow.conversation_id,
            AgentRunRow.ssh_session_id, AgentRunRow.api_config_id, AgentRunRow.status,
            AgentRunRow.react_iteration, AgentRunRow.error_code, AgentRunRow.started_at, AgentRunRow.ended_at)
            .where(AgentRunRow.agent_run_id == str(agent_run_id))).first()
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
