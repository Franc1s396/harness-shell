"""审批借用 SSH 生命周期，不关闭或重建主连接。"""

import asyncio
from uuid import uuid4

from harness_shell_sidecar.ssh.sessions import SshSessionRegistry


class Connection:
    """仅通过显式 Event 通知真实断连的测试传输。"""

    def __init__(self) -> None:
        """初始化等待信号。"""
        self.closed = asyncio.Event()

    def is_closed(self) -> bool:
        """返回传输是否关闭。"""
        return self.closed.is_set()

    def close(self) -> None:
        """通知连接已关闭。"""
        self.closed.set()

    async def wait_closed(self) -> None:
        """等待显式断连。"""
        await self.closed.wait()


def test_target_snapshot_and_disconnect_wait() -> None:
    async def scenario() -> None:
        """自然断连唤醒审批，本地取消等待不关闭连接。"""
        registry = SshSessionRegistry()
        connection = Connection()
        session = registry.register(uuid4(), connection, connection_profile_version=1,
            host_label="server", target_host_key_fingerprint="test", host="example.org", port=22, username="ops")
        assert (session.host, session.port, session.username) == ("example.org", 22, "ops")
        waiter = asyncio.create_task(registry.wait_unavailable(session.ssh_session_id))
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        assert not connection.is_closed()
        waiter = asyncio.create_task(registry.wait_unavailable(session.ssh_session_id))
        connection.close()
        await asyncio.wait_for(waiter, 1)
        await registry.close_all()
    asyncio.run(scenario())


def test_jump_loss_and_independent_waiters() -> None:
    """两个 Run 借用同一跳板连接时，取消其中一个不影响另一个。"""
    async def scenario() -> None:
        registry = SshSessionRegistry()
        target, jump = Connection(), Connection()
        session = registry.register(uuid4(), target, connection_profile_version=1,
            host_label="server", host="target", port=22, username="ops", target_host_key_fingerprint="test",
            jump_connection=jump)
        first = asyncio.create_task(registry.wait_unavailable(session.ssh_session_id))
        second = asyncio.create_task(registry.wait_unavailable(session.ssh_session_id))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert not second.done() and not target.is_closed() and not jump.is_closed()
        jump.close()
        await asyncio.wait_for(second, 1)
        await registry.close_all()
    asyncio.run(scenario())


def test_local_removal_immediately_wakes_waiters() -> None:
    """本地 registry 移除通知先于传输 wait_closed 清理结束。"""
    class SlowClose(Connection):
        """由独立门禁延迟传输最终清理。"""
        def __init__(self) -> None:
            super().__init__()
            self.released = asyncio.Event()  # 控制 cleanup 的最终结束。
        async def wait_closed(self) -> None:
            await self.released.wait()
    async def scenario() -> None:
        registry = SshSessionRegistry()
        target = SlowClose()
        session = registry.register(uuid4(), target, connection_profile_version=1,
            host_label="server", host="target", port=22, username="ops", target_host_key_fingerprint="test")
        waiter = asyncio.create_task(registry.wait_unavailable(session.ssh_session_id))
        await asyncio.sleep(0)
        closing = asyncio.create_task(registry.close(session.ssh_session_id))
        try:
            await asyncio.wait_for(waiter, 1)
            assert not closing.done()
        finally:
            target.released.set()
            await closing
    asyncio.run(scenario())
