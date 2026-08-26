from __future__ import annotations

import asyncio

import pytest


@pytest.mark.parametrize("authentication", ["password", "key", "encrypted-key"])
def test_direct_password_key_and_passphrase_authentication(
    authentication, runtime_context, lab
) -> None:
    async def scenario() -> None:
        auth_kind = "password" if authentication == "password" else "private_key"
        profile = runtime_context.create_profile(
            name=f"direct-{authentication}",
            host=lab.jump_host,
            port=lab.jump_port,
            username=lab.jump_username,
            auth_kind=auth_kind,
        )
        observation = await runtime_context.runtime.inspect_host_key(profile.connection_id)
        assert observation.host_key_candidate is not None
        assert observation.host_key_candidate.fingerprint_sha256 == lab.jump_host_fingerprint
        runtime_context.repository.trust_first_host_key(observation.host_key_candidate)
        kwargs = {"password": lab.jump_password.encode()}
        if authentication == "key":
            kwargs = {"private_key": lab.unencrypted_private_key_path.read_bytes()}
        elif authentication == "encrypted-key":
            kwargs = {
                "private_key": lab.encrypted_private_key_path.read_bytes(),
                "passphrase": lab.private_key_passphrase.encode(),
            }
        status = await runtime_context.runtime.connect(profile.connection_id, **kwargs)
        assert status.state == "READY"
        assert status.session_id is not None
        await runtime_context.runtime.disconnect(status.session_id)
        assert len(runtime_context.runtime.sessions) == 0

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())
