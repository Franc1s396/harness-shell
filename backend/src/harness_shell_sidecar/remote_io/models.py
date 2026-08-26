"""Frozen M2 contracts for internal Agent read-only remote I/O."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ArtifactReference(FrozenModel):
    artifact_id: UUID
    sha256: Sha256Hex
    byte_count: Annotated[int, Field(ge=0)]
    media_type: str
    sensitivity: Literal["normal", "sensitive"]
    encrypted: Literal[True] = True
    complete: bool


class RemoteExecRequest(FrozenModel):
    operation_id: UUID
    ssh_session_id: UUID
    command: Annotated[str, StringConstraints(min_length=1)]
    timeout_ms: Annotated[int, Field(ge=1, le=60_000)]
    output_budget_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)]


class RemoteExecResult(FrozenModel):
    operation_id: UUID
    channel_id: UUID
    exit_status: int | None
    exit_signal: str | None
    stdout_artifact: ArtifactReference
    stderr_artifact: ArtifactReference
    byte_count: Annotated[int, Field(ge=0)]
    truncated: bool
    timeout: bool
    cancelled: bool


class RemoteStat(FrozenModel):
    path: str
    size: Annotated[int, Field(ge=0)]
    mode: Annotated[int, Field(ge=0)]
    mtime_ns: int | None
    is_file: bool
    is_dir: bool


class RemoteListResult(FrozenModel):
    path: str
    entries: tuple[RemoteStat, ...]
    truncated: bool


class RemoteReadRangeResult(FrozenModel):
    path: str
    offset: Annotated[int, Field(ge=0)]
    requested_length: Annotated[int, Field(ge=1, le=262_144)]
    artifact: ArtifactReference
    eof: bool


class RemoteHashResult(FrozenModel):
    path: str
    sha256: Sha256Hex
    byte_count: Annotated[int, Field(ge=0)]
