"""只追加已通过预算校验的历史摘要，不改变权威消息或既有摘要。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from harness_shell_sidecar.storage import PlaintextRecordStore
from harness_shell_sidecar.storage.orm import AgentContextSummaryRow
from .context import ContextService
from .context_models import ContextError, ContextMessage, ContextSummary
from .conversations import ConversationRepository


class ContextSummaryRepository:
    """借用短事务 Session，校验有序摘要并以最后 revision 拒绝陈旧追加。"""

    def __init__(self, session: Session) -> None:
        """绑定仓库，不额外打开数据库连接。"""
        self._session = session  # 当前操作拥有的 Session；本仓库不提交。
        self._history = ConversationRepository(session, PlaintextRecordStore(session))  # 权威历史只读视图。

    def _validate_boundary(self, conversation_id: UUID, sequence: int, source_run_id: UUID,
                           records: Sequence[ContextMessage]) -> None:
        """允许历史轮内切分，但拒绝当前 Human 和不完整工具单位。"""
        # 1. 来源必须属于同一会话，且覆盖序号真实存在。
        indexes = [i for i, record in enumerate(records) if record.sequence == sequence]
        run = self._history.get_run(source_run_id)
        if not indexes or run is None or run.conversation_id != conversation_id:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary references invalid history")
        # 2. 修复 ToolMessage 可能属于当前 Run，故仍以当前 Human 为保护边界。
        source_users = [record.sequence for record in records
                        if record.agent_run_id == source_run_id and isinstance(record.message, HumanMessage)]
        if len(source_users) != 1 or sequence >= source_users[0]:
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary coverage reaches its source turn")
        index = indexes[0]
        if isinstance(records[index].message, SystemMessage):
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary boundary is not a historical message unit")
        # 3. 只验证覆盖前缀；之后可能存在合法的当前轮未完成工具调用。
        ContextService.message_units(records[:index + 1])

    def load(self, conversation_id: UUID) -> tuple[ContextSummary, ...]:
        """按 revision 加载全部摘要；旧库首条版本可大于 1，后续必须连续且覆盖递增。"""
        rows = self._session.scalars(select(AgentContextSummaryRow).where(
            AgentContextSummaryRow.conversation_id == str(conversation_id)
        ).order_by(AgentContextSummaryRow.revision)).all()
        if not rows:
            return ()
        records = self._history.load_context_messages(conversation_id)
        summaries: list[ContextSummary] = []
        for row in rows:
            try:
                if row.revision <= 0 or not row.summary_text.strip():
                    raise ValueError("invalid summary")
                summary = ContextSummary(conversation_id, row.revision, row.covered_through_sequence,
                    row.summary_text, UUID(row.source_run_id), datetime.fromisoformat(row.created_at),
                    datetime.fromisoformat(row.updated_at))
            except (ValueError, TypeError) as error:
                raise ContextError("CONTEXT_SUMMARY_INVALID", "stored summary metadata is invalid") from error
            if summaries and (summary.revision != summaries[-1].revision + 1
                              or summary.covered_through_sequence <= summaries[-1].covered_through_sequence):
                raise ContextError("CONTEXT_SUMMARY_INVALID", "stored summary order is invalid")
            self._validate_boundary(conversation_id, summary.covered_through_sequence, summary.source_run_id, records)
            summaries.append(summary)
        return tuple(summaries)

    def commit_candidate(self, *, conversation_id: UUID, expected_revision: int,
                         covered_through_sequence: int, summary_text: str,
                         source_run_id: UUID) -> ContextSummary:
        """在调用方写事务内校验最新版本后追加一条，绝不覆盖旧摘要。"""
        # RuntimeDatabase 写事务在读取前持有 SQLite 写锁，CAS 和 INSERT 属于同一事务。
        summaries = self.load(conversation_id)
        previous = summaries[-1] if summaries else None
        revision = previous.revision if previous else 0
        if (revision != expected_revision or not summary_text.strip()
                or (previous and covered_through_sequence <= previous.covered_through_sequence)):
            raise ContextError("CONTEXT_SUMMARY_INVALID", "summary append is stale or invalid")
        self._validate_boundary(conversation_id, covered_through_sequence, source_run_id,
                                self._history.load_context_messages(conversation_id))
        now = datetime.now(timezone.utc)
        self._session.add(AgentContextSummaryRow(conversation_id=str(conversation_id),
            revision=revision + 1, covered_through_sequence=covered_through_sequence,
            summary_text=summary_text, source_run_id=str(source_run_id),
            created_at=now.isoformat(), updated_at=now.isoformat()))
        self._session.flush()
        return ContextSummary(conversation_id, revision + 1, covered_through_sequence,
                              summary_text, source_run_id, now, now)
