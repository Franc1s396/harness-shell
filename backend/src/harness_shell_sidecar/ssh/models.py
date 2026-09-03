"""Strict public identity request models for SSH operations."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class HostKeyInspectionRequest(BaseModel):
    """Locate one profile whose direct or single-hop Host Key is inspected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Target connection profile identity.
    connection_id: UUID


class SshConnectRequest(BaseModel):
    """Locate one profile whose credentials Python resolves internally."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Target connection profile identity.
    connection_id: UUID


class SshSessionRequest(BaseModel):
    """Locate one active SSH session."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Active SSH session identity.
    ssh_session_id: UUID
