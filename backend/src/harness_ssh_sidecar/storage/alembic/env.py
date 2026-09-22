"""只借用外层迁移事务的连接，不创建 Engine 或提交事务。"""

from alembic import context


def run_migrations_online() -> None:
    """所有 revision 及版本写入都加入运行器的单一事务。"""
    connection = context.config.attributes["connection"]
    context.configure(connection=connection, transactional_ddl=True,
                      transaction_per_migration=False)
    context.run_migrations()


run_migrations_online()
