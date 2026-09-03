"""Direct AsyncSSH connection lifecycle with fail-closed Host Key checks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import asyncssh

from harness_shell_sidecar.connections import ConnectionRepository

from .auth import build_auth_options
from .errors import ConnectionStatus, SshRuntimeError
from .host_keys import (
    HostKeyMismatch,
    HostKeyObserved,
    InspectHostKeyClient,
    VerifiedHostKeyClient,
    empty_known_hosts,
)
from .sessions import SshSessionRegistry


StatusListener = Callable[[ConnectionStatus], Awaitable[None]]


class SshRuntime:
    """执行 Host Key 验证、认证、ProxyJump 和会话生命周期的 SSH 控制面。"""

    def __init__(
        self,
        repository: ConnectionRepository,
        *,
        connector: Callable[..., Awaitable[Any]] = asyncssh.connect,
        status_listener: StatusListener | None = None,
    ) -> None:
        """注入连接仓储与状态监听器，并创建空会话注册表。"""

        self._repository = repository  # 读取连接配置和受信任 Host Key。
        self._connector = connector  # AsyncSSH 连接工厂；测试可替换为 Fake。
        self._status_listener = status_listener  # 向桌面端发布状态事件的回调。
        self.sessions = SshSessionRegistry()  # 对 PTY/exec/SFTP 暴露的活动会话。

    async def inspect_host_key(
        self,
        connection_id: UUID,
        *,
        jump_password: bytes | bytearray | None = None,
        jump_private_key: bytes | bytearray | None = None,
        jump_passphrase: bytes | bytearray | None = None,
        expected_jump_profile_version: int | None = None,
        jump_connection_id: UUID | None = None,
    ) -> ConnectionStatus:
        """检查目标或跳板 Host Key，且不保留任何认证会话。"""

        correlation_id = uuid4()
        profile = self._profile(connection_id, correlation_id)
        if profile.proxy_jump_id is None:
            return await self._inspect_profile(profile, correlation_id)

        jump = self._proxy_profile(
            profile,
            expected_jump_profile_version,
            correlation_id,
            expected_connection_id=jump_connection_id,
        )
        active_jump_key = self._repository.active_host_key(jump.connection_id)
        if active_jump_key is None:
            return await self._inspect_profile(jump, correlation_id)

        jump_auth = build_auth_options(
            auth_kind=jump.auth_kind,
            password=jump_password,
            private_key=jump_private_key,
            passphrase=jump_passphrase,
        )
        jump_connection = await self._open_verified(
            jump, active_jump_key, jump_auth, correlation_id, node="proxy_jump"
        )
        try:
            return await self._inspect_profile(
                profile, correlation_id, tunnel=jump_connection
            )
        finally:
            await self._close_connection(jump_connection)

    async def _inspect_profile(
        self, profile, correlation_id: UUID, *, tunnel=None
    ) -> ConnectionStatus:
        """通过专用 Client 截获 Profile 端点的 Host Key 并生成安全状态。"""

        options = {
            "username": profile.username,
            "client_factory": lambda: InspectHostKeyClient(
                profile.connection_id, profile.host, profile.port
            ),
            "known_hosts": empty_known_hosts(),
            "agent_path": None,
            "client_keys": [],
            "password": None,
            "preferred_auth": [],
        }
        if tunnel is not None:
            options["tunnel"] = tunnel
        try:
            connection = await self._connector(
                profile.host, profile.port, **options
            )
        except HostKeyObserved as observed:
            active = self._repository.active_host_key(profile.connection_id)
            if active is None:
                return self._status(
                    profile.connection_id,
                    "HOST_KEY_REQUIRED",
                    correlation_id,
                    candidate=observed.candidate,
                )
            if self._matches(active, observed.candidate):
                return self._status(
                    profile.connection_id, "DISCONNECTED", correlation_id
                )
            return self._status(
                profile.connection_id,
                "FAILED",
                correlation_id,
                error_code="HOST_KEY_CHANGED",
                candidate=observed.candidate,
                trusted_fingerprint_sha256=active.fingerprint_sha256,
            )
        except (OSError, asyncssh.DisconnectError) as exc:
            raise SshRuntimeError(
                "HOST_KEY_INSPECTION_FAILED",
                node="host_key",
                recoverable=True,
                remote_state="pre_auth",
                correlation_id=correlation_id,
            ) from exc
        else:
            connection.close()
            await connection.wait_closed()
            raise SshRuntimeError(
                "HOST_KEY_CALLBACK_NOT_INVOKED",
                node="host_key",
                recoverable=False,
                remote_state="unknown",
                correlation_id=correlation_id,
            )

    async def connect(
        self,
        connection_id: UUID,
        *,
        password: bytes | bytearray | None = None,
        private_key: bytes | bytearray | None = None,
        passphrase: bytes | bytearray | None = None,
        expected_profile_version: int | None = None,
        jump_password: bytes | bytearray | None = None,
        jump_private_key: bytes | bytearray | None = None,
        jump_passphrase: bytes | bytearray | None = None,
        expected_jump_profile_version: int | None = None,
        jump_connection_id: UUID | None = None,
    ) -> ConnectionStatus:
        """验证配置快照与 Host Key 后，在最多两次尝试内建立 SSH 会话。"""

        correlation_id = uuid4()
        profile = self._profile(connection_id, correlation_id)
        if (
            expected_profile_version is not None
            and profile.version != expected_profile_version
        ):
            raise SshRuntimeError(
                "CONNECTION_PROFILE_CHANGED",
                node="profile",
                recoverable=True,
                remote_state="not_contacted",
                correlation_id=correlation_id,
            )
        if profile.proxy_jump_id is not None:
            jump = self._proxy_profile(
                profile,
                expected_jump_profile_version,
                correlation_id,
                expected_connection_id=jump_connection_id,
            )
            return await self._connect_via_proxy(
                profile,
                jump,
                correlation_id,
                password=password,
                private_key=private_key,
                passphrase=passphrase,
                jump_password=jump_password,
                jump_private_key=jump_private_key,
                jump_passphrase=jump_passphrase,
            )

        active_host_key = self._repository.active_host_key(connection_id)
        if active_host_key is None:
            return await self._inspect_profile(profile, correlation_id)

        auth_options = build_auth_options(
            auth_kind=profile.auth_kind,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
        )
        await self._emit(
            self._status(connection_id, "CONNECTING", correlation_id)
        )
        for attempt in (1, 2):
            try:
                connection = await self._open_verified(
                    profile,
                    active_host_key,
                    auth_options,
                    correlation_id,
                    node="target",
                )
            except SshRuntimeError as error:
                await self._emit_error(profile.connection_id, error)
                raise
            except (OSError, asyncssh.DisconnectError) as exc:
                if attempt == 1:
                    continue
                error = SshRuntimeError(
                    "SSH_CONNECT_FAILED",
                    node="connect",
                    recoverable=True,
                    remote_state="pre_auth",
                    correlation_id=correlation_id,
                )
                await self._emit_error(profile.connection_id, error)
                raise error from exc
            else:
                session = self.sessions.register(
                    profile.connection_id,
                    connection,
                    connection_profile_version=profile.version,
                    host_label=profile.display_name,
                    target_host_key_fingerprint=active_host_key.fingerprint_sha256,
                )
                status = self._status(
                    connection_id,
                    "READY",
                    correlation_id,
                    session_id=session.ssh_session_id,
                )
                await self._emit(status)
                return status

        raise AssertionError("bounded connect loop must return or raise")

    async def _connect_via_proxy(
        self,
        profile,
        jump,
        correlation_id: UUID,
        *,
        password,
        private_key,
        passphrase,
        jump_password,
        jump_private_key,
        jump_passphrase,
    ) -> ConnectionStatus:
        """验证跳板和目标两个端点后建立单层 ProxyJump 会话。"""

        active_jump_key = self._repository.active_host_key(jump.connection_id)
        if active_jump_key is None:
            return await self._inspect_profile(jump, correlation_id)

        jump_auth = build_auth_options(
            auth_kind=jump.auth_kind,
            password=jump_password,
            private_key=jump_private_key,
            passphrase=jump_passphrase,
        )
        active_target_key = self._repository.active_host_key(profile.connection_id)
        if active_target_key is None:
            jump_connection = await self._open_verified(
                jump,
                active_jump_key,
                jump_auth,
                correlation_id,
                node="proxy_jump",
            )
            try:
                return await self._inspect_profile(
                    profile, correlation_id, tunnel=jump_connection
                )
            finally:
                await self._close_connection(jump_connection)

        target_auth = build_auth_options(
            auth_kind=profile.auth_kind,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
        )
        await self._emit(
            self._status(profile.connection_id, "CONNECTING", correlation_id)
        )

        for attempt in (1, 2):
            jump_connection = None
            try:
                jump_connection = await self._open_verified(
                    jump,
                    active_jump_key,
                    jump_auth,
                    correlation_id,
                    node="proxy_jump",
                )
                target_connection = await self._open_verified(
                    profile,
                    active_target_key,
                    target_auth,
                    correlation_id,
                    node="target",
                    tunnel=jump_connection,
                )
            except SshRuntimeError as error:
                if jump_connection is not None:
                    await self._close_connection(jump_connection)
                await self._emit_error(profile.connection_id, error)
                raise
            except (OSError, asyncssh.DisconnectError) as exc:
                if jump_connection is not None:
                    await self._close_connection(jump_connection)
                if attempt == 1:
                    continue
                error = SshRuntimeError(
                    "SSH_CONNECT_FAILED",
                    node="connect",
                    recoverable=True,
                    remote_state="pre_auth",
                    correlation_id=correlation_id,
                )
                await self._emit_error(profile.connection_id, error)
                raise error from exc
            else:
                session = self.sessions.register(
                    profile.connection_id,
                    target_connection,
                    jump_connection,
                    connection_profile_version=profile.version,
                    host_label=profile.display_name,
                    target_host_key_fingerprint=active_target_key.fingerprint_sha256,
                    jump_connection_id=jump.connection_id,
                    jump_profile_version=jump.version,
                    jump_host_key_fingerprint=active_jump_key.fingerprint_sha256,
                )
                status = self._status(
                    profile.connection_id,
                    "READY",
                    correlation_id,
                    session_id=session.ssh_session_id,
                )
                await self._emit(status)
                return status

        raise AssertionError("bounded proxy connect loop must return or raise")

    async def disconnect(self, session_id: UUID) -> ConnectionStatus:
        """发布关闭状态，收敛指定会话全部资源，再发布已断开状态。"""

        session = self.sessions.get(session_id)
        if session is None:
            raise SshRuntimeError(
                "SSH_SESSION_NOT_FOUND",
                node="disconnect",
                recoverable=False,
                remote_state="not_contacted",
            )
        correlation_id = uuid4()
        await self._emit(
            self._status(
                session.connection_id,
                "CLOSING",
                correlation_id,
                session_id=session_id,
            )
        )
        await self.sessions.close(session_id)
        status = self._status(
            session.connection_id, "DISCONNECTED", correlation_id
        )
        await self._emit(status)
        return status

    async def close_all(self) -> None:
        """关闭注册表中的全部 SSH 会话及其子 channel。"""

        await self.sessions.close_all()

    def _profile(self, connection_id: UUID, correlation_id: UUID):
        """读取必需连接配置，并把缺失转换为结构化 SSH 错误。"""

        profile = self._repository.get(connection_id)
        if profile is None:
            raise SshRuntimeError(
                "CONNECTION_NOT_FOUND",
                node="profile",
                recoverable=False,
                remote_state="not_contacted",
                correlation_id=correlation_id,
            )
        return profile

    def _proxy_profile(
        self,
        profile,
        expected_version: int | None,
        correlation_id: UUID,
        *,
        expected_connection_id: UUID | None = None,
    ):
        """解析并校验单层跳板配置及调用方持有的版本快照。"""

        jump = self._profile(profile.proxy_jump_id, correlation_id)
        if (
            expected_connection_id is not None
            and jump.connection_id != expected_connection_id
        ):
            raise SshRuntimeError(
                "PROXY_JUMP_PROFILE_MISMATCH",
                node="proxy_jump.profile",
                recoverable=False,
                remote_state="not_contacted",
                correlation_id=correlation_id,
            )
        if jump.proxy_jump_id is not None:
            raise SshRuntimeError(
                "MULTI_HOP_PROXY_FORBIDDEN",
                node="proxy_jump",
                recoverable=False,
                remote_state="not_contacted",
                correlation_id=correlation_id,
            )
        if expected_version is not None and jump.version != expected_version:
            raise SshRuntimeError(
                "CONNECTION_PROFILE_CHANGED",
                node="proxy_jump.profile",
                recoverable=True,
                remote_state="not_contacted",
                correlation_id=correlation_id,
            )
        return jump

    async def _open_verified(
        self,
        profile,
        active_host_key,
        auth_options: dict,
        correlation_id: UUID,
        *,
        node: str,
        tunnel=None,
    ):
        """仅用精确匹配的 Host Key 和显式认证选项打开 AsyncSSH 连接。"""

        options = {
            "username": profile.username,
            "client_factory": lambda: VerifiedHostKeyClient(
                profile.connection_id,
                profile.host,
                profile.port,
                active_host_key,
            ),
            "known_hosts": empty_known_hosts(),
            **auth_options,
        }
        if tunnel is not None:
            options["tunnel"] = tunnel
        try:
            return await self._connector(profile.host, profile.port, **options)
        except HostKeyMismatch as mismatch:
            error_node = "host_key" if node == "target" else f"{node}.host_key"
            raise SshRuntimeError(
                "HOST_KEY_CHANGED",
                node=error_node,
                recoverable=False,
                remote_state="pre_auth",
                correlation_id=correlation_id,
                candidate=mismatch.candidate,
                trusted_fingerprint_sha256=active_host_key.fingerprint_sha256,
            ) from mismatch
        except asyncssh.PermissionDenied as exc:
            error_node = (
                "authentication" if node == "target" else f"{node}.authentication"
            )
            raise SshRuntimeError(
                "SSH_AUTHENTICATION_FAILED",
                node=error_node,
                recoverable=False,
                remote_state="pre_auth",
                correlation_id=correlation_id,
            ) from exc

    @staticmethod
    async def _close_connection(connection) -> None:
        """发起连接关闭并等待 AsyncSSH 完成底层资源释放。"""

        connection.close()
        await connection.wait_closed()

    @staticmethod
    def _matches(active, candidate) -> bool:
        """比较 Host Key 算法、指纹和公钥正文是否全部一致。"""

        return (
            active.key_algorithm == candidate.key_algorithm
            and active.fingerprint_sha256 == candidate.fingerprint_sha256
            and active.public_key_openssh_b64 == candidate.public_key_openssh_b64
        )

    @staticmethod
    def _status(
        connection_id: UUID,
        state: str,
        correlation_id: UUID,
        *,
        session_id: UUID | None = None,
        error_code: str | None = None,
        recoverable: bool = False,
        candidate=None,
        trusted_fingerprint_sha256: str | None = None,
    ) -> ConnectionStatus:
        """集中构造字段完整且严格校验的公开连接状态。"""

        return ConnectionStatus(
            connection_id=connection_id,
            state=state,
            session_id=session_id,
            error_code=error_code,
            recoverable=recoverable,
            correlation_id=correlation_id,
            host_key_candidate=candidate,
            trusted_fingerprint_sha256=trusted_fingerprint_sha256,
        )

    async def _emit(self, status: ConnectionStatus) -> None:
        """在配置监听器时异步发布一条连接状态。"""

        if self._status_listener is not None:
            await self._status_listener(status)

    async def _emit_error(
        self, connection_id: UUID, error: SshRuntimeError
    ) -> None:
        """把结构化 SSH 异常转换为 FAILED 连接状态并发布。"""

        await self._emit(
            self._status(
                connection_id,
                "FAILED",
                error.correlation_id,
                error_code=error.error_code,
                recoverable=error.recoverable,
                candidate=error.candidate,
            )
        )
