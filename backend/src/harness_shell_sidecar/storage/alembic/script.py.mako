"""${message}"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """在运行器事务内应用当前版本。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """显式逆向操作，不由启动过程执行。"""
    ${downgrades if downgrades else "pass"}
