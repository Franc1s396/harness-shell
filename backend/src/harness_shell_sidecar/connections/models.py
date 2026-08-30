"""Strict M2 connection and host-key contracts."""

from __future__ import annotations

import base64
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


NonBlank80 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]
NonBlank128 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
NonBlank255 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
JsSafeProfileVersion = Annotated[
    int, Field(ge=1, le=2**53 - 1, strict=True)
]


class ConnectionProfileInput(BaseModel):
    """创建或更新 SSH 连接配置时由调用方提供的字段。"""

    #: 拒绝未声明字段，并关闭 Pydantic 的隐式类型转换。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 连接在 UI 中显示的名称。
    display_name: NonBlank80
    #: 可选的连接分组名称。
    group_name: NonBlank80 | None = None
    #: SSH 服务器主机名或 IP 地址。
    host: NonBlank255
    #: SSH 服务端口，默认使用 22。
    port: Annotated[int, Field(ge=1, le=65_535)] = 22
    #: 登录 SSH 服务器时使用的用户名。
    username: NonBlank128
    #: 身份验证方式；仅允许密码或私钥。
    auth_kind: Literal["password", "private_key"]
    #: 指向凭据存储中密码或私钥的标识符。
    credential_id: UUID
    #: 私钥口令对应的凭据标识符；密码认证时必须为空。
    passphrase_credential_id: UUID | None = None
    #: 可选的 ProxyJump 连接配置标识符。
    proxy_jump_id: UUID | None = None
    #: 是否在 UI 中将该连接标记为收藏。
    favorite: bool = False

    @model_validator(mode="after")
    def validate_authentication_fields(self) -> ConnectionProfileInput:
        """校验认证类型与私钥口令字段之间的组合约束。"""

        if self.auth_kind == "password" and self.passphrase_credential_id is not None:
            raise ValueError("password authentication cannot use a private-key passphrase")
        return self


class ConnectionProfile(ConnectionProfileInput):
    """已持久化并带有身份与审计时间的 SSH 连接配置。"""

    #: 连接配置的稳定唯一标识符。
    connection_id: UUID
    #: 连接配置首次创建的时间。
    created_at: AwareDatetime
    #: 连接配置最近一次更新的时间。
    updated_at: AwareDatetime
    #: 由 SQLite 在每次成功更新时单调递增的并发版本号。
    version: JsSafeProfileVersion

    @model_validator(mode="after")
    def reject_self_proxy_jump(self) -> ConnectionProfile:
        """禁止连接把自身配置为 ProxyJump 跳板。"""

        if self.proxy_jump_id == self.connection_id:
            raise ValueError("connection cannot use itself as a proxy jump")
        return self


class HostKeyCandidate(BaseModel):
    """等待用户确认或替换的远端 SSH Host Key 候选值。"""

    #: 使用严格模式校验跨进程输入，避免字段静默丢失或类型强转。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 该 Host Key 所属的连接配置标识符。
    connection_id: UUID
    #: 采集 Host Key 时实际访问的主机名或 IP 地址。
    host: NonBlank255
    #: 采集 Host Key 时实际访问的 SSH 端口。
    port: Annotated[int, Field(ge=1, le=65_535)]
    #: SSH 公钥算法名称，例如 ssh-ed25519。
    key_algorithm: NonBlank128
    #: 供用户核对的 SHA-256 Host Key 指纹。
    fingerprint_sha256: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^SHA256:[A-Za-z0-9+/=_-]+$")
    ]
    #: OpenSSH 公钥文本经过标准 Base64 编码后的持久化值。
    public_key_openssh_b64: str

    @field_validator("public_key_openssh_b64")
    @classmethod
    def require_canonical_base64(cls, value: str) -> str:
        """确保公钥字段使用非空且唯一表示的标准 Base64 编码。"""

        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("public key must be valid base64") from exc
        if not decoded or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("public key must use canonical base64")
        return value

    def public_key_openssh(self) -> bytes:
        """解码并返回原始 OpenSSH 公钥文本字节。"""

        return base64.b64decode(self.public_key_openssh_b64, validate=True)


class HostKeyRecord(BaseModel):
    """连接已确认的 Host Key 历史记录。"""

    #: 对持久化记录执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: Host Key 记录的唯一标识符。
    host_key_id: UUID
    #: 记录所属的连接配置标识符。
    connection_id: UUID
    #: SSH 公钥算法名称。
    key_algorithm: NonBlank128
    #: 已确认公钥的 SHA-256 指纹。
    fingerprint_sha256: str
    #: 已确认 OpenSSH 公钥文本的标准 Base64 编码。
    public_key_openssh_b64: str
    #: 记录当前生效或已被新记录替换的状态。
    status: Literal["active", "replaced"]
    #: 用户确认该 Host Key 的时间。
    confirmed_at: AwareDatetime
    #: 该记录被替换的时间；仍生效时为空。
    replaced_at: AwareDatetime | None = None
