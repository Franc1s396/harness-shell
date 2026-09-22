"""将唯一滚动摘要无损迁移为可追加的摘要序列。"""

from alembic import op

revision = "0003_context_summary_history"
down_revision = "0002_agent_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """在启动迁移事务内重建复合主键，保留既有摘要全部字段。"""
    # 摘要表没有入向外键；新表复制成功后才替换，失败由迁移 owner 整批回滚。
    op.execute("""CREATE TABLE agent_context_summaries_next (
        conversation_id TEXT NOT NULL REFERENCES agent_conversations(conversation_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK (revision > 0),
        covered_through_sequence INTEGER NOT NULL CHECK (covered_through_sequence > 0),
        summary_text TEXT NOT NULL CHECK (length(trim(summary_text)) > 0),
        source_run_id TEXT NOT NULL REFERENCES agent_runs(agent_run_id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, revision)
    ) STRICT""")
    op.execute("""INSERT INTO agent_context_summaries_next
        (conversation_id, revision, covered_through_sequence, summary_text, source_run_id, created_at, updated_at)
        SELECT conversation_id, revision, covered_through_sequence, summary_text, source_run_id, created_at, updated_at
        FROM agent_context_summaries""")
    op.drop_table("agent_context_summaries")
    op.rename_table("agent_context_summaries_next", "agent_context_summaries")


def downgrade() -> None:
    """旧结构不能无损表示多个摘要，拒绝隐式丢弃或合并。"""
    raise RuntimeError("context summary history cannot be downgraded without data loss")
