from __future__ import annotations

import asyncio


def test_single_proxy_jump_uses_separate_identity_and_host_keys(
    runtime_context, connect_proxy, lab
) -> None:
    async def scenario() -> None:
        jump, target, status = await connect_proxy()
        assert jump.username != target.username
        assert lab.jump_password != lab.target_password
        jump_key = runtime_context.repository.active_host_key(jump.connection_id)
        target_key = runtime_context.repository.active_host_key(target.connection_id)
        assert jump_key.fingerprint_sha256 != target_key.fingerprint_sha256
        session = runtime_context.runtime.sessions.get(status.session_id)
        assert session is not None
        assert session.jump_connection is not None
        assert session.connection is not session.jump_connection
        await runtime_context.runtime.disconnect(status.session_id)
        assert len(runtime_context.runtime.sessions) == 0

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runtime_context.close())
