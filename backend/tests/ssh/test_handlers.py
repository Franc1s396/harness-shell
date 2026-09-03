from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.credentials import ResolvedSshConnect
from harness_shell_sidecar.runtime import DispatchError, RequestDispatcher
from harness_shell_sidecar.ssh.errors import ConnectionStatus, SshRuntimeError
from harness_shell_sidecar.ssh.handlers import register_ssh_handlers


class FakeRuntime:
    """捕获 Handler 传入的瞬时秘密并返回固定 SSH 状态。"""

    def __init__(self) -> None:
        """初始化空的瞬时秘密引用列表。"""

        self.captured_secrets: list[bytearray] = []
        self.captured_arguments: dict[str, object] = {}

    async def inspect_host_key(
        self,
        connection_id: UUID,
        **secrets: object,
    ) -> ConnectionStatus:
        """捕获跳板秘密并返回需要确认 Host Key 的状态。"""

        self._capture(secrets)
        return status(connection_id, "HOST_KEY_REQUIRED")

    async def connect(
        self,
        connection_id: UUID,
        **secrets: object,
    ) -> ConnectionStatus:
        """捕获目标与跳板秘密并返回 READY 状态。"""

        self._capture(secrets)
        return status(connection_id, "READY", session_id=uuid4())

    async def disconnect(self, session_id: UUID) -> ConnectionStatus:
        """返回固定已断开状态。"""

        return status(uuid4(), "DISCONNECTED")

    def _capture(self, secrets: dict[str, object]) -> None:
        """保留参数与可清零 buffer 的别名供 finally 断言。"""

        self.captured_arguments = secrets
        self.captured_secrets = [
            value for value in secrets.values() if isinstance(value, bytearray)
        ]


class FakeCredentialService:
    """为单个连接返回测试拥有的已解析凭据。"""

    def __init__(self, resolved: ResolvedSshConnect) -> None:
        """保存下一次构建返回的唯一快照。"""

        self.resolved = resolved

    def build_ssh_connect(self, connection_id: UUID) -> ResolvedSshConnect:
        """返回字段完整的版本冻结快照。"""

        assert connection_id == self.resolved.connection_id
        return self.resolved


def status(
    connection_id: UUID,
    state: str,
    *,
    session_id: UUID | None = None,
) -> ConnectionStatus:
    """Build one safe deterministic SSH status."""

    return ConnectionStatus(
        connection_id=connection_id,
        state=state,
        session_id=session_id,
        error_code=None,
        recoverable=False,
        correlation_id=uuid4(),
        host_key_candidate=None,
    )


def test_connect_resolves_and_zeroizes_secret() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        connection_id = uuid4()
        password = bytearray(b"secret")
        resolved = ResolvedSshConnect(
            connection_id=connection_id,
            profile_version=7,
            password=password,
            _allocated=[password],
        )
        register_ssh_handlers(
            dispatcher,
            runtime,
            FakeCredentialService(resolved),
        )

        result = await dispatcher.dispatch(
            uuid4(),
            "ssh.connect",
            {"connection_id": str(connection_id)},
        )

        assert result["status"]["state"] == "READY"
        assert runtime.captured_arguments["expected_profile_version"] == 7
        assert runtime.captured_secrets
        assert all(not any(secret) for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_connect_rejects_every_public_secret_or_version_field() -> None:
    async def scenario() -> None:
        connection_id = uuid4()
        for forbidden in (
            {"password_b64": "c2VjcmV0"},
            {"profile_version": 1},
            {"jump": None},
        ):
            dispatcher = RequestDispatcher()
            resolved = ResolvedSshConnect(
                connection_id=connection_id,
                profile_version=1,
            )
            register_ssh_handlers(
                dispatcher,
                FakeRuntime(),
                FakeCredentialService(resolved),
            )
            with pytest.raises(DispatchError) as raised:
                await dispatcher.dispatch(
                    uuid4(),
                    "ssh.connect",
                    {"connection_id": str(connection_id), **forbidden},
                )
            assert raised.value.error_code == "INVALID_REQUEST_PAYLOAD"

    asyncio.run(scenario())


def test_proxy_jump_connect_zeroizes_both_credentials() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        connection_id = uuid4()
        target = bytearray(b"target-secret")
        jump = bytearray(b"jump-secret")
        resolved = ResolvedSshConnect(
            connection_id=connection_id,
            profile_version=7,
            password=target,
            jump_connection_id=uuid4(),
            jump_profile_version=3,
            jump_password=jump,
            _allocated=[target, jump],
        )
        register_ssh_handlers(
            dispatcher,
            runtime,
            FakeCredentialService(resolved),
        )

        result = await dispatcher.dispatch(
            uuid4(),
            "ssh.connect",
            {"connection_id": str(connection_id)},
        )

        assert result["status"]["state"] == "READY"
        assert runtime.captured_arguments["expected_jump_profile_version"] == 3
        assert len(runtime.captured_secrets) == 2
        assert all(not any(secret) for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_proxy_jump_host_key_inspection_zeroizes_credential() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        connection_id = uuid4()
        jump = bytearray(b"jump-secret")
        resolved = ResolvedSshConnect(
            connection_id=connection_id,
            profile_version=1,
            jump_connection_id=uuid4(),
            jump_profile_version=3,
            jump_password=jump,
            _allocated=[jump],
        )
        register_ssh_handlers(
            dispatcher,
            runtime,
            FakeCredentialService(resolved),
        )

        result = await dispatcher.dispatch(
            uuid4(),
            "host_key.inspect",
            {"connection_id": str(connection_id)},
        )

        assert result["status"]["state"] == "HOST_KEY_REQUIRED"
        assert runtime.captured_secrets
        assert all(not any(secret) for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_ssh_errors_keep_only_structured_safe_details() -> None:
    class FailingRuntime(FakeRuntime):
        """Host Key 检查总是抛出结构化错误的运行时替身。"""

        async def inspect_host_key(
            self,
            connection_id: UUID,
            **secrets: object,
        ) -> ConnectionStatus:
            """注入不包含底层秘密的 HOST_KEY_CHANGED 失败。"""

            raise SshRuntimeError(
                "HOST_KEY_CHANGED",
                node="host_key",
                recoverable=False,
                remote_state="pre_auth",
            )

    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        connection_id = uuid4()
        resolved = ResolvedSshConnect(
            connection_id=connection_id,
            profile_version=1,
        )
        register_ssh_handlers(
            dispatcher,
            FailingRuntime(),
            FakeCredentialService(resolved),
        )
        with pytest.raises(DispatchError) as raised:
            await dispatcher.dispatch(
                uuid4(),
                "host_key.inspect",
                {"connection_id": str(connection_id)},
            )
        assert raised.value.error_code == "HOST_KEY_CHANGED"
        assert raised.value.details["node"] == "host_key"
        assert raised.value.details["remote_state"] == "pre_auth"
        assert "username" not in str(raised.value.details)
        assert "secret" not in str(raised.value.details)

    asyncio.run(scenario())
