"""Strict M2 connection and host-key contracts."""

from __future__ import annotations

import base64
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


NonBlank80 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]
NonBlank128 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
NonBlank255 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]


class ConnectionProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: NonBlank80
    group_name: NonBlank80 | None = None
    host: NonBlank255
    port: Annotated[int, Field(ge=1, le=65_535)] = 22
    username: NonBlank128
    auth_kind: Literal["password", "private_key"]
    credential_id: UUID
    passphrase_credential_id: UUID | None = None
    proxy_jump_id: UUID | None = None
    favorite: bool = False

    @model_validator(mode="after")
    def validate_authentication_fields(self) -> ConnectionProfileInput:
        if self.auth_kind == "password" and self.passphrase_credential_id is not None:
            raise ValueError("password authentication cannot use a private-key passphrase")
        return self


class ConnectionProfile(ConnectionProfileInput):
    connection_id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def reject_self_proxy_jump(self) -> ConnectionProfile:
        if self.proxy_jump_id == self.connection_id:
            raise ValueError("connection cannot use itself as a proxy jump")
        return self


class HostKeyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID
    host: NonBlank255
    port: Annotated[int, Field(ge=1, le=65_535)]
    key_algorithm: NonBlank128
    fingerprint_sha256: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^SHA256:[A-Za-z0-9+/=_-]+$")
    ]
    public_key_openssh_b64: str

    @field_validator("public_key_openssh_b64")
    @classmethod
    def require_canonical_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("public key must be valid base64") from exc
        if not decoded or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("public key must use canonical base64")
        return value

    def public_key_openssh(self) -> bytes:
        return base64.b64decode(self.public_key_openssh_b64, validate=True)


class HostKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    host_key_id: UUID
    connection_id: UUID
    key_algorithm: NonBlank128
    fingerprint_sha256: str
    public_key_openssh_b64: str
    status: Literal["active", "replaced"]
    confirmed_at: AwareDatetime
    replaced_at: AwareDatetime | None = None
