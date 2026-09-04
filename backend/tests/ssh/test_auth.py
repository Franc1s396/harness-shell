from __future__ import annotations

from typing import Literal

import asyncssh
import pytest

from harness_shell_sidecar.ssh.auth import build_auth_options
from harness_shell_sidecar.ssh.errors import SshRuntimeError


def test_password_auth_disables_agent_and_default_key_discovery() -> None:
    options = build_auth_options(auth_kind="password", password=b"secret")

    assert options["password"] == "secret"
    assert options["client_keys"] == []
    assert options["agent_path"] is None


def test_private_key_auth_imports_unencrypted_and_encrypted_openssh_keys() -> None:
    key = asyncssh.generate_private_key("ssh-ed25519")
    plain = key.export_private_key("openssh")
    encrypted = key.export_private_key("openssh", passphrase="correct")

    plain_options = build_auth_options(auth_kind="private_key", private_key=plain)
    encrypted_options = build_auth_options(
        auth_kind="private_key",
        private_key=encrypted,
        passphrase=b"correct",
    )

    assert len(plain_options["client_keys"]) == 1
    assert len(encrypted_options["client_keys"]) == 1
    assert plain_options["agent_path"] is None
    assert encrypted_options["agent_path"] is None


@pytest.mark.parametrize(
    ("private_key", "passphrase", "error_code", "expected_reason"),
    [
        (
            asyncssh.generate_private_key("ssh-ed25519").export_private_key(
                "openssh", passphrase="correct"
            ),
            None,
            "PRIVATE_KEY_PASSPHRASE_REQUIRED",
            "encrypted private key requires a passphrase",
        ),
        (
            asyncssh.generate_private_key("ssh-ed25519").export_private_key(
                "openssh", passphrase="correct"
            ),
            b"wrong",
            "PRIVATE_KEY_PASSPHRASE_INVALID",
            "private key passphrase could not decrypt the imported key",
        ),
        (
            b"not-a-private-key",
            None,
            "PRIVATE_KEY_UNSUPPORTED",
            "imported private key format is unsupported or invalid",
        ),
    ],
)
def test_private_key_failures_have_distinct_safe_codes(
    private_key: bytes,
    passphrase: bytes | None,
    error_code: str,
    expected_reason: str,
) -> None:
    with pytest.raises(SshRuntimeError) as raised:
        build_auth_options(
            auth_kind="private_key",
            private_key=private_key,
            passphrase=passphrase,
        )

    assert raised.value.error_code == error_code
    assert raised.value.safe_message == expected_reason
    assert expected_reason in str(raised.value)
    assert "correct" not in str(raised.value)
    assert "wrong" not in str(raised.value)


@pytest.mark.parametrize(
    ("auth_kind", "password", "private_key", "error_code", "expected_reason"),
    [
        (
            "password",
            None,
            None,
            "SSH_PASSWORD_REQUIRED",
            "password authentication requires a password",
        ),
        (
            "password",
            b"\xff",
            None,
            "SSH_PASSWORD_ENCODING_INVALID",
            "SSH password is not valid UTF-8",
        ),
        (
            "private_key",
            None,
            None,
            "PRIVATE_KEY_REQUIRED",
            "private-key authentication requires imported key data",
        ),
    ],
)
def test_missing_or_invalid_auth_material_has_a_specific_safe_reason(
    auth_kind: Literal["password", "private_key"],
    password: bytes | None,
    private_key: bytes | None,
    error_code: str,
    expected_reason: str,
) -> None:
    with pytest.raises(SshRuntimeError) as raised:
        build_auth_options(
            auth_kind=auth_kind,
            password=password,
            private_key=private_key,
        )

    assert raised.value.error_code == error_code
    assert raised.value.safe_message == expected_reason
    assert expected_reason in str(raised.value)
