"""In-memory SSH authentication material parsing."""

from __future__ import annotations

import base64
import binascii
from typing import Literal

import asyncssh

from .errors import SshRuntimeError


def build_auth_options(
    *,
    auth_kind: Literal["password", "private_key"],
    password: bytes | bytearray | None = None,
    private_key: bytes | bytearray | None = None,
    passphrase: bytes | bytearray | None = None,
) -> dict:
    if auth_kind == "password":
        if password is None:
            raise _auth_error(
                "SSH_PASSWORD_REQUIRED",
                "password authentication requires a password",
            )
        try:
            password_text = bytes(password).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _auth_error(
                "SSH_PASSWORD_ENCODING_INVALID",
                "SSH password is not valid UTF-8",
            ) from exc
        return {
            "password": password_text,
            "client_keys": [],
            "agent_path": None,
        }

    if private_key is None:
        raise _auth_error(
            "PRIVATE_KEY_REQUIRED",
            "private-key authentication requires imported key data",
        )
    key_bytes = bytes(private_key)
    encrypted = _private_key_is_encrypted(key_bytes)
    if encrypted is True and passphrase is None:
        raise _auth_error(
            "PRIVATE_KEY_PASSPHRASE_REQUIRED",
            "encrypted private key requires a passphrase",
        )
    try:
        key = asyncssh.import_private_key(
            key_bytes,
            passphrase=None if passphrase is None else bytes(passphrase),
        )
    except (asyncssh.KeyImportError, asyncssh.KeyEncryptionError, ValueError) as exc:
        if encrypted is True and passphrase is not None:
            raise _auth_error(
                "PRIVATE_KEY_PASSPHRASE_INVALID",
                "private key passphrase could not decrypt the imported key",
            ) from exc
        raise _auth_error(
            "PRIVATE_KEY_UNSUPPORTED",
            "imported private key format is unsupported or invalid",
        ) from exc
    return {"client_keys": [key], "agent_path": None, "password": None}


def _private_key_is_encrypted(data: bytes) -> bool | None:
    if b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in data:
        return True
    if b"Proc-Type: 4,ENCRYPTED" in data:
        return True
    if b"-----BEGIN OPENSSH PRIVATE KEY-----" not in data:
        return None
    try:
        encoded = b"".join(
            line.strip()
            for line in data.splitlines()
            if line and not line.startswith(b"-----")
        )
        decoded = base64.b64decode(encoded, validate=True)
        prefix = b"openssh-key-v1\x00"
        if not decoded.startswith(prefix):
            return None
        offset = len(prefix)
        length = int.from_bytes(decoded[offset : offset + 4], "big")
        offset += 4
        cipher = decoded[offset : offset + length]
        if len(cipher) != length:
            return None
        return cipher != b"none"
    except (ValueError, binascii.Error):
        return None


def _auth_error(error_code: str, message: str) -> SshRuntimeError:
    """Build one authentication failure without copying credential material."""

    return SshRuntimeError(
        error_code,
        message,
        node="authentication",
        recoverable=False,
        remote_state="pre_auth",
    )
