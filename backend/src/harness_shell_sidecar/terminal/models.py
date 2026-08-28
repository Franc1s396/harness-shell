"""Strict contracts for interactive SSH PTY sessions."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PtyCols = Annotated[int, Field(ge=20, le=500)]
PtyRows = Annotated[int, Field(ge=5, le=300)]


class PtySession(BaseModel):
    """对桌面端公开的交互式 SSH PTY 会话快照。"""

    #: 禁止额外字段和类型隐式转换。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 独立于 SSH 主会话的 PTY 标识符。
    pty_session_id: UUID
    #: 承载该 PTY channel 的 SSH 会话标识符。
    ssh_session_id: UUID
    #: 对应的持久化连接配置标识符。
    connection_id: UUID
    #: 当前终端列数。
    cols: PtyCols
    #: 当前终端行数。
    rows: PtyRows
    #: PTY channel 的有限生命周期状态。
    state: Literal["OPEN", "CLOSED", "FAILED"]
