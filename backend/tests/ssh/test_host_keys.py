from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncssh
import pytest

from harness_shell_sidecar.connections import ConnectionProfileInput, ConnectionRepository
from harness_shell_sidecar.ssh.errors import SshRuntimeError
from harness_shell_sidecar.ssh.host_keys import candidate_from_key
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.storage import RuntimeDatabase


@dataclass
class AuthProbe:
    """统计测试 SSH Server 实际进入密码认证的次数。"""

    #: validate_password 被调用的次数。
    calls: int = 0


class PasswordServer(asyncssh.SSHServer):
    """只接受固定测试账号并记录认证调用的 AsyncSSH Server。"""

    def __init__(self, probe: AuthProbe) -> None:
        """绑定跨连接共享的认证计数探针。"""

        self._probe = probe  # 用于证明 Host Key 失败发生在认证之前。

    def begin_auth(self, username: str) -> bool:
        """要求客户端进入用户认证阶段。"""

        return True

    def password_auth_supported(self) -> bool:
        """声明测试 Server 支持密码认证。"""

        return True

    def validate_password(self, username: str, password: str) -> bool:
        """记录认证调用并仅接受固定测试用户名和密码。"""

        self._probe.calls += 1
        return username == "deploy" and password == "secret"


async def start_server(host_key, probe: AuthProbe):
    return await asyncssh.create_server(
        lambda: PasswordServer(probe),
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
    )


def profile(repository: ConnectionRepository, port: int):
    return repository.create(
        ConnectionProfileInput(
            display_name="local",
            group_name=None,
            host="127.0.0.1",
            port=port,
            username="deploy",
            auth_kind="password",
            credential_id=UUID("00000000-0000-4000-8000-000000000001"),
            passphrase_credential_id=None,
            proxy_jump_id=None,
            favorite=False,
        )
    )


def repository(tmp_path: Path):
    database = RuntimeDatabase.open((tmp_path / "runtime.sqlite3").resolve())
    return database, ConnectionRepository(database)


def test_first_observation_returns_candidate_before_authentication(tmp_path: Path) -> None:
    async def scenario() -> None:
        probe = AuthProbe()
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        server = await start_server(host_key, probe)
        database, repo = repository(tmp_path)
        try:
            value = profile(repo, server.get_port())
            runtime = SshRuntime(repo)
            status = await runtime.inspect_host_key(value.connection_id)
            assert status.state == "HOST_KEY_REQUIRED"
            assert status.host_key_candidate is not None
            assert status.host_key_candidate.fingerprint_sha256 == host_key.get_fingerprint(
                "sha256"
            )
            assert probe.calls == 0
            assert len(runtime.sessions) == 0
        finally:
            server.close()
            await server.wait_closed()
            database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "second_algorithm", ["ssh-ed25519", "ecdsa-sha2-nistp256"]
)
def test_changed_host_key_fails_before_authentication(
    tmp_path: Path, second_algorithm: str
) -> None:
    async def scenario() -> None:
        probe = AuthProbe()
        first_key = asyncssh.generate_private_key("ssh-ed25519")
        second_key = asyncssh.generate_private_key(second_algorithm)
        server = await start_server(second_key, probe)
        database, repo = repository(tmp_path)
        try:
            value = profile(repo, server.get_port())
            repo.trust_first_host_key(
                candidate_from_key(
                    value.connection_id,
                    value.host,
                    value.port,
                    first_key,
                )
            )
            runtime = SshRuntime(repo)
            inspection = await runtime.inspect_host_key(value.connection_id)
            assert inspection.state == "FAILED"
            assert inspection.error_code == "HOST_KEY_CHANGED"
            assert (
                inspection.trusted_fingerprint_sha256
                == first_key.get_fingerprint("sha256")
            )
            assert inspection.host_key_candidate is not None
            assert (
                inspection.host_key_candidate.fingerprint_sha256
                == second_key.get_fingerprint("sha256")
            )
            with pytest.raises(SshRuntimeError) as raised:
                await runtime.connect(value.connection_id, password=b"secret")
            assert raised.value.error_code == "HOST_KEY_CHANGED"
            assert raised.value.remote_state == "pre_auth"
            assert probe.calls == 0
            assert len(runtime.sessions) == 0
        finally:
            server.close()
            await server.wait_closed()
            database.close()

    asyncio.run(scenario())


def test_exact_host_key_connects_and_disconnects_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        probe = AuthProbe()
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        server = await start_server(host_key, probe)
        database, repo = repository(tmp_path)
        try:
            value = profile(repo, server.get_port())
            repo.trust_first_host_key(
                candidate_from_key(
                    value.connection_id,
                    value.host,
                    value.port,
                    host_key,
                )
            )
            runtime = SshRuntime(repo)
            status = await runtime.connect(value.connection_id, password=b"secret")
            assert status.state == "READY"
            assert status.session_id is not None
            assert probe.calls == 1
            assert len(runtime.sessions) == 1

            disconnected = await runtime.disconnect(status.session_id)
            assert disconnected.state == "DISCONNECTED"
            assert len(runtime.sessions) == 0
        finally:
            server.close()
            await server.wait_closed()
            database.close()

    asyncio.run(scenario())
