"""只持久化已通过预算校验的摘要，不改变权威消息。"""
from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecordStore
from .context_models import ContextSummary, ContextError
from .conversations import ConversationRepository


class ContextSummaryRepository:
    """借用运行时数据库，使用短事务原子替换摘要。"""
    def __init__(self, database: RuntimeDatabase) -> None:
        """绑定仓库，不额外打开数据库连接。"""
        self._database = database  # 运行时拥有的连接；此处不负责关闭。
        self._history = ConversationRepository(database, PlaintextRecordStore(database))  # 用于校验的只读视图。

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
        row = self._database.execute(
            "SELECT revision, covered_through_sequence, summary_text, source_run_id, created_at, updated_at "
            "FROM agent_context_summaries WHERE conversation_id = ?", (str(conversation_id),)).fetchone()
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
        # 1. 模型请求和预算检查已在事务外完成；此处只开启一个短写事务。
        connection = self._database.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            # 2. 重新读取当前版本，拒绝过期写入、空摘要或没有向前推进的覆盖边界。
            previous = self.load(conversation_id)
            revision = previous.revision if previous else 0
            if (revision != expected_revision or not summary_text.strip()
                    or (previous and covered_through_sequence <= previous.covered_through_sequence)):
                raise ContextError("CONTEXT_SUMMARY_INVALID", "summary replacement is stale or invalid")
            # 3. 校验候选边界，并保留首次创建时间；每次成功替换递增 revision。
            self._validate_boundary(conversation_id, covered_through_sequence, source_run_id)
            now = datetime.now(timezone.utc)
            created = previous.created_at if previous else now
            # 4. 只新增或替换独立摘要记录，不删除或改写原始对话消息。
            connection.execute(
                "INSERT INTO agent_context_summaries VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET revision=excluded.revision, "
                "covered_through_sequence=excluded.covered_through_sequence, summary_text=excluded.summary_text, "
                "source_run_id=excluded.source_run_id, updated_at=excluded.updated_at",
                (str(conversation_id), revision + 1, covered_through_sequence, summary_text,
                 str(source_run_id), created.isoformat(), now.isoformat()))
            # 5. 提交成功后才返回新摘要；任一步异常均回滚并向外传播。
            connection.execute("COMMIT")
            return ContextSummary(conversation_id, revision + 1, covered_through_sequence,
                                  summary_text, source_run_id, created, now)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
