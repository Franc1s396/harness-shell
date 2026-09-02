"""Single-owner typed Runtime WebSocket gateway and domain event converter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import WebSocket
from pydantic import TypeAdapter, ValidationError
from starlette.websockets import WebSocketDisconnect

from harness_shell_sidecar.manual_sftp.models import MutationProgressProjection
from harness_shell_sidecar.runtime.dispatcher import DispatchError
from harness_shell_sidecar.ssh.errors import ConnectionStatus
from harness_shell_sidecar.terminal.manager import PtyManagerError

from .models import (
    PtyClosedMessage,
    PtyClosedPayload,
    PtyInputMessage,
    PtyInputResultMessage,
    PtyInputResultPayload,
    PtyOutputMessage,
    PtyOutputPayload,
    RuntimeClientMessage,
    RuntimePingMessage,
    RuntimePongMessage,
    RuntimePongPayload,
    RuntimeServerMessage,
    SftpOperationProgressMessage,
    SshConnectionStateMessage,
)


if TYPE_CHECKING:
    from harness_shell_sidecar.runtime.resources import RuntimeResources


WEBSOCKET_QUEUE_CAPACITY = 64
MAX_WEBSOCKET_TEXT_BYTES = 65_536
HEARTBEAT_TIMEOUT_SECONDS = 15.0
CONTRACT_CLOSE_CODE = 4400
OWNER_CONFLICT_CLOSE_CODE = 4409
RUNTIME_NOT_READY_CLOSE_CODE = 4403
HEARTBEAT_TIMEOUT_CLOSE_CODE = 4408

_CLIENT_ADAPTER = TypeAdapter(RuntimeClientMessage)


def _model_from_json(value: object, model):
    """Validate raw domain JSON through strict models without Python coercion."""

    return model.model_validate_json(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )


def _message_fields() -> dict[str, object]:
    """Create the common fields for one unsolicited server event."""

    return {
        "schema_version": 1,
        "message_id": uuid4(),
        "causation_id": None,
        "timestamp": datetime.now(timezone.utc),
    }


def convert_domain_event(event: dict[str, object]) -> RuntimeServerMessage:
    """Convert exactly one allowlisted current domain event to its final WS type."""

    event_name = event.get("event")
    if event_name == "ssh.connection.status":
        if set(event) != {"event", "status"}:
            raise ValueError("SSH status event fields are invalid")
        status = _model_from_json(event["status"], ConnectionStatus)
        return SshConnectionStateMessage(
            type="ssh.connection_state",
            payload=status,
            **_message_fields(),
        )
    if event_name == "ssh.pty.output":
        if set(event) != {
            "event",
            "pty_session_id",
            "stream_sequence",
            "data_b64",
        }:
            raise ValueError("PTY output event fields are invalid")
        payload = _model_from_json(
            {key: value for key, value in event.items() if key != "event"},
            PtyOutputPayload,
        )
        return PtyOutputMessage(
            type="pty.output",
            payload=payload,
            **_message_fields(),
        )
    if event_name == "ssh.pty.closed":
        if set(event) != {
            "event",
            "pty_session_id",
            "exit_status",
            "exit_signal",
        }:
            raise ValueError("PTY closed event fields are invalid")
        payload = _model_from_json(
            {key: value for key, value in event.items() if key != "event"},
            PtyClosedPayload,
        )
        return PtyClosedMessage(
            type="pty.closed",
            payload=payload,
            **_message_fields(),
        )
    if event_name == "manual_sftp.operation.progress":
        payload = _model_from_json(
            {key: value for key, value in event.items() if key != "event"},
            MutationProgressProjection,
        )
        return SftpOperationProgressMessage(
            type="sftp.operation_progress",
            payload=payload,
            **_message_fields(),
        )
    raise ValueError("domain event type is not allowlisted")


class RuntimeWebSocketGateway:
    """Own the single Desktop WebSocket and two bounded message queues."""

    def __init__(
        self,
        *,
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        """Create disconnected inbound/outbound queues with capacity 64."""

        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat timeout must be positive")
        self._inbound: asyncio.Queue[str] = asyncio.Queue(
            maxsize=WEBSOCKET_QUEUE_CAPACITY
        )
        self._outbound: asyncio.Queue[RuntimeServerMessage] = asyncio.Queue(
            maxsize=WEBSOCKET_QUEUE_CAPACITY
        )
        self._owner_lock = asyncio.Lock()
        self._connected = False
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds

    async def claim(self) -> bool:
        """Claim the unique active connection without replacing its owner."""

        async with self._owner_lock:
            if self._connected:
                return False
            self._connected = True
            return True

    async def release(self) -> None:
        """Release the connection and discard only its unread inbound messages."""

        async with self._owner_lock:
            self._connected = False
            while not self._inbound.empty():
                self._inbound.get_nowait()
                self._inbound.task_done()

    async def publish(self, message: RuntimeServerMessage) -> None:
        """Apply backpressure until the active Desktop consumes the event."""

        await self._outbound.put(message)

    async def publish_domain_event(self, event: dict[str, object]) -> None:
        """Validate and convert one raw manager event before queueing it."""

        await self.publish(convert_domain_event(event))

    async def next_outbound(self) -> RuntimeServerMessage:
        """Return the next typed server message in exact queue order."""

        return await self._outbound.get()

    async def run(self, websocket: WebSocket, resources: RuntimeResources) -> None:
        """Run receiver, processor, sender, and heartbeat until one terminates."""

        heartbeat = asyncio.Event()
        tasks = {
            asyncio.create_task(self._receive(websocket)),
            asyncio.create_task(self._process(websocket, resources, heartbeat)),
            asyncio.create_task(self._send(websocket)),
            asyncio.create_task(self._watch_heartbeat(websocket, heartbeat)),
        }
        done: set[asyncio.Task[None]] = set()
        try:
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # ASGI server shutdown may cancel the endpoint before a child task
            # wins FIRST_COMPLETED. The gateway still owns and joins all four.
            pending = tasks - done
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            try:
                task.result()
            except WebSocketDisconnect:
                pass

    async def _receive(self, websocket: WebSocket) -> None:
        """Read only bounded UTF-8 text messages and apply inbound backpressure."""

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not isinstance(text, str):
                await websocket.close(code=CONTRACT_CLOSE_CODE)
                return
            if len(text.encode("utf-8")) > MAX_WEBSOCKET_TEXT_BYTES:
                await websocket.close(code=1009)
                return
            await self._inbound.put(text)

    async def _process(
        self,
        websocket: WebSocket,
        resources: RuntimeResources,
        heartbeat: asyncio.Event,
    ) -> None:
        """Validate client messages and keep stable domain failures connected."""

        while True:
            encoded = await self._inbound.get()
            try:
                try:
                    message = _CLIENT_ADAPTER.validate_json(encoded)
                except (ValueError, ValidationError):
                    await websocket.close(code=CONTRACT_CLOSE_CODE)
                    return
                if isinstance(message, RuntimePingMessage):
                    heartbeat.set()
                    await self.publish(
                        RuntimePongMessage(
                            schema_version=1,
                            type="runtime.pong",
                            message_id=uuid4(),
                            causation_id=message.message_id,
                            timestamp=datetime.now(timezone.utc),
                            payload=RuntimePongPayload(
                                server_timestamp=datetime.now(timezone.utc)
                            ),
                        )
                    )
                elif isinstance(message, PtyInputMessage):
                    await self._write_pty(resources, message)
            finally:
                self._inbound.task_done()

    async def _write_pty(
        self,
        resources: RuntimeResources,
        message: PtyInputMessage,
    ) -> None:
        """Write one PTY chunk under dispatcher ownership and correlate the result."""

        data = message.payload.decoded_data()

        async def work(_context) -> None:
            await resources.pty_manager.write(message.payload.pty_session_id, data)

        error_code: str | None = None
        try:
            await resources.dispatcher.execute(message.message_id, work)
        except PtyManagerError as error:
            error_code = error.error_code
        except DispatchError as error:
            error_code = error.error_code
        await self.publish(
            PtyInputResultMessage(
                schema_version=1,
                type="pty.input_result",
                message_id=uuid4(),
                causation_id=message.message_id,
                timestamp=datetime.now(timezone.utc),
                payload=PtyInputResultPayload(
                    pty_session_id=message.payload.pty_session_id,
                    accepted_bytes=0 if error_code is not None else len(data),
                    error_code=error_code,
                ),
            )
        )

    async def _send(self, websocket: WebSocket) -> None:
        """Serialize typed messages in queue order without dropping or merging."""

        while True:
            message = await self._outbound.get()
            try:
                await websocket.send_text(message.model_dump_json())
            finally:
                self._outbound.task_done()

    async def _watch_heartbeat(
        self,
        websocket: WebSocket,
        heartbeat: asyncio.Event,
    ) -> None:
        """Require explicit ping messages; other traffic never refreshes liveness."""

        while True:
            try:
                await asyncio.wait_for(
                    heartbeat.wait(),
                    timeout=self._heartbeat_timeout_seconds,
                )
            except TimeoutError:
                await websocket.close(code=HEARTBEAT_TIMEOUT_CLOSE_CODE)
                return
            heartbeat.clear()


async def runtime_websocket_endpoint(websocket: WebSocket) -> None:
    """Accept only one ready Runtime WebSocket for the ASGI process."""

    from .lifespan import RuntimeOwnerError

    owner = websocket.app.state.runtime_owner
    try:
        resources = owner.require_resources()
    except RuntimeOwnerError:
        await websocket.accept()
        await websocket.close(code=RUNTIME_NOT_READY_CLOSE_CODE)
        return
    gateway: RuntimeWebSocketGateway = owner.websocket_gateway
    if not await gateway.claim():
        await websocket.accept()
        await websocket.close(code=OWNER_CONFLICT_CLOSE_CODE)
        return
    await websocket.accept()
    try:
        try:
            await gateway.run(websocket, resources)
        except asyncio.CancelledError:
            # Endpoint cancellation is the ASGI server's connection-close
            # signal. Internal gateway tasks were already joined above.
            return
    finally:
        await gateway.release()


__all__ = [
    "RuntimeWebSocketGateway",
    "convert_domain_event",
    "runtime_websocket_endpoint",
]
