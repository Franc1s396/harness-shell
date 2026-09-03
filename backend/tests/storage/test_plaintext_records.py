from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase


class _PlaintextStore(Protocol):
    """Describe the public store operations exercised by this contract test."""

    def put(self, record: object) -> None: ...

    def get(self, record_type: str, record_id: str) -> object | None: ...

    def delete(self, record_type: str, record_id: str) -> bool: ...

    def list_ids(self, record_type: str) -> tuple[str, ...]: ...


def load_plaintext_types() -> tuple[type, type]:
    """Load the target public types so a missing implementation is a RED failure."""

    try:
        from harness_shell_sidecar.storage import PlaintextRecord, PlaintextRecordStore
    except ImportError as exc:
        raise AssertionError("plaintext record API is not implemented") from exc
    return PlaintextRecord, PlaintextRecordStore


def open_store(tmp_path: Path) -> tuple[RuntimeDatabase, _PlaintextStore]:
    """Open a fresh schema-v6 database and its plaintext record owner."""

    _, plaintext_store = load_plaintext_types()
    database = RuntimeDatabase.open_plaintext(tmp_path / "runtime.sqlite3")
    return database, plaintext_store(database)


def test_plaintext_store_round_trips_payload_without_encoding(tmp_path: Path) -> None:
    plaintext_record, _ = load_plaintext_types()
    database, store = open_store(tmp_path)
    source = plaintext_record("credential", "credential-1", 1, b"marker-secret")
    try:
        store.put(source)

        assert store.get("credential", "credential-1") == source
        assert database.execute(
            "SELECT payload FROM runtime_records WHERE record_type = ? AND record_id = ?",
            ("credential", "credential-1"),
        ).fetchone() == (b"marker-secret",)
    finally:
        database.close()


def test_plaintext_store_updates_payload_and_preserves_created_at(
    tmp_path: Path,
) -> None:
    plaintext_record, _ = load_plaintext_types()
    database, store = open_store(tmp_path)
    try:
        store.put(plaintext_record("agent_message", "message-1", 1, b"first"))
        created_at = database.execute(
            "SELECT created_at FROM runtime_records"
        ).fetchone()[0]

        store.put(plaintext_record("agent_message", "message-1", 2, b"second"))

        row = database.execute(
            "SELECT schema_version, payload, created_at FROM runtime_records"
        ).fetchone()
        assert row == (2, b"second", created_at)
    finally:
        database.close()


def test_plaintext_store_lists_and_deletes_only_matching_records(
    tmp_path: Path,
) -> None:
    plaintext_record, _ = load_plaintext_types()
    database, store = open_store(tmp_path)
    try:
        store.put(plaintext_record("credential", "b", 1, b"two"))
        store.put(plaintext_record("credential", "a", 1, b"one"))
        store.put(plaintext_record("other", "c", 1, b"three"))

        assert store.list_ids("credential") == ("a", "b")
        assert store.delete("credential", "missing") is False
        assert store.delete("credential", "a") is True
        assert store.get("credential", "a") is None
        assert store.list_ids("credential") == ("b",)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("record_type", "record_id", "schema_version"),
    [
        ("", "id", 1),
        ("type", "", 1),
        ("type", "id", 0),
    ],
)
def test_plaintext_record_rejects_invalid_identity_or_version(
    record_type: str,
    record_id: str,
    schema_version: int,
) -> None:
    plaintext_record, _ = load_plaintext_types()
    with pytest.raises(ValueError):
        plaintext_record(record_type, record_id, schema_version, b"payload")
