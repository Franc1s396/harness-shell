"""SSH 操作的严格公开标识请求模型。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class HostKeyInspectionRequest(BaseModel):
    """定位需要检查直连或单层跳板 Host Key 的配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 目标连接配置标识。
    connection_id: UUID


class SshConnectRequest(BaseModel):
    """定位由 Python 内部解析凭据的配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 目标连接配置标识。
    connection_id: UUID


class SshSessionRequest(BaseModel):
    """定位活动 SSH 会话。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: 活动 SSH 会话标识。
    ssh_session_id: UUID
