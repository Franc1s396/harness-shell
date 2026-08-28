from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from harness_shell_sidecar.protocol import FrameEnvelope, MessageType, Sensitivity
from harness_shell_sidecar.runtime import SidecarService


class MemoryTransport:
    """用 asyncio Queue 模拟 Sidecar 双向传输的测试替身。"""

    def __init__(self) -> None:
        """创建彼此独立的入站帧队列和出站帧队列。"""

        # 测试注入的入站批次。
        self.input: asyncio.Queue[list[FrameEnvelope] | None] = asyncio.Queue()
        self.output: asyncio.Queue[FrameEnvelope] = asyncio.Queue()  # 服务发出的帧。

    def start(self) -> None:
        """模拟无需实际启动资源的传输启动。"""

        pass

    async def read(self) -> list[FrameEnvelope] | None:
        """等待测试向入站队列提供下一批帧或 EOF。"""

        return await self.input.get()

    async def send(self, frame: FrameEnvelope) -> None:
        """把服务响应保存到测试可观察的出站队列。"""

        await self.output.put(frame)

    async def close(self) -> None:
        """模拟无需释放实际资源的传输关闭。"""

        pass


def frame(sequence: int, payload: dict, *, secret: bool = False) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.REQUEST,
        request_id=uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=sequence,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.SECRET if secret else Sensitivity.NORMAL,
        payload=payload,
    )


def profile_params(name: str) -> dict:
    return {
        "display_name": name,
        "group_name": None,
        "host": f"{name}.example",
        "port": 22,
        "username": "deploy",
        "auth_kind": "password",
        "credential_id": str(uuid4()),
        "passphrase_credential_id": None,
        "proxy_jump_id": None,
        "favorite": False,
    }


def test_initialized_service_registers_connection_and_host_key_handlers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = MemoryTransport()
        service = SidecarService(transport)
        running = asyncio.create_task(service.run())
        assert (await transport.output.get()).payload["event"] == "sidecar.ready"

        initialize = frame(
            1,
            {
                "method": "initialize",
                "app_version": "0.1.0",
                "runtime_db_path": str((tmp_path / "runtime.sqlite3").resolve()),
                "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
                "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
                "heartbeat_interval_ms": 5_000,
                "heartbeat_timeout_ms": 15_000,
            },
            secret=True,
        )
        await transport.input.put([initialize])
        assert (await transport.output.get()).payload["result"] == "initialized"

        create = frame(
            2,
            {"method": "connections.create", "params": profile_params("prod")},
        )
        await transport.input.put([create])
        created = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert created.message_type is MessageType.RESPONSE
        connection = created.payload["connection"]
        assert connection["display_name"] == "prod"

        get_connection = frame(
            3,
            {
                "method": "connections.get",
                "params": {"connection_id": connection["connection_id"]},
            },
        )
        await transport.input.put([get_connection])
        fetched = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert fetched.payload == {"connection": connection}

        listing = frame(4, {"method": "connections.list", "params": {}})
        await transport.input.put([listing])
        listed = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert listed.payload["connections"] == [connection]

        key_bytes = b"ssh-ed25519 AAAAtest"
        confirm = frame(
            5,
            {
                "method": "host_key.confirm",
                "params": {
                    "connection_id": connection["connection_id"],
                    "host": connection["host"],
                    "port": connection["port"],
                    "key_algorithm": "ssh-ed25519",
                    "fingerprint_sha256": "SHA256:test",
                    "public_key_openssh_b64": base64.b64encode(key_bytes).decode("ascii"),
                },
            },
        )
        await transport.input.put([confirm])
        trusted = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert trusted.message_type is MessageType.RESPONSE
        assert trusted.payload["host_key"]["fingerprint_sha256"] == "SHA256:test"

        updated_params = profile_params("prod-updated")
        updated_params["connection_id"] = connection["connection_id"]
        update = frame(
            6,
            {"method": "connections.update", "params": updated_params},
        )
        await transport.input.put([update])
        updated = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert updated.message_type is MessageType.RESPONSE
        assert updated.payload["connection"]["display_name"] == "prod-updated"

        replacement_key = b"ssh-ed25519 AAAAreplacement"
        replace = frame(
            7,
            {
                "method": "host_key.replace",
                "params": {
                    "connection_id": connection["connection_id"],
                    "host": "prod-updated.example",
                    "port": 22,
                    "key_algorithm": "ssh-ed25519",
                    "fingerprint_sha256": "SHA256:replacement",
                    "public_key_openssh_b64": base64.b64encode(replacement_key).decode(
                        "ascii"
                    ),
                    "expected_old_fingerprint": "SHA256:test",
                },
            },
        )
        await transport.input.put([replace])
        replaced = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert replaced.message_type is MessageType.RESPONSE
        assert (
            replaced.payload["host_key"]["fingerprint_sha256"]
            == "SHA256:replacement"
        )

        delete = frame(
            8,
            {
                "method": "connections.delete",
                "params": {"connection_id": connection["connection_id"]},
            },
        )
        await transport.input.put([delete])
        deleted = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert deleted.payload == {"deleted": True}

        shutdown = frame(9, {"method": "shutdown"})
        await transport.input.put([shutdown])
        assert (await transport.output.get()).payload == {"result": "stopping"}
        assert await running == 0

    asyncio.run(scenario())


def test_connection_handler_rejects_invalid_json_payload_without_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        transport = MemoryTransport()
        service = SidecarService(transport)
        running = asyncio.create_task(service.run())
        await transport.output.get()
        await transport.input.put(
            [
                frame(
                    1,
                    {
                        "method": "initialize",
                        "app_version": "0.1.0",
                        "runtime_db_path": str((tmp_path / "runtime.sqlite3").resolve()),
                        "runtime_data_key_b64": base64.b64encode(b"d" * 32).decode("ascii"),
                        "audit_hmac_key_b64": base64.b64encode(b"a" * 32).decode("ascii"),
                        "heartbeat_interval_ms": 5_000,
                        "heartbeat_timeout_ms": 15_000,
                    },
                    secret=True,
                )
            ]
        )
        await transport.output.get()

        invalid = profile_params("bad")
        invalid["port"] = 0
        await transport.input.put(
            [frame(2, {"method": "connections.create", "params": invalid})]
        )
        rejected = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert rejected.message_type is MessageType.ERROR
        assert rejected.payload["error_code"] == "INVALID_REQUEST_PAYLOAD"

        await transport.input.put([frame(3, {"method": "connections.list", "params": {}})])
        listed = await asyncio.wait_for(transport.output.get(), timeout=1)
        assert listed.payload == {"connections": []}

        await transport.input.put([frame(4, {"method": "shutdown"})])
        await transport.output.get()
        assert await running == 0

    asyncio.run(scenario())
