from __future__ import annotations

from ..storage_support import RepositoryClient, sql

from importlib import import_module
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


def load_credentials_module():
    """加载目标包，使缺失实现明确触发测试失败。"""

    try:
        return import_module("harness_shell_sidecar.credentials")
    except ModuleNotFoundError as exc:
        raise AssertionError("credential repository API is not implemented") from exc


def open_repository(tmp_path: Path):
    """打开全新数据库及其凭据仓库。"""

    credentials = load_credentials_module()
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    store = RepositoryClient(database, PlaintextRecordStore)
    return database, store, credentials.CredentialRepository(store)


def test_repository_persists_plaintext_and_returns_only_identity(
    tmp_path: Path,
) -> None:
    database, store, repository = open_repository(tmp_path)
    try:
        credential_id = repository.create("api_key", "marker-secret")
        raw = sql(database,
            "SELECT payload FROM runtime_records WHERE record_type = 'credential'"
        ).fetchone()[0]

        assert b"marker-secret" in raw
        assert repository.resolve(credential_id, "api_key") == bytearray(
            b"marker-secret"
        )
    finally:
        database.close()


def test_repository_rejects_missing_and_wrong_kind_without_secret_text(
    tmp_path: Path,
) -> None:
    credentials = load_credentials_module()
    database, _, repository = open_repository(tmp_path)
    try:
        credential_id = repository.create("ssh_password", "marker-secret")

        with pytest.raises(
            credentials.CredentialRepositoryError,
            match="CREDENTIAL_KIND_MISMATCH",
        ) as mismatch:
            repository.resolve(credential_id, "api_key")
        with pytest.raises(
            credentials.CredentialRepositoryError,
            match="CREDENTIAL_NOT_FOUND",
        ) as missing:
            repository.resolve(uuid4(), "api_key")

        assert "marker-secret" not in str(mismatch.value)
        assert "marker-secret" not in str(missing.value)
    finally:
        database.close()


def test_repository_delete_is_explicit(tmp_path: Path) -> None:
    database, _, repository = open_repository(tmp_path)
    try:
        credential_id = repository.create("private_key_passphrase", "passphrase")

        assert repository.delete(credential_id) is True
        assert repository.delete(credential_id) is False
    finally:
        database.close()
