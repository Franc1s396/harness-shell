"""严格的明文凭据持久化与按类型校验的解析。"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore

from .cipher import MAX_CREDENTIAL_PLAINTEXT_BYTES
from .models import CredentialKind


_CREDENTIAL_RECORD_TYPE = "credential"
_CREDENTIAL_SCHEMA_VERSION = 1
_CREDENTIAL_KINDS = frozenset(
    {
        "ssh_password",
        "private_key_passphrase",
        "imported_private_key",
        "api_key",
    }
)


class CredentialRepositoryError(ValueError):
    """报告稳定且安全的凭据持久化失败错误码。"""

    error_code: str

    def __init__(self, error_code: str, message: str) -> None:
        """保存稳定错误码与已审查详情，不包含凭据内容。"""

        self.error_code = error_code
        self.safe_message = message
        super().__init__(f"{error_code}: {message}")


class CredentialRepository:
    """管理 schema v7 明文凭据，并在读取时严格匹配用途。"""

    _store: PlaintextRecordStore

    def __init__(self, store: PlaintextRecordStore) -> None:
        """将仓库绑定到 Runtime 拥有的通用记录存储。"""

        self._store = store  # 在所有领域仓库之后关闭的共享资源管理者。

    def create(self, kind: CredentialKind, secret: str) -> UUID:
        """持久化新的明文凭据并返回不透明标识。"""

        _require_kind(kind)
        encoded_secret = secret.encode("utf-8")
        if not encoded_secret or len(encoded_secret) > MAX_CREDENTIAL_PLAINTEXT_BYTES:
            raise CredentialRepositoryError(
                "CREDENTIAL_SECRET_INVALID",
                "the credential secret is empty or exceeds the storage limit",
            )
        credential_id = uuid4()
        payload = json.dumps(
            {
                "credential_id": str(credential_id),
                "kind": kind,
                "secret": secret,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self._store.put(
            PlaintextRecord(
                record_type=_CREDENTIAL_RECORD_TYPE,
                record_id=str(credential_id),
                schema_version=_CREDENTIAL_SCHEMA_VERSION,
                payload=payload,
            )
        )
        return credential_id

    def resolve(
        self,
        credential_id: UUID,
        expected_kind: CredentialKind,
    ) -> bytearray:
        """只有标识与用途均匹配时才返回可变秘密缓冲区。"""

        _require_kind(expected_kind)
        record = self._store.get(_CREDENTIAL_RECORD_TYPE, str(credential_id))
        if record is None:
            raise CredentialRepositoryError(
                "CREDENTIAL_NOT_FOUND",
                "the requested credential record does not exist",
            )
        payload = _decode_record(record, credential_id)
        if payload["kind"] != expected_kind:
            raise CredentialRepositoryError(
                "CREDENTIAL_KIND_MISMATCH",
                "the credential record belongs to a different purpose",
            )
        return bytearray(payload["secret"].encode("utf-8"))

    def delete(self, credential_id: UUID) -> bool:
        """删除指定凭据，并报告其原先是否存在。"""

        return self._store.delete(_CREDENTIAL_RECORD_TYPE, str(credential_id))


def _decode_record(record: PlaintextRecord, credential_id: UUID) -> dict[str, str]:
    """校验完整记录标识、版本、JSON 结构和字段类型。"""

    if record.schema_version != _CREDENTIAL_SCHEMA_VERSION:
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record schema version is unsupported",
        )
    try:
        decoded = record.payload.decode("utf-8", errors="strict")
        payload = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record is not canonical UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "credential_id",
        "kind",
        "secret",
    }:
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record fields do not match the schema",
        )
    if not all(isinstance(value, str) for value in payload.values()):
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record contains non-string field values",
        )
    if payload["credential_id"] != str(credential_id):
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record identity does not match its storage key",
        )
    _require_kind(payload["kind"])
    if not payload["secret"]:
        raise CredentialRepositoryError(
            "CREDENTIAL_RECORD_INVALID",
            "the credential record contains an empty secret",
        )
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复 JSON 键，不静默采用最后一个值。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential record field")
        result[key] = value
    return result


def _require_kind(kind: str) -> None:
    """拒绝限定支持集合之外的凭据用途。"""

    if kind not in _CREDENTIAL_KINDS:
        raise CredentialRepositoryError(
            "CREDENTIAL_KIND_INVALID",
            "the credential kind is outside the supported allowlist",
        )
