"""Typed runtime messages carried inside protocol envelopes."""

from __future__ import annotations

import base64
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class RuntimeInitializationFailure(RuntimeError):
    def __init__(self, error_code: str, public_message: str) -> None:
        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("runtime error code must use uppercase identifiers")
        super().__init__(public_message)
        self.error_code = error_code
        self.public_message = public_message


class RuntimePhase(StrEnum):
    STARTING = "STARTING"
    HANDSHAKING = "HANDSHAKING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class InitializeRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    method: Literal["initialize"]
    app_version: str
    runtime_db_path: Path
    runtime_data_key_b64: str
    audit_hmac_key_b64: str
    heartbeat_interval_ms: Literal[5000]
    heartbeat_timeout_ms: Literal[15000]

    @field_validator("runtime_db_path")
    @classmethod
    def require_absolute_runtime_database(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("runtime database path must be absolute")
        return value

    @field_validator("runtime_data_key_b64", "audit_hmac_key_b64")
    @classmethod
    def require_256_bit_key(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("runtime key must be valid base64") from exc
        if len(decoded) != 32:
            raise ValueError("runtime key must decode to exactly 32 bytes")
        return value

    def runtime_data_key(self) -> bytes:
        return base64.b64decode(self.runtime_data_key_b64, validate=True)

    def audit_hmac_key(self) -> bytes:
        return base64.b64decode(self.audit_hmac_key_b64, validate=True)


class RuntimeCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_versions: tuple[Literal[1], ...] = (1,)
    storage_schema_version: Literal[1] = 1
    features: tuple[str, ...] = (
        "encrypted_records",
        "audit_chain",
        "local_trace",
    )
