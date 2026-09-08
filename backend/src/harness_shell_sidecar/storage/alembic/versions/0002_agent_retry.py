"""为用户消息的显式重试保存稳定关联，不改动既有历史。"""
from alembic import op
import sqlalchemy as sa

revision = "0002_agent_retry"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧 Run 保留空关联，新请求写入用户消息标识。"""
    op.add_column("agent_runs", sa.Column("user_message_id", sa.Text(), nullable=True))
    op.create_index("ix_agent_runs_user_message_id", "agent_runs", ["user_message_id"])


def downgrade() -> None:
    """移除重试关联字段，保留 Run 和消息正文。"""
    op.drop_index("ix_agent_runs_user_message_id", table_name="agent_runs")
    op.drop_column("agent_runs", "user_message_id")
