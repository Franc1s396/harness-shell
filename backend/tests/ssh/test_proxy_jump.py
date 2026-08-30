from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import asyncssh
import pytest

from harness_shell_sidecar.connections import ConnectionProfileInput, ConnectionRepository
from harness_shell_sidecar.ssh.errors import SshRuntimeError
from harness_shell_sidecar.ssh.host_keys import candidate_from_key
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.storage import RuntimeDatabase


class FakeConnection:
    """记录 ProxyJump 目标或跳板关闭顺序的连接替身。"""

    def __init__(
        self,
        name: str,
        close_order: list[str],
        *,
        fail_wait_closed: bool = False,
    ) -> None:
        """绑定连接名称、共享顺序列表及可选关闭失败。"""

        self.name = name  # 区分 jump 与 target 的可读名称。
        self.close_order = close_order  # 共享的 close 调用顺序记录。
        self.fail_wait_closed = fail_wait_closed  # 是否在等待关闭时注入异常。
        self.closed = False  # close 是否被调用。
        self.waited = False  # wait_closed 是否被等待。

    def close(self) -> None:
        """记录关闭状态和当前连接名称。"""

        self.closed = True
        self.close_order.append(self.name)

    async def wait_closed(self) -> None:
        """记录等待动作，并按配置注入关闭失败。"""

        self.waited = True
        if self.fail_wait_closed:
            raise OSError(f"{self.name} close failed")


@dataclass
class ProxyConnector:
    """模拟两段 ProxyJump 连接并验证端点、认证和 tunnel 选项。"""

    #: 跳板端点实际返回的 Host Key。
    jump_key: object
    #: 目标端点实际返回的 Host Key。
    target_key: object
    #: 每次 connector 调用的完整端点与选项记录。
    calls: list[dict] = field(default_factory=list)
    #: 已成功创建、可供关闭断言的连接替身。
    connections: list[FakeConnection] = field(default_factory=list)
    #: 实际携带密码进入认证的端点调用记录。
    auth_calls: list[tuple[str, str, str]] = field(default_factory=list)
    #: target 与 jump 的共享关闭顺序记录。
    close_order: list[str] = field(default_factory=list)
    #: 是否让目标连接在 wait_closed 时失败。
    fail_target_close: bool = False

    async def __call__(self, host: str, port: int, **options):
        """验证连接选项、执行 Host Key 回调并返回命名连接替身。"""

        call = {"host": host, "port": port, **options}
        self.calls.append(call)
        if host == "jump.example":
            key = self.jump_key
            expected_username = "jump-user"
            expected_password = "jump-secret"
            name = "jump"
        elif host == "target.example":
            key = self.target_key
            expected_username = "target-user"
            expected_password = "target-secret"
            name = "target"
            assert isinstance(options.get("tunnel"), FakeConnection)
            assert options["tunnel"].name == "jump"
        else:
            raise AssertionError(f"unexpected endpoint {host}")

        client = options["client_factory"]()
        client.validate_host_public_key(host, host, port, key)
        if options.get("password") is not None:
            assert options["username"] == expected_username
            assert options["password"] == expected_password
            self.auth_calls.append(
                (host, options["username"], options["password"])
            )
        connection = FakeConnection(
            name,
            self.close_order,
            fail_wait_closed=name == "target" and self.fail_target_close,
        )
        self.connections.append(connection)
        return connection

    def calls_for(self, host: str) -> list[dict]:
        """筛选指定端点的 connector 调用记录。"""

        return [call for call in self.calls if call["host"] == host]


def setup_profiles(tmp_path: Path, connector: ProxyConnector):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    repository = ConnectionRepository(database)
    jump = repository.create(
        ConnectionProfileInput(
            display_name="jump",
            group_name=None,
            host="jump.example",
            port=22,
            username="jump-user",
            auth_kind="password",
            credential_id=uuid4(),
            passphrase_credential_id=None,
            proxy_jump_id=None,
            favorite=False,
        )
    )
    target = repository.create(
        ConnectionProfileInput(
            display_name="target",
            group_name=None,
            host="target.example",
            port=22,
            username="target-user",
            auth_kind="password",
            credential_id=uuid4(),
            passphrase_credential_id=None,
            proxy_jump_id=jump.connection_id,
            favorite=False,
        )
    )
    runtime = SshRuntime(repository, connector=connector)
    return database, repository, jump, target, runtime


def trust(repository, profile, key) -> None:
    repository.trust_first_host_key(
        candidate_from_key(
            profile.connection_id, profile.host, profile.port, key
        )
    )


def connect(runtime: SshRuntime, target, jump):
    return runtime.connect(
        target.connection_id,
        password=b"target-secret",
        expected_profile_version=target.version,
        jump_password=b"jump-secret",
        expected_jump_profile_version=jump.version,
    )


