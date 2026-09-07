"""只持久化已通过预算校验的摘要，不改变权威消息。"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from harness_shell_sidecar.storage import PlaintextRecordStore
from .context_models import ContextSummary, ContextError
from .conversations import ConversationRepository
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from harness_shell_sidecar.storage.orm import AgentContextSummaryRow


class ContextSummaryRepository:
    """借用运行时数据库，使用短事务原子替换摘要。"""
    def __init__(self, session: Session) -> None:
        """绑定仓库，不额外打开数据库连接。"""
        self._session = session  # 当前操作拥有的 Session；本仓库不提交。
        self._history = ConversationRepository(session, PlaintextRecordStore(session))  # 用于校验的只读视图。

    def _validate_boundary(self, conversation_id: UUID, sequence: int, source_run_id: UUID) -> None:
        """拒绝跨会话、覆盖当前轮或工具未配对的摘要边界。"""
        # 1. 确认覆盖序号真实存在，且生成摘要的 Run 属于同一个会话。
        records = self._history.load_context_messages(conversation_id)
        indexes = [i for i, record in enumerate(records) if record.sequence == sequence]
        run = self._history.get_run(source_run_id)
        if not indexes or run is None or run.conversation_id != conversation_id:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary references invalid history")
        # 修复用 ToolMessage 可能属于本轮，但位于本轮 HumanMessage 之前。
        # 因此以 HumanMessage 为边界，而非首条带 source_run_id 的记录。
        # 2. 以生成摘要那一轮的 HumanMessage 为界，禁止将该轮自身纳入摘要。
        source_users = [record.sequence for record in records
                        if record.agent_run_id == source_run_id and isinstance(record.message, HumanMessage)]
        if len(source_users) != 1 or sequence >= source_users[0]:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary coverage reaches its source turn")
        # 3. 覆盖末尾必须紧邻下一轮 HumanMessage，不能截断一个历史轮次。
        index = indexes[0]
        if index + 1 >= len(records) or not isinstance(records[index + 1].message, HumanMessage):
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary does not end at a complete historical turn")
        # 4. 检查覆盖范围内的工具调用与结果完整配对，拒绝悬空结果或未完成调用。
        pending: set[str] = set()
        for record in records[:index + 1]:
            message = record.message
            if isinstance(message, AIMessage):
                pending.update(call["id"] for call in message.tool_calls)
            elif isinstance(message, ToolMessage):
                if message.tool_call_id not in pending:
                    raise ContextError("CONTEXT_SUMMARY_INVALID", "summary history contains an unpaired tool result")
                pending.remove(message.tool_call_id)
        if pending:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary boundary splits a tool call from its result")

    def load(self, conversation_id: UUID) -> ContextSummary | None:
        """加载并校验唯一当前摘要，区分缺失与损坏。"""
        # 1. 读取会话当前摘要；只有确实不存在记录时才返回 None。
        row = self._session.execute(select(AgentContextSummaryRow.revision,
            AgentContextSummaryRow.covered_through_sequence, AgentContextSummaryRow.summary_text,
            AgentContextSummaryRow.source_run_id, AgentContextSummaryRow.created_at,
            AgentContextSummaryRow.updated_at).where(
            AgentContextSummaryRow.conversation_id == str(conversation_id))).first()
        if row is None:
            return None
        # 2. 解析并校验持久化字段；损坏记录必须报错，不能当作“没有摘要”。
        try:
            if row[0] <= 0 or not row[2].strip():
                raise ValueError("invalid summary")
            result = ContextSummary(conversation_id, row[0], row[1], row[2], UUID(row[3]),
                datetime.fromisoformat(row[4]), datetime.fromisoformat(row[5]))
        except (ValueError, TypeError) as error:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "stored summary metadata is invalid") from error
        # 3. 对照实际对话历史验证覆盖边界，再将可信的摘要记录交给调用方。
        self._validate_boundary(conversation_id, result.covered_through_sequence, result.source_run_id)
        return result

    def commit_candidate(self, *, conversation_id: UUID, expected_revision: int,
                         covered_through_sequence: int, summary_text: str,
                         source_run_id: UUID) -> ContextSummary:
        """模型调用和估算结束后，在一个短事务内比较并替换摘要。"""
        # 1. 调用者已开启短写事务；再次校验 revision 和历史边界。
        previous = self.load(conversation_id)
        revision = previous.revision if previous else 0
        if (revision != expected_revision or not summary_text.strip()
                or (previous and covered_through_sequence <= previous.covered_through_sequence)):
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary replacement is stale or invalid")
        self._validate_boundary(conversation_id, covered_through_sequence, source_run_id)
        now = datetime.now(timezone.utc)
        created = previous.created_at if previous else now
        statement = insert(AgentContextSummaryRow).values(conversation_id=str(conversation_id),
            revision=revision + 1, covered_through_sequence=covered_through_sequence,
            summary_text=summary_text, source_run_id=str(source_run_id),
            created_at=created.isoformat(), updated_at=now.isoformat())
        # 2. 同一事务只替换摘要，历史消息保持不变；应用层负责最终提交。
        self._session.execute(statement.on_conflict_do_update(
            index_elements=[AgentContextSummaryRow.conversation_id],
            set_={name: getattr(statement.excluded, name) for name in (
                "revision", "covered_through_sequence", "summary_text", "source_run_id", "updated_at")}))
        return ContextSummary(conversation_id, revision + 1, covered_through_sequence,
                              summary_text, source_run_id, created, now)
