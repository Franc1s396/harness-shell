"""Strict plaintext credential persistence and kind-checked resolution."""

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
    """Report a stable safe credential persistence failure code."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        """Store only the stable code and never include credential contents."""

        self.error_code = error_code
        super().__init__(error_code)


class CredentialRepository:
    """Own schema-v6 plaintext credentials and enforce exact purpose on reads."""

    _store: PlaintextRecordStore

    def __init__(self, store: PlaintextRecordStore) -> None:
        """Bind the repository to the Runtime-owned generic record store."""

        self._store = store  # Shared owner closed after all domain repositories.

    def create(self, kind: CredentialKind, secret: str) -> UUID:
        """Persist one new plaintext credential and return its opaque identity."""

        _require_kind(kind)
        encoded_secret = secret.encode("utf-8")
        if not encoded_secret or len(encoded_secret) > MAX_CREDENTIAL_PLAINTEXT_BYTES:
            raise CredentialRepositoryError("CREDENTIAL_SECRET_INVALID")
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
        """Return a mutable secret only when identity and purpose both match."""

        _require_kind(expected_kind)
        record = self._store.get(_CREDENTIAL_RECORD_TYPE, str(credential_id))
        if record is None:
            raise CredentialRepositoryError("CREDENTIAL_NOT_FOUND")
        payload = _decode_record(record, credential_id)
        if payload["kind"] != expected_kind:
            raise CredentialRepositoryError("CREDENTIAL_KIND_MISMATCH")
        return bytearray(payload["secret"].encode("utf-8"))

    def delete(self, credential_id: UUID) -> bool:
        """Delete one credential identity and report whether it existed."""

        return self._store.delete(_CREDENTIAL_RECORD_TYPE, str(credential_id))


def _decode_record(record: PlaintextRecord, credential_id: UUID) -> dict[str, str]:
    """Validate complete record identity, version, JSON shape, and field types."""

    if record.schema_version != _CREDENTIAL_SCHEMA_VERSION:
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID")
    try:
        decoded = record.payload.decode("utf-8", errors="strict")
        payload = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "credential_id",
        "kind",
        "secret",
    }:
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID")
    if not all(isinstance(value, str) for value in payload.values()):
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID")
    if payload["credential_id"] != str(credential_id):
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID")
    _require_kind(payload["kind"])
    if not payload["secret"]:
        raise CredentialRepositoryError("CREDENTIAL_RECORD_INVALID")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently taking the final value."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential record field")
        result[key] = value
    return result


def _require_kind(kind: str) -> None:
    """Reject any credential purpose outside the closed supported set."""

    if kind not in _CREDENTIAL_KINDS:
        raise CredentialRepositoryError("CREDENTIAL_KIND_INVALID")
