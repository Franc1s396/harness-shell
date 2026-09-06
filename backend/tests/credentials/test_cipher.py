from __future__ import annotations

import base64
from importlib import import_module

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def load_credentials_module():
    """加载目标包，使缺失实现明确触发测试失败。"""

    try:
        return import_module("harness_shell_sidecar.credentials")
    except ModuleNotFoundError as exc:
        raise AssertionError("credential cipher API is not implemented") from exc


def encrypt_for_test(public_key, plaintext: bytes):
    """使用固定字节构建精确兼容 Web Crypto 的 v1 信封。"""

    credentials = load_credentials_module()
    aes_key = bytes(range(32))
    iv = bytes(range(12))
    aad = f"harness-shell-credential-v1\0{public_key.key_id}".encode("utf-8")
    ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, aad)
    rsa_public_key = serialization.load_der_public_key(
        base64.b64decode(public_key.public_key_spki_b64, validate=True)
    )
    wrapped_key = rsa_public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return credentials.CredentialEnvelope(
        version=1,
        key_id=public_key.key_id,
        wrapped_key_b64=base64.b64encode(wrapped_key).decode("ascii"),
        iv_b64=base64.b64encode(iv).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
    )


def test_cipher_decrypts_webcrypto_compatible_envelope() -> None:
    credentials = load_credentials_module()
    cipher = credentials.RuntimeCredentialCipher.generate()
    envelope = encrypt_for_test(cipher.public_key(), b"secret-value")

    assert cipher.decrypt(envelope) == bytearray(b"secret-value")


def test_cipher_rejects_stale_key_id() -> None:
    credentials = load_credentials_module()
    first = credentials.RuntimeCredentialCipher.generate()
    second = credentials.RuntimeCredentialCipher.generate()
    envelope = encrypt_for_test(first.public_key(), b"secret-value")

    with pytest.raises(credentials.CredentialCipherError, match="CREDENTIAL_KEY_STALE"):
        second.decrypt(envelope)


def test_cipher_rejects_noncanonical_base64() -> None:
    credentials = load_credentials_module()
    cipher = credentials.RuntimeCredentialCipher.generate()
    envelope = encrypt_for_test(cipher.public_key(), b"secret-value")
    invalid = envelope.model_copy(
        update={"ciphertext_b64": envelope.ciphertext_b64 + "="}
    )

    with pytest.raises(
        credentials.CredentialCipherError,
        match="CREDENTIAL_ENVELOPE_INVALID",
    ):
        cipher.decrypt(invalid)
