"""公开标识与 SSH 秘密之间的凭据解析边界。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from harness_shell_sidecar.connections import (
    ConnectionProfile,
    ConnectionRepository,
)

from .cipher import zeroize
from .repository import CredentialRepositoryError, CredentialRepository
from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecordStore


class CredentialServiceError(RuntimeError):
    """暴露稳定凭据解析失败，不包含秘密文本。"""

    error_code: str

    def __init__(self, error_code: str, message: str) -> None:
        """保留稳定公开错误码与已审查非秘密详情。"""

        self.error_code = error_code
        self.safe_message = message
        super().__init__(f"{error_code}: {message}")


@dataclass(slots=True)
class ResolvedSshConnect:
    """拥有版本冻结的 SSH 请求及其全部临时秘密缓冲区。"""

    #: 直连连接标识。
    connection_id: UUID
    #: 凭据解析后再次检查的直连配置版本。
    profile_version: int
    #: 密码认证使用的密码字节。
    password: bytearray | None = None
    #: 密钥认证使用的已导入私钥 UTF-8 字节。
    private_key: bytearray | None = None
    #: 可选的直连私钥口令字节。
    passphrase: bytearray | None = None
    #: 可选的单层 ProxyJump 连接标识。
    jump_connection_id: UUID | None = None
    #: 可选的 ProxyJump 配置版本。
    jump_profile_version: int | None = None
    #: 可选的 ProxyJump 密码字节。
    jump_password: bytearray | None = None
    #: 可选的 ProxyJump 私钥字节。
    jump_private_key: bytearray | None = None
    #: 可选的 ProxyJump 私钥口令字节。
    jump_passphrase: bytearray | None = None
    #: 全部已分配秘密缓冲区，包括上述字段引用的缓冲区。
    _allocated: list[bytearray] = field(default_factory=list, repr=False)

    def close(self) -> None:
        """覆盖全部临时秘密缓冲区，允许安全重复调用。"""

        for secret in self._allocated:
            zeroize(secret)
        self._allocated.clear()


class CredentialService:
    """快照配置、解析精确凭据类型并拒绝竞态。"""


    def __init__(
        self,
        database: RuntimeDatabase,
    ) -> None:
        """绑定 Runtime 拥有的仓库，不接管生命周期。"""

        self._database = database  # 短 Session 的工厂，不保存秘密或 Session。

    def build_ssh_connect(self, connection_id: UUID) -> ResolvedSshConnect:
        """基于稳定配置版本解析直连与单层跳板凭据。"""

        # 1. 冻结目标和可选跳板配置，拒绝多层 ProxyJump。
        direct = self._required_profile(connection_id)
        jump = (
            None
            if direct.proxy_jump_id is None
            else self._required_profile(direct.proxy_jump_id)
        )
        if jump is not None and jump.proxy_jump_id is not None:
            raise CredentialServiceError(
                "MULTI_HOP_PROXY_FORBIDDEN",
                "the selected ProxyJump profile references another jump",
            )

        # 2. 建立临时秘密的统一所有者，再分别解析目标与跳板凭据。
        resolved = ResolvedSshConnect(
            connection_id=direct.connection_id,
            profile_version=direct.version,
            jump_connection_id=None if jump is None else jump.connection_id,
            jump_profile_version=None if jump is None else jump.version,
        )
        try:
            (
                resolved.password,
                resolved.private_key,
                resolved.passphrase,
            ) = self._resolve_profile(direct, resolved._allocated)
            if jump is not None:
                (
                    resolved.jump_password,
                    resolved.jump_private_key,
                    resolved.jump_passphrase,
                ) = self._resolve_profile(jump, resolved._allocated)
            # 3. 解析后复核两端配置版本，防止网络操作使用陈旧身份。
            self._require_same_version(direct)
            if jump is not None:
                self._require_same_version(jump)
            return resolved
        except CredentialServiceError:
            resolved.close()
            raise
        # 4. 失败路径清零已取得的全部秘密，再映射或传播原始失败。
        except CredentialRepositoryError as error:
            resolved.close()
            raise CredentialServiceError(
                error.error_code,
                error.safe_message,
            ) from None
        except BaseException:
            resolved.close()
            raise

    def _required_profile(self, connection_id: UUID) -> ConnectionProfile:
        """加载必需配置；不存在时返回稳定错误。"""

        with self._database.read_session() as session:
            profile = ConnectionRepository(session).get(connection_id)
        if profile is None:
            raise CredentialServiceError(
                "CONNECTION_NOT_FOUND",
                "the requested connection profile does not exist",
            )
        return profile

    def _resolve_profile(
        self,
        profile: ConnectionProfile,
        allocated: list[bytearray],
    ) -> tuple[bytearray | None, bytearray | None, bytearray | None]:
        """仅解析配置声明的精确凭据类型。"""

        if profile.auth_kind == "password":
            with self._database.read_session() as session:
                password = CredentialRepository(PlaintextRecordStore(session)).resolve(
                    profile.credential_id,
                    "ssh_password",
                )
            allocated.append(password)
            return password, None, None

        with self._database.read_session() as session:
            private_key = CredentialRepository(PlaintextRecordStore(session)).resolve(
                profile.credential_id,
                "imported_private_key",
            )
        allocated.append(private_key)
        passphrase = None
        if profile.passphrase_credential_id is not None:
            with self._database.read_session() as session:
                passphrase = CredentialRepository(PlaintextRecordStore(session)).resolve(
                    profile.passphrase_credential_id,
                    "private_key_passphrase",
                )
            allocated.append(passphrase)
        return None, private_key, passphrase

    def _require_same_version(self, snapshot: ConnectionProfile) -> None:
        """拒绝凭据解析后发生的删除或任何成功更新。"""

        with self._database.read_session() as session:
            current = ConnectionRepository(session).get(snapshot.connection_id)
        if current is None or current.version != snapshot.version:
            raise CredentialServiceError(
                "CONNECTION_PROFILE_CHANGED",
                "the connection profile changed while credentials were resolved",
            )


__all__ = [
    "CredentialService",
    "CredentialServiceError",
    "ResolvedSshConnect",
]
