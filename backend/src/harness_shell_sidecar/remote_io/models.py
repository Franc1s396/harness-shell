"""Frozen M2 contracts for internal Agent read-only remote I/O."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    """远端只读 I/O 数据契约共用的严格不可变基类。"""

    #: 禁止额外字段、隐式转换和模型原地修改。
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ArtifactReference(FrozenModel):
    """指向本地加密 Artifact 的完整性与展示元数据。"""

    #: Artifact 的唯一标识符。
    artifact_id: UUID
    #: 明文内容的 SHA-256 十六进制摘要。
    sha256: Sha256Hex
    #: 明文内容的字节数。
    byte_count: Annotated[int, Field(ge=0)]
    #: Artifact 内容的 MIME 类型。
    media_type: str
    #: 内容是否需要按敏感数据限制访问。
    sensitivity: Literal["normal", "sensitive"]
    #: 明文是否始终以加密记录形式持久化；协议中固定为真。
    encrypted: Literal[True] = True
    #: 内容是否完整；超时、取消或截断时为假。
    complete: bool


class RemoteExecRequest(FrozenModel):
    """一次有超时与输出预算约束的远端命令执行请求。"""

    #: 供审计和幂等边界使用的操作标识符。
    operation_id: UUID
    #: 承载命令的已验证 SSH 会话标识符。
    ssh_session_id: UUID
    #: 交给远端 shell 执行的非空命令文本。
    command: Annotated[str, StringConstraints(min_length=1)]
    #: 操作允许占用的最长时间（毫秒）。
    timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    #: stdout 与 stderr 合计允许返回的字节预算。
    output_budget_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)]


class RemoteExecResult(FrozenModel):
    """远端命令执行状态及其加密输出 Artifact 引用。"""

    #: 与请求对应的操作标识符。
    operation_id: UUID
    #: 本次独立 exec channel 的标识符。
    channel_id: UUID
    #: 进程退出码；被信号终止或未启动完成时可为空。
    exit_status: int | None
    #: 终止进程的远端信号名称；正常退出时为空。
    exit_signal: str | None
    #: 捕获的 stdout 加密 Artifact。
    stdout_artifact: ArtifactReference
    #: 捕获的 stderr 加密 Artifact。
    stderr_artifact: ArtifactReference
    #: 两个输出流实际观测到的总字节数。
    byte_count: Annotated[int, Field(ge=0)]
    #: 输出是否因预算、硬上限、超时或取消而不完整。
    truncated: bool
    #: 操作是否因超过 deadline 而停止。
    timeout: bool
    #: 操作是否由调用方取消。
    cancelled: bool


class RemoteStat(FrozenModel):
    """远端路径经 lstat 得到的最小只读元数据。"""

    #: 被查询的绝对 POSIX 路径。
    path: str
    #: 文件大小；远端未提供时归一化为 0。
    size: Annotated[int, Field(ge=0)]
    #: POSIX 文件类型与权限位。
    mode: Annotated[int, Field(ge=0)]
    #: 纳秒级修改时间；服务端缺失时为空。
    mtime_ns: int | None
    #: 路径是否为普通文件。
    is_file: bool
    #: 路径是否为目录。
    is_dir: bool


class RemoteListResult(FrozenModel):
    """一次有条目上限的远端目录枚举结果。"""

    #: 被枚举的绝对目录路径。
    path: str
    #: 在预算内返回的目录项元数据。
    entries: tuple[RemoteStat, ...]
    #: 是否因条目上限而仍有结果未返回。
    truncated: bool


class RemoteReadRangeResult(FrozenModel):
    """远端普通文件指定区间的加密读取结果。"""

    #: 被读取的绝对文件路径。
    path: str
    #: 本次读取的起始字节偏移量。
    offset: Annotated[int, Field(ge=0)]
    #: 调用方请求读取的最大字节数。
    requested_length: Annotated[int, Field(ge=1, le=262_144)]
    #: 实际读取内容对应的加密 Artifact。
    artifact: ArtifactReference
    #: 本次读取是否已经到达文件末尾。
    eof: bool


class RemoteHashResult(FrozenModel):
    """远端普通文件的流式 SHA-256 计算结果。"""

    #: 被计算摘要的绝对文件路径。
    path: str
    #: 完整文件内容的 SHA-256 十六进制摘要。
    sha256: Sha256Hex
    #: 参与摘要计算的总字节数。
    byte_count: Annotated[int, Field(ge=0)]
