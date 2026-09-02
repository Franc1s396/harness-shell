"""Strict transport-independent request models for SSH operations."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProfileVersion = Annotated[int, Field(ge=1, le=2**53 - 1, strict=True)]


class SshAuthenticationRequest(BaseModel):
    """Carry exactly one authentication secret without exposing it in repr."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Canonical Base64 password bytes.
    password_b64: str | None = Field(default=None, repr=False)
    #: Canonical Base64 private-key bytes.
    private_key_b64: str | None = Field(default=None, repr=False)
    #: Canonical Base64 private-key passphrase bytes.
    passphrase_b64: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_one_authentication_kind(self) -> SshAuthenticationRequest:
        """Require password xor private key and bind passphrase to a key."""

        if (self.password_b64 is None) == (self.private_key_b64 is None):
            raise ValueError("exactly one authentication secret is required")
        if self.private_key_b64 is None and self.passphrase_b64 is not None:
            raise ValueError("passphrase requires a private key")
        return self


class SshJumpRequest(SshAuthenticationRequest):
    """Freeze one ProxyJump profile version with its transient credential."""

    #: Jump connection profile identity.
    connection_id: UUID
    #: JS-safe monotonic version observed before credential resolution.
    profile_version: ProfileVersion


class HostKeyInspectionRequest(BaseModel):
    """Describe one direct or single-hop Host Key inspection."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Target connection profile identity.
    connection_id: UUID
    #: Optional single ProxyJump request.
    jump: SshJumpRequest | None = None


class SshConnectRequest(SshAuthenticationRequest):
    """Freeze target and optional jump profiles with transient credentials."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Target connection profile identity.
    connection_id: UUID
    #: JS-safe monotonic version observed before credential resolution.
    profile_version: ProfileVersion
    #: Optional single ProxyJump request.
    jump: SshJumpRequest | None = None


class SshSessionRequest(BaseModel):
    """Locate one active SSH session."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Active SSH session identity.
    ssh_session_id: UUID


class SshSessionSnapshot(BaseModel):
    """Expose only stable non-secret metadata for one connected SSH session."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    #: Opaque identity used by PTY, SFTP, and Agent operations.
    ssh_session_id: UUID
    #: Persisted connection profile identity used to create the session.
    connection_id: UUID
    #: Monotonic profile version frozen before network I/O.
    connection_profile_version: ProfileVersion
    #: Safe user-selected connection label.
    host_label: str
    #: Verified target Host Key fingerprint.
    target_host_key_fingerprint: str
    #: Optional ProxyJump connection profile identity.
    jump_connection_id: UUID | None
    #: Optional ProxyJump profile version frozen before network I/O.
    jump_profile_version: ProfileVersion | None
    #: Optional verified ProxyJump Host Key fingerprint.
    jump_host_key_fingerprint: str | None