def test_untrusted_jump_stops_before_jump_auth_and_target_contact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, _, jump, target, runtime = setup_profiles(tmp_path, connector)
        try:
            status = await connect(runtime, target, jump)
            assert status.state == "HOST_KEY_REQUIRED"
            assert status.host_key_candidate.connection_id == jump.connection_id
            assert len(connector.calls_for("jump.example")) == 1
            assert connector.calls_for("jump.example")[0].get("password") is None
            assert connector.auth_calls == []
            assert connector.calls_for("target.example") == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_trusted_jump_and_untrusted_target_returns_target_candidate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        trust(repository, jump, connector.jump_key)
        try:
            status = await connect(runtime, target, jump)
            assert status.state == "HOST_KEY_REQUIRED"
            assert status.host_key_candidate.connection_id == target.connection_id
            assert connector.calls_for("jump.example")[0]["password"] == "jump-secret"
            assert connector.calls_for("target.example")[0].get("password") is None
            assert all(connection.closed for connection in connector.connections)
            assert all(connection.waited for connection in connector.connections)
        finally:
            database.close()

    asyncio.run(scenario())


def test_changed_jump_key_blocks_auth_and_target_contact(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        trust(repository, jump, asyncssh.generate_private_key("ssh-ed25519"))
        trust(repository, target, connector.target_key)
        try:
            with pytest.raises(SshRuntimeError) as raised:
                await connect(runtime, target, jump)
            assert raised.value.error_code == "HOST_KEY_CHANGED"
            assert raised.value.trusted_fingerprint_sha256 is not None
            assert connector.calls_for("target.example") == []
            assert connector.auth_calls == []
        finally:
            database.close()

    asyncio.run(scenario())


def test_proxy_jump_uses_separate_identities_and_disconnects_target_first(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        trust(repository, jump, connector.jump_key)
        trust(repository, target, connector.target_key)
        try:
            status = await connect(runtime, target, jump)
            assert status.state == "READY"
            assert connector.auth_calls == [
                ("jump.example", "jump-user", "jump-secret"),
                ("target.example", "target-user", "target-secret"),
            ]
            session = runtime.sessions.get(status.session_id)
            assert session is not None
            assert session.connection.name == "target"
            assert session.jump_connection.name == "jump"

            await runtime.disconnect(status.session_id)
            assert connector.close_order == ["target", "jump"]
            assert len(runtime.sessions) == 0
        finally:
            database.close()

    asyncio.run(scenario())


def test_disconnect_closes_jump_even_when_target_close_raises(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
            fail_target_close=True,
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        trust(repository, jump, connector.jump_key)
        trust(repository, target, connector.target_key)
        try:
            status = await connect(runtime, target, jump)
            with pytest.raises(OSError, match="target close failed"):
                await runtime.disconnect(status.session_id)
            assert connector.close_order == ["target", "jump"]
            assert len(runtime.sessions) == 0
        finally:
            database.close()

    asyncio.run(scenario())


def test_changed_target_key_closes_jump_and_retains_no_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        trust(repository, jump, connector.jump_key)
        trust(repository, target, asyncssh.generate_private_key("ssh-ed25519"))
        try:
            with pytest.raises(SshRuntimeError) as raised:
                await connect(runtime, target, jump)
            assert raised.value.error_code == "HOST_KEY_CHANGED"
            assert len(runtime.sessions) == 0
            assert connector.connections[0].name == "jump"
            assert connector.connections[0].closed is True
            assert connector.connections[0].waited is True
        finally:
            database.close()

    asyncio.run(scenario())


def test_nested_jump_is_rejected_before_network_io(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = ProxyConnector(
            asyncssh.generate_private_key("ssh-ed25519"),
            asyncssh.generate_private_key("ssh-ed25519"),
        )
        database, repository, jump, target, runtime = setup_profiles(
            tmp_path, connector
        )
        third = repository.create(
            ConnectionProfileInput(
                display_name="third",
                group_name=None,
                host="third.example",
                port=22,
                username="third-user",
                auth_kind="password",
                credential_id=uuid4(),
                passphrase_credential_id=None,
                proxy_jump_id=None,
                favorite=False,
            )
        )
        database.execute(
            "UPDATE connection_profiles SET proxy_jump_id = ? WHERE connection_id = ?",
            (str(third.connection_id), str(jump.connection_id)),
        )
        try:
            with pytest.raises(SshRuntimeError) as raised:
                await connect(runtime, target, jump)
            assert raised.value.error_code == "MULTI_HOP_PROXY_FORBIDDEN"
            assert connector.calls == []
        finally:
            database.close()

    asyncio.run(scenario())
