from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harness_shell_sidecar.connections import (
    ConnectionProfile,
    ConnectionProfileInput,
    HostKeyCandidate,
)


def profile_input(**overrides: object) -> ConnectionProfileInput:
    values: dict[str, object] = {
        "display_name": "prod",
        "group_name": "production",
        "host": "prod.example",
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


def test_password_profile_rejects_private_key_passphrase() -> None:
    with pytest.raises(ValueError, match="passphrase"):
        profile_input(passphrase_credential_id=uuid4())


@pytest.mark.parametrize(
    ("field", "value"),
    (("display_name", " "), ("host", ""), ("username", ""), ("port", 65_536)),
)
def test_profile_rejects_invalid_identity(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        profile_input(**{field: value})


def test_persisted_profile_rejects_self_proxy_jump() -> None:
    connection_id = uuid4()
    now = datetime.now(timezone.utc)

    with pytest.raises(ValueError, match="itself"):
        ConnectionProfile(
            connection_id=connection_id,
            **profile_input(proxy_jump_id=connection_id).model_dump(),
            created_at=now,
            updated_at=now,
        )


def test_host_key_candidate_requires_canonical_base64() -> None:
    with pytest.raises(ValueError, match="base64"):
        HostKeyCandidate(
            connection_id=uuid4(),
            host="prod.example",
            port=22,
            key_algorithm="ssh-ed25519",
            fingerprint_sha256="SHA256:abc",
            public_key_openssh_b64="not-base64%%%",
        )

    candidate = HostKeyCandidate(
        connection_id=uuid4(),
        host="prod.example",
        port=22,
        key_algorithm="ssh-ed25519",
        fingerprint_sha256="SHA256:abc",
        public_key_openssh_b64=base64.b64encode(b"ssh-ed25519 AAAA").decode("ascii"),
    )
    assert candidate.public_key_openssh() == b"ssh-ed25519 AAAA"
