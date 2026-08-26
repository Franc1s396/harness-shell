from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType, Sensitivity
from harness_shell_sidecar.runtime import DispatchError, RequestDispatcher
from harness_shell_sidecar.ssh.errors import ConnectionStatus, SshRuntimeError
from harness_shell_sidecar.ssh.handlers import register_ssh_handlers


def frame(method: str, params: dict, *, secret: bool = False) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.REQUEST,
        request_id=uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=1,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.SECRET if secret else Sensitivity.NORMAL,
        payload={"method": method, "params": params},
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.captured_secrets: list[bytearray] = []

    async def inspect_host_key(
        self, connection_id: UUID, **secrets
    ) -> ConnectionStatus:
        self.captured_secrets = [
            value for value in secrets.values() if isinstance(value, bytearray)
        ]
        return status(connection_id, "HOST_KEY_REQUIRED")

    async def connect(self, connection_id: UUID, **secrets) -> ConnectionStatus:
        self.captured_secrets = [
            value for value in secrets.values() if isinstance(value, bytearray)
        ]
        return status(connection_id, "READY", session_id=uuid4())

    async def disconnect(self, session_id: UUID) -> ConnectionStatus:
        return status(uuid4(), "DISCONNECTED")


def status(
    connection_id: UUID, state: str, *, session_id: UUID | None = None
) -> ConnectionStatus:
    return ConnectionStatus(
        connection_id=connection_id,
        state=state,
        session_id=session_id,
        error_code=None,
        recoverable=False,
        correlation_id=uuid4(),
        host_key_candidate=None,
    )


def test_secret_connect_payload_is_required_decoded_and_zeroized() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        register_ssh_handlers(dispatcher, runtime)
        connection_id = uuid4()

        with pytest.raises(DispatchError) as normal:
            await dispatcher.dispatch(
                frame(
                    "ssh.connect",
                    {
                        "connection_id": str(connection_id),
                        "profile_updated_at": "2026-08-25T00:00:00Z",
                        "password_b64": base64.b64encode(b"secret").decode("ascii"),
                    },
                )
            )
        assert normal.value.error_code == "SENSITIVE_FRAME_REQUIRED"

        result = await dispatcher.dispatch(
            frame(
                "ssh.connect",
                {
                    "connection_id": str(connection_id),
                    "profile_updated_at": "2026-08-25T00:00:00Z",
                    "password_b64": base64.b64encode(b"secret").decode("ascii"),
                },
                secret=True,
            )
        )
        assert result.payload["status"]["state"] == "READY"
        assert runtime.captured_secrets
        assert all(set(secret) <= {0} for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_proxy_jump_connect_decodes_and_zeroizes_both_credentials() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        register_ssh_handlers(dispatcher, runtime)
        connection_id = uuid4()
        jump_id = uuid4()

        result = await dispatcher.dispatch(
            frame(
                "ssh.connect",
                {
                    "connection_id": str(connection_id),
                    "profile_updated_at": "2026-08-25T00:00:00Z",
                    "password_b64": base64.b64encode(b"target-secret").decode(
                        "ascii"
                    ),
                    "jump": {
                        "connection_id": str(jump_id),
                        "profile_updated_at": "2026-08-25T00:00:01Z",
                        "password_b64": base64.b64encode(b"jump-secret").decode(
                            "ascii"
                        ),
                    },
                },
                secret=True,
            )
        )

        assert result.payload["status"]["state"] == "READY"
        assert len(runtime.captured_secrets) == 2
        assert all(set(secret) <= {0} for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_proxy_jump_host_key_inspection_requires_secret_frame() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        dispatcher = RequestDispatcher()
        register_ssh_handlers(dispatcher, runtime)
        params = {
            "connection_id": str(uuid4()),
            "jump": {
                "connection_id": str(uuid4()),
                "profile_updated_at": "2026-08-25T00:00:01Z",
                "password_b64": base64.b64encode(b"jump-secret").decode("ascii"),
            },
        }

        with pytest.raises(DispatchError) as normal:
            await dispatcher.dispatch(frame("host_key.inspect", params))
        assert normal.value.error_code == "SENSITIVE_FRAME_REQUIRED"

        result = await dispatcher.dispatch(
            frame("host_key.inspect", params, secret=True)
        )
        assert result.payload["status"]["state"] == "HOST_KEY_REQUIRED"
        assert runtime.captured_secrets
        assert all(set(secret) <= {0} for secret in runtime.captured_secrets)

    asyncio.run(scenario())


def test_ssh_errors_keep_only_structured_safe_details() -> None:
    class FailingRuntime(FakeRuntime):
        async def inspect_host_key(
            self, connection_id: UUID, **secrets
        ) -> ConnectionStatus:
            raise SshRuntimeError(
                "HOST_KEY_CHANGED",
                node="host_key",
                recoverable=False,
                remote_state="pre_auth",
            )

    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        register_ssh_handlers(dispatcher, FailingRuntime())
        with pytest.raises(DispatchError) as raised:
            await dispatcher.dispatch(
                frame("host_key.inspect", {"connection_id": str(uuid4())})
            )
        assert raised.value.error_code == "HOST_KEY_CHANGED"
        assert raised.value.details["node"] == "host_key"
        assert raised.value.details["remote_state"] == "pre_auth"
        assert "username" not in str(raised.value.details)
        assert "secret" not in str(raised.value.details)

    asyncio.run(scenario())
