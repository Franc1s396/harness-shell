"""Ephemeral RSA-OAEP and AES-GCM credential envelope decryption."""

from __future__ import annotations

import base64
import binascii
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import CredentialEnvelope, CredentialPublicKey


MAX_CREDENTIAL_PLAINTEXT_BYTES = 1_048_576
_AES_KEY_BYTES = 32
_AES_GCM_IV_BYTES = 12
_AES_GCM_TAG_BYTES = 16


class CredentialCipherError(ValueError):
    """Report a stable safe credential envelope failure code."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        """Store only the stable code and omit cryptographic exception text."""

        self.error_code = error_code
        super().__init__(error_code)


class RuntimeCredentialCipher:
    """Own one process-ephemeral RSA private key and its public identity."""

    _key_id: UUID
    _private_key: rsa.RSAPrivateKey

    def __init__(self, key_id: UUID, private_key: rsa.RSAPrivateKey) -> None:
        """Take exclusive ownership of one generated Runtime private key."""

        self._key_id = key_id  # Changes on every Runtime process start.
        # Never persisted, logged, returned, or transferred outside Python memory.
        self._private_key = private_key

    @classmethod
    def generate(cls) -> RuntimeCredentialCipher:
        """Generate a 3072-bit RSA key pair for the current Runtime process."""

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        return cls(uuid4(), private_key)

    def public_key(self) -> CredentialPublicKey:
        """Export canonical DER SPKI bytes for direct React encryption."""

        public_der = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return CredentialPublicKey(
            version=1,
            scheme="RSA-OAEP-256+A256GCM",
            key_id=self._key_id,
            public_key_spki_b64=base64.b64encode(public_der).decode("ascii"),
        )

    def decrypt(self, envelope: CredentialEnvelope) -> bytearray:
        """Authenticate and decrypt one current-key bounded credential envelope."""

        if envelope.key_id != self._key_id:
            raise CredentialCipherError("CREDENTIAL_KEY_STALE")
        wrapped_key = _decode_canonical_base64(envelope.wrapped_key_b64)
        iv = _decode_canonical_base64(envelope.iv_b64)
        ciphertext = _decode_canonical_base64(envelope.ciphertext_b64)
        if len(wrapped_key) != self._private_key.key_size // 8:
            raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
        if len(iv) != _AES_GCM_IV_BYTES:
            raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
        if not (
            _AES_GCM_TAG_BYTES < len(ciphertext)
            <= MAX_CREDENTIAL_PLAINTEXT_BYTES + _AES_GCM_TAG_BYTES
        ):
            raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
        try:
            unwrapped = self._private_key.decrypt(
                wrapped_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        except ValueError as exc:
            raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID") from exc
        aes_key = bytearray(unwrapped)
        try:
            if len(aes_key) != _AES_KEY_BYTES:
                raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
            try:
                plaintext = AESGCM(bytes(aes_key)).decrypt(
                    iv,
                    ciphertext,
                    _associated_data(self._key_id),
                )
            except (InvalidTag, ValueError) as exc:
                raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID") from exc
            if not plaintext or len(plaintext) > MAX_CREDENTIAL_PLAINTEXT_BYTES:
                raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
            return bytearray(plaintext)
        finally:
            zeroize(aes_key)


def zeroize(value: bytearray) -> None:
    """Overwrite one mutable secret buffer in place."""

    for index in range(len(value)):
        value[index] = 0


def _associated_data(key_id: UUID) -> bytes:
    """Build the exact credential-envelope v1 authenticated context."""

    return f"harness-shell-credential-v1\0{key_id}".encode("utf-8")


def _decode_canonical_base64(value: str) -> bytes:
    """Decode canonical Base64 without accepting alternate spellings."""

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise CredentialCipherError("CREDENTIAL_ENVELOPE_INVALID")
    return decoded
