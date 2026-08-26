"""Safe SSH runtime errors and public connection status models."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from harness_shell_sidecar.connections import HostKeyCandidate


RemoteState = Literal[
    "not_contacted", "pre_auth", "authenticated", "channel_dispatched", "unknown"
]


class ConnectionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID
    state: Literal[
        "DISCONNECTED",
        "CONNECTING",
        "HOST_KEY_REQUIRED",
        "READY",
        "CLOSING",
        "FAILED",
    ]
    session_id: UUID | None
    error_code: str | None
    recoverable: bool
    correlation_id: UUID
    host_key_candidate: HostKeyCandidate | None
    trusted_fingerprint_sha256: str | None = None


class SshRuntimeError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        *,
        node: str,
        recoverable: bool,
        remote_state: RemoteState,
        correlation_id: UUID | None = None,
        candidate: HostKeyCandidate | None = None,
        trusted_fingerprint_sha256: str | None = None,
    ) -> None:
        super().__init__("SSH operation failed")
        self.error_code = error_code
        self.node = node
        self.recoverable = recoverable
        self.remote_state = remote_state
        self.correlation_id = correlation_id or uuid4()
        self.candidate = candidate
        self.trusted_fingerprint_sha256 = trusted_fingerprint_sha256

    def public_payload(self) -> dict:
        payload: dict = {
            "error_code": self.error_code,
            "node": self.node,
            "recoverable": self.recoverable,
            "correlation_id": str(self.correlation_id),
            "remote_state": self.remote_state,
        }
        if self.candidate is not None:
            payload["host_key_candidate"] = self.candidate.model_dump(mode="json")
        if self.trusted_fingerprint_sha256 is not None:
            payload["trusted_fingerprint_sha256"] = self.trusted_fingerprint_sha256
        return payload
