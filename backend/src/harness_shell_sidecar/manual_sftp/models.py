"""Strict public models shared by manual SFTP handlers and services."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


JsSafeInt = Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
ChunkSequence = Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
EntryType = Literal["file", "directory", "symlink", "other"]


class StrictModel(BaseModel):
    """Reject unknown fields and coercion at every manual-SFTP boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ManualSftpContext(StrictModel):
    """Describe one explicitly bound live SSH session without exposing secrets."""

    ssh_session_id: UUID = Field(description="Bound live SSH session identifier.")
    connection_id: UUID = Field(description="Connection profile behind the session.")
    home: str = Field(description="Remote user's absolute home directory.")
    host_label: str = Field(description="Safe display label for the bound host.")
    sftp_version: Annotated[int, Field(ge=3, le=6, strict=True)] = Field(
        description="Negotiated SFTP protocol version."
    )


class RemoteEntry(StrictModel):
    """Expose no-follow remote metadata safe for the WebView."""

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
    """Return one bounded page from a short-lived listing cursor."""

    listing_id: UUID
    path: str
    entries: tuple[RemoteEntry, ...] = Field(max_length=200)
    next_sequence: ChunkSequence
    done: bool
    observed_entry_count: Annotated[int, Field(ge=0, le=50_000, strict=True)]
    complete: bool


class TransferSnapshot(StrictModel):
    """Freeze one remote target for later compare-before-mutate checks."""

    path: str
    exists: bool
    entry_type: EntryType | None
    size: JsSafeInt | None
    mtime_ns: Annotated[str, Field(pattern=r"^[0-9]+$", strict=True)] | None
    sha256: Sha256Hex | None


class RemoteFileHash(StrictModel):
    """Return a complete remote-file hash tied to a verified snapshot."""

    path: str
    snapshot: TransferSnapshot
    sha256: Sha256Hex
    byte_count: JsSafeInt


class UploadReady(StrictModel):
    """Describe an exclusive remote upload temporary file ready for chunks."""

    operation_id: UUID
    temp_path: str
    next_sequence: ChunkSequence
    next_offset: JsSafeInt


class UploadChunkAck(StrictModel):
    """Acknowledge exactly one sequential upload chunk."""

    operation_id: UUID
    sequence: ChunkSequence
    offset: JsSafeInt
    accepted_bytes: Annotated[int, Field(ge=1, le=262_144, strict=True)]


class DownloadReady(StrictModel):
    """Freeze one remote download source before returning any bytes."""

    operation_id: UUID
    path: str
    snapshot: TransferSnapshot
    sha256: Sha256Hex
    byte_count: JsSafeInt
    next_sequence: ChunkSequence
    next_offset: JsSafeInt


class DownloadChunk(StrictModel):
    """Return one raw binary download chunk and exact next position."""

    operation_id: UUID
    sequence: ChunkSequence
    offset: JsSafeInt
    data: bytes
    next_offset: JsSafeInt
    eof: bool


class DeletePlanSummary(StrictModel):
    """Expose only complete no-follow recursive-delete plan metadata."""

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
    """Return one trustworthy terminal result or explicit uncertainty."""

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
    """Expose a safe summary of one encrypted recovery record."""

    recovery_id: UUID
    operation_id: UUID
    kind: Literal[
        "upload_temp", "download_part", "delete_tombstone", "mutation_unknown"
    ]
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
            "open_local_folder",
            "keep",
        ],
        ...,
    ]


class MutationProgressProjection(StrictModel):
    """Publish a safe manual mutation phase without local paths or raw errors."""

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
    """Persist one no-follow canonical recursive-delete manifest entry."""

    path: str
    entry_type: EntryType
    size: JsSafeInt | None
    mode: Annotated[int, Field(ge=0, strict=True)]
    mtime_ns: Annotated[str, Field(pattern=r"^[0-9]+$", strict=True)] | None
    link_target: str | None
