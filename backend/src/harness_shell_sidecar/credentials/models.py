"""严格的公开凭据传输与仓库标识模型。"""

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
    """在凭据边界拒绝未知字段和隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CredentialEnvelope(StrictCredentialModel):
    """携带 RSA 包装的 AES-GCM 凭据变更载荷。"""

    version: Literal[1] = Field(description="Credential envelope protocol version.")
    key_id: UUID = Field(description="Ephemeral Runtime RSA key identity.")
    wrapped_key_b64: str = Field(description="Canonical Base64 RSA-OAEP ciphertext.")
    iv_b64: str = Field(description="Canonical Base64 96-bit AES-GCM IV.")
    ciphertext_b64: str = Field(
        description="Canonical Base64 AES-GCM ciphertext and authentication tag."
    )


class CredentialPublicKey(StrictCredentialModel):
    """仅暴露当前 Runtime RSA 公共加密材料。"""

    version: Literal[1] = Field(description="Credential envelope protocol version.")
    scheme: Literal["RSA-OAEP-256+A256GCM"] = Field(
        description="Required hybrid credential encryption scheme."
    )
    key_id: UUID = Field(description="Ephemeral Runtime RSA key identity.")
    public_key_spki_b64: str = Field(
        description="Canonical Base64 DER SubjectPublicKeyInfo bytes."
    )
