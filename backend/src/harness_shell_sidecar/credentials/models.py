"""Strict public credential wire and repository identity models."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


CredentialKind = Literal[
    "ssh_password",
    "private_key_passphrase",
    "imported_private_key",
    "api_key",
]


class StrictCredentialModel(BaseModel):
    """Forbid unknown fields and implicit coercion on credential boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CredentialEnvelope(StrictCredentialModel):
    """Carry one RSA-wrapped AES-GCM credential mutation payload."""

    version: Literal[1] = Field(description="Credential envelope protocol version.")
    key_id: UUID = Field(description="Ephemeral Runtime RSA key identity.")
    wrapped_key_b64: str = Field(description="Canonical Base64 RSA-OAEP ciphertext.")
    iv_b64: str = Field(description="Canonical Base64 96-bit AES-GCM IV.")
    ciphertext_b64: str = Field(
        description="Canonical Base64 AES-GCM ciphertext and authentication tag."
    )


class CredentialPublicKey(StrictCredentialModel):
    """Expose only the current Runtime RSA public encryption material."""

    version: Literal[1] = Field(description="Credential envelope protocol version.")
    scheme: Literal["RSA-OAEP-256+A256GCM"] = Field(
        description="Required hybrid credential encryption scheme."
    )
    key_id: UUID = Field(description="Ephemeral Runtime RSA key identity.")
    public_key_spki_b64: str = Field(
        description="Canonical Base64 DER SubjectPublicKeyInfo bytes."
    )
