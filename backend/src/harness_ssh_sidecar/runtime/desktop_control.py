"""打包版桌面 Runtime 的严格继承管道控制协议。"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from io import FileIO
from typing import BinaryIO
from uuid import UUID

import msvcrt


READY_FRAME_VERSION = 1
READY_FRAME_MAX_JSON_BYTES = 4_096
GRACEFUL_SHUTDOWN_BYTE = b"\x01"


class DesktopControlProtocolError(RuntimeError):
    """报告严格就绪或控制管道的协议违规。"""


@dataclass(frozen=True, slots=True)
class DesktopReadyFrame:
    """向 Launcher 仅发布一次的已校验就绪数据。"""

    #: Launcher 与 Sidecar 共同理解的协议版本。
    version: int
    #: 此 Sidecar 进程实例的唯一标识。
    instance_id: UUID
    #: 预绑定监听器实际选中的 loopback 端口。
    port: int


def _reject_duplicate_fields(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """构建 JSON 对象并显式拒绝重复名称。"""

    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"ready payload contains duplicate field: {name}")
        result[name] = value
    return result


def decode_ready_payload(payload: bytes) -> DesktopReadyFrame:
    """解码有界且严格的 UTF-8 JSON 就绪载荷。"""

    # 1. 限制帧长度并严格解码 UTF-8 JSON，拒绝重复字段。
    if not payload or len(payload) > READY_FRAME_MAX_JSON_BYTES:
        raise ValueError("ready payload length is outside the accepted bounds")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("ready payload must be strict UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_fields)
    except json.JSONDecodeError as error:
        raise ValueError("ready payload must be one JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("ready payload must be one JSON object")

    # 2. 精确检查字段集合，拒绝缺失字段和未知字段。
    expected_fields = {"version", "instance_id", "port"}
    unknown_fields = set(value) - expected_fields
    if unknown_fields:
        raise ValueError(
            f"ready payload contains unknown field: {sorted(unknown_fields)[0]}"
        )
    missing_fields = expected_fields - set(value)
    if missing_fields:
        raise ValueError(
            f"ready payload is missing field: {sorted(missing_fields)[0]}"
        )

    # 3. 分别校验版本、非零端口和 UUID，全部通过才构建就绪对象。
    version = value["version"]
    port = value["port"]
    instance_id = value["instance_id"]
    if type(version) is not int or version != READY_FRAME_VERSION:
        raise ValueError("ready payload version must be integer 1")
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("ready payload port must be an integer between 1 and 65535")
    if not isinstance(instance_id, str):
        raise ValueError("ready payload instance_id must be a UUID string")
    try:
        parsed_instance_id = UUID(instance_id)
    except ValueError as error:
        raise ValueError("ready payload instance_id must be a UUID string") from error
    return DesktopReadyFrame(
        version=version,
        instance_id=parsed_instance_id,
        port=port,
    )


def encode_ready_frame(*, instance_id: UUID, port: int) -> bytes:
    """编码带长度前缀的规范就绪帧。"""

    ready = DesktopReadyFrame(
        version=READY_FRAME_VERSION,
        instance_id=instance_id,
        port=port,
    )
    if not 1 <= ready.port <= 65_535:
        raise ValueError("ready port must be between 1 and 65535")
    payload = json.dumps(
        {
            "instance_id": str(ready.instance_id),
            "port": ready.port,
            "version": ready.version,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > READY_FRAME_MAX_JSON_BYTES:
        raise ValueError("ready payload exceeds the maximum encoded length")
    return struct.pack(">I", len(payload)) + payload


class DesktopControl:
    """将每个继承管道句柄转交给一个 Python fd 后管理其所有权。"""

    def __init__(
        self,
        control_read_handle: int,
        ready_write_handle: int,
    ) -> None:
        """将每个继承 Windows 句柄仅转交 Python 一次。"""

        self._control_reader: BinaryIO | None = None
        self._ready_writer: BinaryIO | None = None
        self._ready_published = False

        control_fd = msvcrt.open_osfhandle(
            control_read_handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            self._control_reader = FileIO(control_fd, mode="rb", closefd=True)
            ready_fd = msvcrt.open_osfhandle(
                ready_write_handle,
                os.O_WRONLY | os.O_BINARY,
            )
            self._ready_writer = FileIO(ready_fd, mode="wb", closefd=True)
        except BaseException:
            if self._control_reader is not None:
                self._control_reader.close()
            else:
                os.close(control_fd)
            raise

    def publish_ready(self, *, instance_id: UUID, port: int) -> None:
        """写入并刷新且仅发送一个就绪帧，再关闭管道端。"""

        writer = self._ready_writer
        if writer is None or writer.closed or self._ready_published:
            raise DesktopControlProtocolError(
                "desktop readiness may only be published once"
            )
        frame = encode_ready_frame(instance_id=instance_id, port=port)
        view = memoryview(frame)
        while view:
            written = writer.write(view)
            if written is None or written <= 0:
                raise OSError("ready pipe stopped accepting bytes")
            view = view[written:]
        writer.flush()
        self._ready_published = True
        writer.close()

    def wait_for_shutdown(self) -> None:
        """阻塞等待优雅退出字节或 EOF，拒绝其他控制字节。"""

        reader = self._control_reader
        if reader is None or reader.closed:
            raise DesktopControlProtocolError("desktop control pipe is closed")
        control = reader.read(1)
        if control in (b"", GRACEFUL_SHUTDOWN_BYTE):
            return
        raise DesktopControlProtocolError(
            f"desktop control pipe received invalid byte 0x{control.hex()}"
        )

    def close(self) -> None:
        """仅关闭 Python 拥有的描述符，且每个只关闭一次。"""

        for stream in (self._ready_writer, self._control_reader):
            if stream is not None and not stream.closed:
                stream.close()


__all__ = [
    "DesktopControl",
    "DesktopControlProtocolError",
    "DesktopReadyFrame",
    "GRACEFUL_SHUTDOWN_BYTE",
    "READY_FRAME_MAX_JSON_BYTES",
    "decode_ready_payload",
    "encode_ready_frame",
]
