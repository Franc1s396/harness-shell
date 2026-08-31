"""Typed runtime messages carried inside protocol envelopes."""

from __future__ import annotations

import base64
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class RuntimeInitializationFailure(RuntimeError):
    """允许初始化组件安全公开稳定错误码与脱敏消息的异常。"""

    def __init__(self, error_code: str, public_message: str) -> None:
        """校验错误码格式并保存可返回给桌面端的公开信息。"""

        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("runtime error code must use uppercase identifiers")
        super().__init__(public_message)
        self.error_code = error_code  # 协议响应使用的全大写稳定标识符。
        self.public_message = public_message  # 已确认不包含内部或敏感细节的消息。


class RuntimePhase(StrEnum):
    """Sidecar 从启动、握手到终止的有限状态机阶段。"""

    #: 进程刚启动，尚未发布 ready 事件。
    STARTING = "STARTING"
    #: 已发布能力，等待唯一的 initialize 请求。
    HANDSHAKING = "HANDSHAKING"
    #: 初始化完成，可接受应用请求和心跳。
    READY = "READY"
    #: 已接受 shutdown，正在释放资源。
    STOPPING = "STOPPING"
    #: 资源已释放，运行循环正常结束。
    STOPPED = "STOPPED"
    #: 出现终止性错误，必须 fail closed。
    FAILED = "FAILED"


class InitializeRequestPayload(BaseModel):
    """桌面控制面初始化 Sidecar 时发送的敏感配置负载。"""

    #: 禁止未声明字段和隐式类型转换。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 初始化方法判别字段，固定为 initialize。
    method: Literal["initialize"]
    #: 发起握手的桌面应用版本。
    app_version: str
    #: 当前运行实例使用的 SQLite 数据库绝对路径。
    runtime_db_path: Path
    #: 用于加密运行时记录的 256-bit Key 的 Base64 文本。
    runtime_data_key_b64: str
    #: 用于签名审计链的独立 256-bit HMAC Key 的 Base64 文本。
    audit_hmac_key_b64: str
    #: 桌面端发送心跳的固定间隔（毫秒）。
    heartbeat_interval_ms: Literal[5000]
    #: 判定 Sidecar 心跳超时的固定时长（毫秒）。
    heartbeat_timeout_ms: Literal[15000]

    @field_validator("runtime_db_path")
    @classmethod
    def require_absolute_runtime_database(cls, value: Path) -> Path:
        """拒绝依赖进程工作目录解析的相对数据库路径。"""

        if not value.is_absolute():
            raise ValueError("runtime database path must be absolute")
        return value

    @field_validator("runtime_data_key_b64", "audit_hmac_key_b64")
    @classmethod
    def require_256_bit_key(cls, value: str) -> str:
        """确保运行时 Key 是合法 Base64 且精确解码为 32 字节。"""

        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("runtime key must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError("runtime key must decode to exactly 32 bytes")
        return value

    def runtime_data_key(self) -> bytes:
        """解码并返回数据库记录加密 Key。"""

        return base64.b64decode(self.runtime_data_key_b64, validate=True)

    def audit_hmac_key(self) -> bytes:
        """解码并返回审计链签名 Key。"""

        return base64.b64decode(self.audit_hmac_key_b64, validate=True)


class RuntimeCapabilities(BaseModel):
    """Sidecar 在握手 ready 事件中公布的固定能力集合。"""

    #: 对能力模型执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 当前能够处理的协议版本列表。
    protocol_versions: tuple[Literal[1], ...] = (1,)
    #: 当前 SQLite 运行时存储 Schema 版本。
    storage_schema_version: Literal[4] = 4
    #: 供桌面端进行能力协商的稳定功能名称。
    features: tuple[str, ...] = (
        "encrypted_records",
        "audit_chain",
        "local_trace",
        "connection_profiles",
        "host_key_store",
        "ssh_runtime",
        "pty",
        "manual_sftp",
        "react_shell_agent",
    )
