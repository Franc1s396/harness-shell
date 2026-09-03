from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncssh
import pytest

from harness_shell_sidecar.connections import ConnectionProfileInput, ConnectionRepository
from harness_shell_sidecar.ssh.errors import SshRuntimeError
from harness_shell_sidecar.ssh.host_keys import candidate_from_key
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.ssh.sessions import SshSessionRegistry
from harness_shell_sidecar.storage import RuntimeDatabase


class FakeConnection:
    """记录 close 与 wait_closed 调用的最小连接或 channel 替身。"""

    def __init__(self) -> None:
        """初始化尚未关闭且尚未等待的状态。"""

        self.closed = False  # close 是否被调用。
        self.waited = False  # wait_closed 是否被等待。

    def close(self) -> None:
        """记录收到关闭请求。"""

        self.closed = True

    async def wait_closed(self) -> None:
        """记录调用方等待了关闭完成。"""

        self.waited = True

    def is_closed(self) -> bool:
        """Expose the same authoritative closed predicate as AsyncSSH."""

        return self.closed


class FailingChildChannel(FakeConnection):
    """在 wait_closed 阶段失败的子 channel 替身。"""

    async def wait_closed(self) -> None:
        """记录等待后注入关闭异常。"""

        self.waited = True
        raise OSError("child close failed")


@dataclass
class FakeConnector:
    """按顺序注入连接失败，之后返回通过 Host Key 校验的连接。"""

    #: 模拟远端返回并交给 Client 校验的 Host Key。
    host_key: object
    #: 每次调用优先弹出的预设异常。
    failures: list[Exception]
    #: connector 累计调用次数。
    attempts: int = 0
    #: 最近一次成功创建的连接替身。
    connection: FakeConnection | None = None

    async def __call__(self, host: str, port: int, **options):
        """记录尝试、注入预设失败或返回新的已验证连接。"""

        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        client = options["client_factory"]()
        client.validate_host_public_key(host, host, port, self.host_key)
        self.connection = FakeConnection()
        return self.connection


def setup_runtime(tmp_path: Path, connector: FakeConnector):
    database = RuntimeDatabase.open_plaintext((tmp_path / "runtime.sqlite3").resolve())
    repo = ConnectionRepository(database)
    value = repo.create(
        ConnectionProfileInput(
            display_name="retry",
            group_name=None,
            host="retry.example",
            port=22,
            username="deploy",
            auth_kind="password",
            credential_id=uuid4(),
            passphrase_credential_id=None,
            proxy_jump_id=None,
            favorite=False,
        )
    )
    repo.trust_first_host_key(
        candidate_from_key(
            value.connection_id, value.host, value.port, connector.host_key
        )
    )
    return database, value, SshRuntime(repo, connector=connector)


def test_retryable_pre_auth_connection_failure_retries_exactly_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            [OSError("tcp failed")],
        )
        database, value, runtime = setup_runtime(tmp_path, connector)
        try:
            status = await runtime.connect(value.connection_id, password=b"secret")
            assert status.state == "READY"
            assert connector.attempts == 2
        finally:
            await runtime.close_all()
            database.close()

    asyncio.run(scenario())


def test_child_close_failure_still_closes_target_and_jump_transports() -> None:
    async def scenario() -> None:
        sessions = SshSessionRegistry()
        target = FakeConnection()
        jump = FakeConnection()
        child = FailingChildChannel()
        session = sessions.register(
            uuid4(),
            target,
            jump,
            connection_profile_version=1,
            host_label="test-host",
            target_host_key_fingerprint="SHA256:test-target",
        )
        session.child_channels.add(child)

        with pytest.raises(OSError, match="child close failed"):
            await sessions.close(session.ssh_session_id)

        assert child.closed is True
        assert child.waited is True
        assert target.closed is True
        assert target.waited is True
        assert jump.closed is True
        assert jump.waited is True
        assert len(sessions) == 0

    asyncio.run(scenario())


def test_connected_predicate_rejects_closed_target_or_jump_transport() -> None:
    """Do not treat a registry entry with a closed transport as connected."""

    sessions = SshSessionRegistry()
    target = FakeConnection()
    jump = FakeConnection()
    session = sessions.register(
        uuid4(),
        target,
        jump,
        connection_profile_version=1,
        host_label="test-host",
        target_host_key_fingerprint="SHA256:test-target",
    )

    assert sessions.is_connected(session.ssh_session_id) is True
    jump.close()
    assert sessions.is_connected(session.ssh_session_id) is False
    jump.closed = False
    target.close()
    assert sessions.is_connected(session.ssh_session_id) is False
    assert sessions.is_connected(uuid4()) is False


def test_close_all_continues_after_an_earlier_session_fails() -> None:
    async def scenario() -> None:
        sessions = SshSessionRegistry()
        first_target = FakeConnection()
        first = sessions.register(
            uuid4(),
            first_target,
            connection_profile_version=1,
            host_label="first-host",
            target_host_key_fingerprint="SHA256:first-target",
        )
        first.child_channels.add(FailingChildChannel())
        second_target = FakeConnection()
        sessions.register(
            uuid4(),
            second_target,
            connection_profile_version=1,
            host_label="second-host",
            target_host_key_fingerprint="SHA256:second-target",
        )

        with pytest.raises(OSError, match="child close failed"):
            await sessions.close_all()

        assert first_target.closed is True
        assert second_target.closed is True
        assert second_target.waited is True
        assert len(sessions) == 0

    asyncio.run(scenario())


def test_profile_change_after_secret_resolution_blocks_network_io(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(asyncssh.generate_private_key("ssh-ed25519"), [])
        database, value, runtime = setup_runtime(tmp_path, connector)
        repo = runtime._repository
        try:
            repo.update(
                value.connection_id,
                ConnectionProfileInput(
                    display_name="changed",
                    group_name=None,
                    host="changed.example",
                    port=22,
                    username="deploy",
                    auth_kind="password",
                    credential_id=uuid4(),
                    passphrase_credential_id=None,
                    proxy_jump_id=None,
                    favorite=False,
                ),
            )
            with pytest.raises(SshRuntimeError) as raised:
                await runtime.connect(
                    value.connection_id,
                    password=b"secret",
                    expected_profile_version=value.version,
                )
            assert raised.value.error_code == "CONNECTION_PROFILE_CHANGED"
            assert connector.attempts == 0
        finally:
            database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        asyncssh.PermissionDenied("denied"),
        SshRuntimeError(
            "HOST_KEY_CHANGED",
            node="host_key",
            recoverable=False,
            remote_state="pre_auth",
        ),
    ],
)
def test_authentication_and_host_key_failures_never_retry(
    tmp_path: Path, failure: Exception
) -> None:
    async def scenario() -> None:
        connector = FakeConnector(
            asyncssh.generate_private_key("ssh-ed25519"), [failure]
        )
        database, value, runtime = setup_runtime(tmp_path, connector)
        try:
            with pytest.raises(SshRuntimeError):
                await runtime.connect(value.connection_id, password=b"secret")
            assert connector.attempts == 1
            assert len(runtime.sessions) == 0
        finally:
            database.close()

    asyncio.run(scenario())
