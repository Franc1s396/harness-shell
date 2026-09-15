"""添加 Agent 图片元数据与原图 BLOB，历史表保持不变。"""
from alembic import op

revision = "0004_agent_image_attachments"
down_revision = "0003_context_summary_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """独立声明历史 DDL，事务由启动 migration owner 提交。"""
    op.execute("""CREATE TABLE agent_attachments (
        attachment_id TEXT NOT NULL PRIMARY KEY,
        draft_id TEXT NOT NULL,
        conversation_id TEXT REFERENCES agent_conversations(conversation_id) ON DELETE CASCADE,
        user_message_id TEXT,
        position INTEGER,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        CHECK ((conversation_id IS NULL AND user_message_id IS NULL AND position IS NULL) OR (conversation_id IS NOT NULL AND user_message_id IS NOT NULL AND position IS NOT NULL)),
        CHECK (position IS NULL OR position BETWEEN 0 AND 4),
        CHECK (byte_size BETWEEN 1 AND 10485760),
        CHECK (width > 0 AND height > 0 AND width * height <= 40000000),
        CHECK (media_type IN ('image/png','image/jpeg','image/webp','image/gif')),
        UNIQUE (user_message_id, position)
    ) STRICT""")
    for column in ('draft_id', 'conversation_id', 'user_message_id'):
        op.create_index(f'ix_agent_attachments_{column}', 'agent_attachments', [column])
    op.execute("""CREATE TABLE agent_attachment_contents (
        attachment_id TEXT NOT NULL PRIMARY KEY REFERENCES agent_attachments(attachment_id) ON DELETE CASCADE,
        data BLOB NOT NULL,
        CHECK (length(data) BETWEEN 1 AND 10485760)
    ) STRICT""")


def downgrade() -> None:
    """图片历史不能被隐式丢弃。"""
    raise RuntimeError("image attachment downgrade would discard user data")
