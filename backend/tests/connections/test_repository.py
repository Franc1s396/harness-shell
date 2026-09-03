from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

import pytest

from harness_shell_sidecar.connections import repository as repository_module
from harness_shell_sidecar.connections import (
    ConnectionProfileInput,
    ConnectionRepository,
    ConnectionRepositoryError,
    HostKeyCandidate,
)
from harness_shell_sidecar.storage import RuntimeDatabase


def profile_input(name: str, **overrides: object) -> ConnectionProfileInput:
    values: dict[str, object] = {
        "display_name": name,
        "group_name": None,
        "host": f"{name}.example",
        "port": 22,
        "username": "deploy",
        "auth_kind": "password",
        "credential_id": uuid4(),
        "passphrase_credential_id": None,
        "proxy_jump_id": None,
        "favorite": False,
    }
    values.update(overrides)
    return ConnectionProfileInput.model_validate(values)


@pytest.fixture
def repository(tmp_path: Path):
    database = RuntimeDatabase.open_plaintext((tmp_path / "runtime.sqlite3").resolve())
    try:
        yield ConnectionRepository(database)
    finally:
        database.close()


def test_profile_crud_round_trip(repository: ConnectionRepository) -> None:
    created = repository.create(profile_input("prod"))
    assert repository.get(created.connection_id) == created
    assert repository.list() == [created]

    updated = repository.update(
        created.connection_id,
        profile_input("prod-new", favorite=True),
    )
    assert updated.connection_id == created.connection_id
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at
    assert created.version == 1
    assert updated.version == 2
    assert updated.favorite is True

    assert repository.delete(created.connection_id) is True
    assert repository.delete(created.connection_id) is False
    assert repository.get(created.connection_id) is None


def test_proxy_jump_must_exist_and_cannot_be_nested(
    repository: ConnectionRepository,
) -> None:
    with pytest.raises(ConnectionRepositoryError) as missing:
        repository.create(profile_input("target", proxy_jump_id=uuid4()))
    assert missing.value.error_code == "PROXY_JUMP_NOT_FOUND"

    jump = repository.create(profile_input("jump"))
    target = repository.create(
        profile_input("target", proxy_jump_id=jump.connection_id)
    )
    with pytest.raises(ConnectionRepositoryError) as nested:
        repository.create(
            profile_input("third", proxy_jump_id=target.connection_id)
        )
    assert nested.value.error_code == "MULTI_HOP_PROXY_FORBIDDEN"


def candidate(connection_id, suffix: bytes) -> HostKeyCandidate:
    key = b"ssh-ed25519 AAAA" + suffix
    return HostKeyCandidate(
        connection_id=connection_id,
        host="prod.example",
        port=22,
        key_algorithm="ssh-ed25519",
        fingerprint_sha256=f"SHA256:{suffix.decode('ascii')}",
        public_key_openssh_b64=base64.b64encode(key).decode("ascii"),
    )


def test_host_key_first_trust_and_compare_and_swap_replacement(
    repository: ConnectionRepository,
) -> None:
    profile = repository.create(profile_input("prod"))
    first = repository.trust_first_host_key(candidate(profile.connection_id, b"first"))
    assert first.status == "active"
    assert repository.active_host_key(profile.connection_id) == first

    with pytest.raises(ConnectionRepositoryError) as duplicate:
        repository.trust_first_host_key(candidate(profile.connection_id, b"second"))
    assert duplicate.value.error_code == "HOST_KEY_ALREADY_TRUSTED"

    with pytest.raises(ConnectionRepositoryError) as stale:
        repository.replace_host_key(
            candidate(profile.connection_id, b"second"),
            "SHA256:stale",
        )
    assert stale.value.error_code == "HOST_KEY_REPLACE_CONFLICT"

    replacement = repository.replace_host_key(
        candidate(profile.connection_id, b"second"),
        first.fingerprint_sha256,
    )
    assert replacement.status == "active"
    assert replacement.fingerprint_sha256 == "SHA256:second"
    assert repository.active_host_key(profile.connection_id) == replacement


def test_profile_version_increments_even_when_wall_clock_does_not(
    repository: ConnectionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repository_module,
        "_utc_now",
        lambda: "2026-08-29T00:00:00.000000Z",
    )
    created = repository.create(profile_input("created"))
    first = repository.update(created.connection_id, profile_input("first"))
    second = repository.update(created.connection_id, profile_input("second"))

    assert (created.version, first.version, second.version) == (1, 2, 3)
    assert created.updated_at == first.updated_at == second.updated_at


def test_profile_update_rejects_version_exhaustion(
    repository: ConnectionRepository,
) -> None:
    created = repository.create(profile_input("created"))
    repository._database.execute(
        "UPDATE connection_profiles SET version = ? WHERE connection_id = ?",
        (2**53 - 1, str(created.connection_id)),
    )

    with pytest.raises(ConnectionRepositoryError) as raised:
        repository.update(created.connection_id, profile_input("blocked"))

    assert raised.value.error_code == "CONNECTION_VERSION_EXHAUSTED"
    stored = repository.get(created.connection_id)
    assert stored is not None
    assert stored.display_name == "created"
    assert stored.version == 2**53 - 1
