from __future__ import annotations

from pathlib import Path

import pytest

from harness_shell_sidecar.connections import (
    ConnectionProfileInput,
    ConnectionRepository,
)
from harness_shell_sidecar.credentials import (
    CredentialRepository,
    CredentialService,
    CredentialServiceError,
)
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase


def test_service_resolves_direct_and_jump_then_zeroizes(
    tmp_path: Path,
) -> None:
    database = RuntimeDatabase.open_plaintext(tmp_path / "runtime.sqlite3")
    try:
        records = PlaintextRecordStore(database)
        credentials = CredentialRepository(records)
        connections = ConnectionRepository(database)
        jump_key = credentials.create("imported_private_key", "jump-key")
        jump_passphrase = credentials.create(
            "private_key_passphrase",
            "jump-passphrase",
        )
        direct_password = credentials.create("ssh_password", "direct-password")
        jump = connections.create(
            connection_input(
                display_name="jump",
                auth_kind="private_key",
                credential_id=jump_key,
                passphrase_credential_id=jump_passphrase,
            )
        )
        direct = connections.create(
            connection_input(
                display_name="direct",
                auth_kind="password",
                credential_id=direct_password,
                proxy_jump_id=jump.connection_id,
            )
        )
        service = CredentialService(connections, credentials)

        resolved = service.build_ssh_connect(direct.connection_id)
        assert resolved.profile_version == direct.version
        assert resolved.password == bytearray(b"direct-password")
        assert resolved.jump_connection_id == jump.connection_id
        assert resolved.jump_profile_version == jump.version
        assert resolved.jump_private_key == bytearray(b"jump-key")
        assert resolved.jump_passphrase == bytearray(b"jump-passphrase")

        allocated = [
            resolved.password,
            resolved.jump_private_key,
            resolved.jump_passphrase,
        ]
        resolved.close()
        assert all(value is not None and not any(value) for value in allocated)
    finally:
        database.close()


def test_service_rejects_profile_version_change_after_resolution(
    tmp_path: Path,
) -> None:
    database = RuntimeDatabase.open_plaintext(tmp_path / "runtime.sqlite3")
    try:
        records = PlaintextRecordStore(database)
        credentials = CredentialRepository(records)
        connections = ConnectionRepository(database)
        password = credentials.create("ssh_password", "direct-password")
        direct_input = connection_input(
            display_name="direct",
            auth_kind="password",
            credential_id=password,
        )
        direct = connections.create(direct_input)
        resolved_buffers: list[bytearray] = []

        class RacingCredentials:
            """返回秘密后立即变更配置。"""

            def resolve(self, credential_id, expected_kind):
                """返回真实秘密并强制版本复核失败。"""

                secret = credentials.resolve(credential_id, expected_kind)
                resolved_buffers.append(secret)
                connections.update(
                    direct.connection_id,
                    direct_input.model_copy(update={"display_name": "changed"}),
                )
                return secret

        service = CredentialService(connections, RacingCredentials())

        with pytest.raises(
            CredentialServiceError,
            match="CONNECTION_PROFILE_CHANGED",
        ):
            service.build_ssh_connect(direct.connection_id)

        assert resolved_buffers and all(not any(value) for value in resolved_buffers)
    finally:
        database.close()


def connection_input(**overrides: object) -> ConnectionProfileInput:
    """为凭据解析测试构建严格配置输入。"""

    values: dict[str, object] = {
        "display_name": "connection",
        "group_name": None,
        "host": "example.invalid",
        "port": 22,
        "username": "tester",
        "auth_kind": "password",
        "credential_id": __import__("uuid").uuid4(),
        "passphrase_credential_id": None,
        "proxy_jump_id": None,
        "favorite": False,
    }
    values.update(overrides)
    return ConnectionProfileInput.model_validate(values)
