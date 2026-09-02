"""Transport-independent runtime lifecycle models."""

from __future__ import annotations

import base64
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator


# Shared encoded JSON boundary used by the HTTP request, response, and Agent
# result preflight paths. Keeping one value prevents transport-layer drift.
MAX_JSON_BODY_BYTES = 1_048_576
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class RuntimeInitializationFailure(RuntimeError):
    """Expose one validated, non-secret initialization failure."""

    def __init__(self, error_code: str, public_message: str) -> None:
        """Retain only a stable code and an explicitly public message."""

        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("runtime error code must use uppercase identifiers")
        super().__init__(public_message)
        self.error_code = error_code  # Stable HTTP-facing identifier.
        self.public_message = public_message  # Safe bounded failure text.


class RuntimePhase(StrEnum):
    """Describe the single runtime owner from live startup through cleanup."""

    #: Process is live, but the unique runtime resource graph does not exist yet.
    LIVE_NOT_INITIALIZED = "LIVE_NOT_INITIALIZED"
    #: Runtime resources are being opened and verified atomically.
    INITIALIZING = "INITIALIZING"
    #: All resources are valid and application operations may be dispatched.
    READY = "READY"
    #: New requests are rejected while active dispatcher work is cancelled.
    DRAINING = "DRAINING"
    #: Domain owners are converging and closing their remote resources.
    CONVERGING = "CONVERGING"
    #: Observability, keys, and local persistence are being closed.
    CLOSING = "CLOSING"
    #: The complete runtime graph has been released successfully.
    STOPPED = "STOPPED"
    #: Initialization or convergence failed and the runtime cannot be reused.
    FAILED = "FAILED"

class RuntimeInitializeRequest(BaseModel):
    """Strict shared input for constructing the one runtime resource graph."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Desktop application version associated with this runtime instance.
    app_version: str
    #: Absolute SQLite database path selected by the trusted desktop core.
    runtime_db_path: Path
    #: Base64-encoded 256-bit authenticated-record encryption key.
    runtime_data_key_b64: str
    #: Base64-encoded independent 256-bit audit HMAC key.
    audit_hmac_key_b64: str
    #: Fixed desktop WebSocket heartbeat interval.
    heartbeat_interval_ms: int
    #: Fixed desktop WebSocket heartbeat timeout.
    heartbeat_timeout_ms: int

    @field_validator("runtime_db_path", mode="before")
    @classmethod
    def parse_runtime_database_path(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        """Preserve JSON strings and adapt Python-mode strings to domain Paths."""

        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return value if info.mode == "json" else Path(value)
        raise ValueError("runtime database path must be a string")

    @field_validator("runtime_db_path")
    @classmethod
    def require_absolute_runtime_database(cls, value: Path) -> Path:
        """Reject paths whose meaning depends on the process working directory."""

        if not value.is_absolute():
            raise ValueError("runtime database path must be absolute")
        return value

    @field_validator("runtime_data_key_b64", "audit_hmac_key_b64")
    @classmethod
    def require_256_bit_key(cls, value: str) -> str:
        """Require canonical Base64 text that decodes to exactly 32 bytes."""

        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("runtime key must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError("runtime key must decode to exactly 32 bytes")
        return value

    @field_validator("heartbeat_interval_ms")
    @classmethod
    def require_heartbeat_interval(cls, value: int) -> int:
        """Keep the private WebSocket heartbeat interval contract exact."""

        if value != 5_000:
            raise ValueError("heartbeat interval must be 5000 milliseconds")
        return value

    @field_validator("heartbeat_timeout_ms")
    @classmethod
    def require_heartbeat_timeout(cls, value: int) -> int:
        """Keep the private WebSocket heartbeat timeout contract exact."""

        if value != 15_000:
            raise ValueError("heartbeat timeout must be 15000 milliseconds")
        return value

    def runtime_data_key(self) -> bytes:
        """Decode the authenticated-record encryption key."""

        return base64.b64decode(self.runtime_data_key_b64, validate=True)

    def audit_hmac_key(self) -> bytes:
        """Decode the independent audit HMAC key."""

        return base64.b64decode(self.audit_hmac_key_b64, validate=True)
