"""临时 RSA-OAEP 与 AES-GCM 凭据信封解密。"""

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
    """报告稳定且安全的凭据信封失败错误码。"""

    error_code: str

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定错误码与经过审查的详情，不含密码学值。"""

        self.error_code = error_code
        self.safe_message = message
        super().__init__(f"{error_code}: {message}")


class RuntimeCredentialCipher:
    """拥有一个进程级临时 RSA 私钥及其公开标识。"""

    _key_id: UUID
    _private_key: rsa.RSAPrivateKey

    def __init__(self, key_id: UUID, private_key: rsa.RSAPrivateKey) -> None:
        """独占一个生成的 Runtime 私钥。"""

        self._key_id = key_id  # 每次 Runtime 进程启动时更新。
        # 仅保留在 Python 内存中，不持久化、记录日志、返回或传出。
        self._private_key = private_key

    @classmethod
    def generate(cls) -> RuntimeCredentialCipher:
        """为当前 Runtime 进程生成 3072 位 RSA 密钥对。"""

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        return cls(uuid4(), private_key)

    def public_key(self) -> CredentialPublicKey:
        """导出标准 DER SPKI 字节，供 React 直接加密。"""

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
        """认证并解密一个使用当前密钥且大小有界的凭据信封。"""

        # 1. 确认信封属于当前进程密钥，并校验标准编码和长度边界。
        if envelope.key_id != self._key_id:
            raise CredentialCipherError(
                "CREDENTIAL_KEY_STALE",
                "the credential envelope targets a different Runtime key",
            )
        wrapped_key = _decode_canonical_base64(envelope.wrapped_key_b64)
        iv = _decode_canonical_base64(envelope.iv_b64)
        ciphertext = _decode_canonical_base64(envelope.ciphertext_b64)
        if len(wrapped_key) != self._private_key.key_size // 8:
            raise CredentialCipherError(
                "CREDENTIAL_ENVELOPE_INVALID",
                "the wrapped key length does not match the Runtime RSA key",
            )
        if len(iv) != _AES_GCM_IV_BYTES:
            raise CredentialCipherError(
                "CREDENTIAL_ENVELOPE_INVALID",
                "the credential envelope IV length is invalid",
            )
        if not (
            _AES_GCM_TAG_BYTES < len(ciphertext)
            <= MAX_CREDENTIAL_PLAINTEXT_BYTES + _AES_GCM_TAG_BYTES
        ):
            raise CredentialCipherError(
                "CREDENTIAL_ENVELOPE_INVALID",
                "the encrypted credential length is outside the allowed range",
            )
        try:
            # 2. 用 RSA-OAEP 解开临时 AES 密钥，解密失败不暴露密码学材料。
            unwrapped = self._private_key.decrypt(
                wrapped_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        except ValueError as exc:
            raise CredentialCipherError(
                "CREDENTIAL_ENVELOPE_INVALID",
                "the Runtime RSA key could not unwrap the credential key",
            ) from exc
        # 3. 以信封用途作为认证上下文执行 AES-GCM 解密，并限制明文大小。
        aes_key = bytearray(unwrapped)
        try:
            if len(aes_key) != _AES_KEY_BYTES:
                raise CredentialCipherError(
                    "CREDENTIAL_ENVELOPE_INVALID",
                    "the unwrapped credential key length is invalid",
                )
            try:
                plaintext = AESGCM(bytes(aes_key)).decrypt(
                    iv,
                    ciphertext,
                    _associated_data(self._key_id),
                )
            except (InvalidTag, ValueError) as exc:
                raise CredentialCipherError(
                    "CREDENTIAL_ENVELOPE_INVALID",
                    "the encrypted credential failed authentication",
                ) from exc
            if not plaintext or len(plaintext) > MAX_CREDENTIAL_PLAINTEXT_BYTES:
                raise CredentialCipherError(
                    "CREDENTIAL_ENVELOPE_INVALID",
                    "the decrypted credential length is outside the allowed range",
                )
            return bytearray(plaintext)
        # 4. 明文交由调用方短期持有；本层始终清零临时 AES 密钥。
        finally:
            zeroize(aes_key)


def zeroize(value: bytearray) -> None:
    """原地覆盖可变秘密缓冲区。"""

    for index in range(len(value)):
        value[index] = 0


def _associated_data(key_id: UUID) -> bytes:
    """构建凭据信封 v1 的精确认证上下文。"""

    return f"harness-shell-credential-v1\0{key_id}".encode("utf-8")


def _decode_canonical_base64(value: str) -> bytes:
    """解码标准 Base64，不接受其他等价写法。"""

    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialCipherError(
            "CREDENTIAL_ENVELOPE_INVALID",
            "a credential envelope field is not valid Base64",
        ) from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise CredentialCipherError(
            "CREDENTIAL_ENVELOPE_INVALID",
            "a credential envelope field is not canonical Base64",
        )
    return decoded
