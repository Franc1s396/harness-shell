"""AES-256-GCM helpers for canonical runtime records."""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class InvalidKeyLength(ValueError):
    """Raised when a runtime data key is not 256 bits."""


class RecordAuthenticationFailed(RuntimeError):
    """Raised when ciphertext or its associated identity was modified."""


def require_data_key(key: bytes | bytearray) -> None:
    if len(key) != 32:
        raise InvalidKeyLength("runtime data key must be exactly 32 bytes")


def record_aad(record_type: str, record_id: str, schema_version: int) -> bytes:
    return b"\x00".join(
        (
            b"harness-shell-record-v1",
            record_type.encode("utf-8"),
            record_id.encode("utf-8"),
            str(schema_version).encode("ascii"),
        )
    )


def encrypt_payload(
    key: bytes | bytearray, plaintext: bytes, aad: bytes
) -> tuple[bytes, bytes]:
    require_data_key(key)
    nonce = os.urandom(12)
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)
    return nonce, ciphertext


def decrypt_payload(
    key: bytes | bytearray, nonce: bytes, ciphertext: bytes, aad: bytes
) -> bytes:
    require_data_key(key)
    try:
        return AESGCM(bytes(key)).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise RecordAuthenticationFailed(
            "encrypted record authentication failed"
        ) from exc

