"""手动 SFTP handler 与服务共享的严格公共模型。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


JsSafeInt = Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
ChunkSequence = Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
EntryType = Literal["file", "directory", "symlink", "other"]


class StrictModel(BaseModel):
    """在每个手动 SFTP 边界拒绝未知字段和类型强制转换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ManualSftpContext(StrictModel):
    """描述显式绑定的活动 SSH 会话，不暴露秘密。"""

    ssh_session_id: UUID = Field(description="Bound live SSH session identifier.")
    connection_id: UUID = Field(description="Connection profile behind the session.")
    home: str = Field(description="Remote user's absolute home directory.")
    host_label: str = Field(description="Safe display label for the bound host.")
    sftp_version: Annotated[int, Field(ge=3, le=6, strict=True)] = Field(
        description="Negotiated SFTP protocol version."
    )


class RemoteEntry(StrictModel):
    """暴露可安全提供给 WebView 的不跟随链接远程元数据。"""

    name: str = Field(description="Single remote basename in the listed directory.")
    path: str = Field(description="Validated absolute POSIX path.")
    entry_type: EntryType = Field(description="No-follow remote entry kind.")
    size: JsSafeInt | None = Field(
        description="Regular-file bytes, or null when absent."
    )
    mode: Annotated[int, Field(ge=0, strict=True)] = Field(
        description="POSIX file type and permission bits."
    )
    mtime_ns: Annotated[str, Field(pattern=r"^[0-9]+$", strict=True)] | None = (
        Field(description="Exact uint64 nanoseconds encoded as decimal.")
    )
    link_target: str | None = Field(description="Explicit readlink result only.")


class ListingBatch(StrictModel):
    """从短生命周期列表游标返回有界分页。"""

    listing_id: UUID
    path: str
    entries: tuple[RemoteEntry, ...] = Field(max_length=200)
    next_sequence: ChunkSequence
    done: bool
    observed_entry_count: Annotated[int, Field(ge=0, le=50_000, strict=True)]
    complete: bool


class TransferSnapshot(StrictModel):
    """冻结远程目标，以便后续变更前比较校验。"""

    path: str
    exists: bool
    entry_type: EntryType | None
    size: JsSafeInt | None
    mtime_ns: Annotated[str, Field(pattern=r"^[0-9]+$", strict=True)] | None
    sha256: Sha256Hex | None


class RemoteFileHash(StrictModel):
    """返回关联已验证快照的完整远程文件哈希。"""

    path: str
    snapshot: TransferSnapshot
    sha256: Sha256Hex
    byte_count: JsSafeInt


class UploadReady(StrictModel):
    """描述可接收分块的独占远程上传临时文件。"""

    operation_id: UUID
    temp_path: str
    next_sequence: ChunkSequence
    next_offset: JsSafeInt


class UploadChunkAck(StrictModel):
    """确认且仅确认一个顺序上传分块。"""

    operation_id: UUID
    sequence: ChunkSequence
    offset: JsSafeInt
    accepted_bytes: Annotated[int, Field(ge=1, le=262_144, strict=True)]


class DownloadReady(StrictModel):
    """在返回任何字节前冻结远程下载源。"""

    operation_id: UUID
    path: str
    snapshot: TransferSnapshot
    sha256: Sha256Hex
    byte_count: JsSafeInt
    next_sequence: ChunkSequence
    next_offset: JsSafeInt


class DownloadChunk(StrictModel):
    """返回原始二进制下载分块和精确下一位置。"""

    operation_id: UUID
    sequence: ChunkSequence
    offset: JsSafeInt
    data: bytes
    next_offset: JsSafeInt
    eof: bool


class DeletePlanSummary(StrictModel):
    """仅暴露完整且不跟随链接的递归删除计划元数据。"""

    delete_plan_id: UUID
    operation_id: UUID
    root_path: str
    root_snapshot: TransferSnapshot
    file_count: Annotated[int, Field(ge=0, le=50_000, strict=True)]
    directory_count: Annotated[int, Field(ge=1, le=50_000, strict=True)]
    symlink_count: Annotated[int, Field(ge=0, le=50_000, strict=True)]
    total_byte_count: JsSafeInt
    manifest_sha256: Sha256Hex
    complete: Literal[True]


class OperationTerminalProjection(StrictModel):
    """返回可信终态结果或明确的不确定状态。"""

    operation_id: UUID
    state: Literal[
        "succeeded", "failed", "cancelled", "cleanup_required", "outcome_unknown"
    ]
    error_code: str | None
    message: str
    sha256: Sha256Hex | None
    byte_count: JsSafeInt | None
    recovery_id: UUID | None


class RecoverySummary(StrictModel):
    """暴露仅含远程状态的恢复记录安全摘要。"""

    recovery_id: UUID
    operation_id: UUID
    kind: Literal["upload_temp", "delete_tombstone", "mutation_unknown"]
    host_label: str
    remote_path: str | None
    display_name: str
    state: Literal["cleanup_required", "outcome_unknown", "recovery_required"]
    created_at: str
    available_actions: tuple[
        Literal[
            "verify",
            "delete_temp",
            "continue_delete",
            "restore_tombstone",
            "keep",
        ],
        ...,
    ]


class MutationProgressProjection(StrictModel):
    """发布安全的手动变更阶段，不含本地路径或原始错误。"""

    operation_id: UUID
    kind: Literal["mkdir", "rename", "remove", "recursive_delete", "recovery"]
    phase: Literal["preparing", "isolating", "deleting", "cleaning", "committing"]
    display_name: str
    remote_path: str
    host_label: str
    items_completed: JsSafeInt | None
    items_total: JsSafeInt | None
    cancellable: Literal[False]


class DeleteManifestEntry(StrictModel):
    """持久化不跟随链接的规范递归删除清单条目。"""

    path: str
    entry_type: EntryType
    size: JsSafeInt | None
    mode: Annotated[int, Field(ge=0, strict=True)]
    mtime_ns: Annotated[str, Field(pattern=r"^[0-9]+$", strict=True)] | None
    link_target: str | None
