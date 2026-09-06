"""单一所有者 typed Runtime WebSocket 网关与领域事件转换器。"""

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
    """通过严格模型校验原始领域 JSON，不做 Python 强制转换。"""

    return model.model_validate_json(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )


def _message_fields() -> dict[str, object]:
    """创建服务器主动事件的公共字段。"""

    return {
        "schema_version": 1,
        "message_id": uuid4(),
        "causation_id": None,
        "timestamp": datetime.now(timezone.utc),
    }


def convert_domain_event(event: dict[str, object]) -> RuntimeServerMessage:
    """将且仅将一个允许的当前领域事件转换为最终 WS 类型。"""

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
    """拥有唯一桌面 WebSocket 和两个有界消息队列。"""

    def __init__(
        self,
        *,
        heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        """创建未连接的入站和出站队列，容量为 64。"""

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
        """取得唯一活动连接，不替换已有所有者。"""

        async with self._owner_lock:
            if self._connected:
                return False
            self._connected = True
            return True

    async def release(self) -> None:
        """释放连接，并仅丢弃其未读入站消息。"""

        async with self._owner_lock:
            self._connected = False
            while not self._inbound.empty():
                self._inbound.get_nowait()
                self._inbound.task_done()

    async def publish(self, message: RuntimeServerMessage) -> None:
        """施加背压，直到活动桌面端消费事件。"""

        await self._outbound.put(message)

    async def publish_domain_event(self, event: dict[str, object]) -> None:
        """入队前校验并转换原始管理器事件。"""

        await self.publish(convert_domain_event(event))

    async def next_outbound(self) -> RuntimeServerMessage:
        """严格按队列顺序返回下一条 typed 服务端消息。"""

        return await self._outbound.get()

    async def run(self, websocket: WebSocket, resources: RuntimeResources) -> None:
        """运行接收、处理、发送和心跳任务，直到其中一个结束。"""

        # 1. 为本连接建立心跳信号，并独占接收、处理、发送和心跳四个任务。
        heartbeat = asyncio.Event()
        tasks = {
            asyncio.create_task(self._receive(websocket)),
            asyncio.create_task(self._process(websocket, resources, heartbeat)),
            asyncio.create_task(self._send(websocket)),
            asyncio.create_task(self._watch_heartbeat(websocket, heartbeat)),
        }
        done: set[asyncio.Task[None]] = set()
        try:
            # 2. 任一任务结束即进入连接收敛，不自动重连或重放消息。
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        # 3. 包括端点取消在内的所有退出路径都取消并等待其余任务。
        finally:
            # ASGI 关闭可能先于任一子任务触发 FIRST_COMPLETED 而取消端点；
            # 网关仍拥有全部四个任务，必须等待它们结束。
            pending = tasks - done
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        # 4. 回收先结束任务的结果，只将明确断连视为正常连接结束。
        for task in done:
            if task.cancelled():
                continue
            try:
                task.result()
            except WebSocketDisconnect:
                pass

    async def _receive(self, websocket: WebSocket) -> None:
        """只读取有界 UTF-8 文本消息，并施加入站背压。"""

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
        """校验客户端消息，稳定领域失败时保留连接。"""

        # 1. 按入站队列顺序严格校验消息，协议非法时关闭连接。
        while True:
            encoded = await self._inbound.get()
            try:
                try:
                    message = _CLIENT_ADAPTER.validate_json(encoded)
                except (ValueError, ValidationError):
                    await websocket.close(code=CONTRACT_CLOSE_CODE)
                    return
                # 2. 仅合法 ping 刷新心跳并回复关联 pong；PTY 输入进入共享 dispatcher。
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
            # 3. 无论处理成功、失败还是取消，都完成当前队列项的计数。
            finally:
                self._inbound.task_done()

    async def _write_pty(
        self,
        resources: RuntimeResources,
        message: PtyInputMessage,
    ) -> None:
        """在 dispatcher 所有权下写入 PTY 分块并关联结果。"""

        # 1. 对已通过严格模型校验的 Base64 输入解码，准备领域写入。
        data = message.payload.decoded_data()

        async def work(_context) -> None:
            await resources.pty_manager.write(message.payload.pty_session_id, data)

        # 2. 使用消息 ID 占用 dispatcher 容量，保留稳定 PTY 或派发失败码。
        error_code: str | None = None
        try:
            await resources.dispatcher.execute(message.message_id, work)
        except PtyManagerError as error:
            error_code = error.error_code
        except DispatchError as error:
            error_code = error.error_code
        # 3. 发布与输入关联的确认；失败时接受字节数为零，不虚构写入成功。
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
        """按队列顺序序列化 typed 消息，不丢弃或合并。"""

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
        """要求显式 ping；其他通信不刷新存活状态。"""

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
    """ASGI 进程仅接受一个就绪 Runtime WebSocket。"""

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
            # 端点取消是 ASGI 服务器的连接关闭信号；
            # 内部网关任务已在上方等待回收。
            return
    finally:
        await gateway.release()


__all__ = [
    "RuntimeWebSocketGateway",
    "convert_domain_event",
    "runtime_websocket_endpoint",
]
