from __future__ import annotations

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
    ("private_key", "passphrase", "error_code"),
    [
        (
            asyncssh.generate_private_key("ssh-ed25519").export_private_key(
                "openssh", passphrase="correct"
            ),
            None,
            "PRIVATE_KEY_PASSPHRASE_REQUIRED",
        ),
        (
            asyncssh.generate_private_key("ssh-ed25519").export_private_key(
                "openssh", passphrase="correct"
            ),
            b"wrong",
            "PRIVATE_KEY_PASSPHRASE_INVALID",
        ),
        (b"not-a-private-key", None, "PRIVATE_KEY_UNSUPPORTED"),
    ],
)
def test_private_key_failures_have_distinct_safe_codes(
    private_key: bytes, passphrase: bytes | None, error_code: str
) -> None:
    with pytest.raises(SshRuntimeError) as raised:
        build_auth_options(
            auth_kind="private_key",
            private_key=private_key,
            passphrase=passphrase,
        )

    assert raised.value.error_code == error_code
    assert "correct" not in str(raised.value)
    assert "wrong" not in str(raised.value)
