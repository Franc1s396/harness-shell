"""Bounded asynchronous adapter for binary stdin/stdout."""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from harness_shell_sidecar.protocol import FrameDecoder, FrameEnvelope, encode_frame


OUTBOUND_QUEUE_CAPACITY = 64
READ_CHUNK_BYTES = 65_536


class StdioTransport:
    """在二进制 stdin/stdout 上提供有界异步协议帧传输。"""

    def __init__(self, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
        """绑定输入输出流，并创建尚未启动的读写状态。"""

        self._input = input_stream  # 从桌面控制面接收协议字节的二进制流。
        self._output = output_stream  # 向桌面控制面发送协议字节的二进制流。
        self._decoder = FrameDecoder()  # 跨 read chunk 保存未完成帧。
        # 限制等待写出的帧数量，向发送方施加背压。
        self._outbound: asyncio.Queue[FrameEnvelope | None] = asyncio.Queue(
            maxsize=OUTBOUND_QUEUE_CAPACITY
        )
        self._writer_task: asyncio.Task[None] | None = None  # 唯一 stdout 写任务。

    def start(self) -> None:
        """启动唯一的后台写循环；重复启动会直接失败。"""

        if self._writer_task is not None:
            raise RuntimeError("stdio writer already started")
        self._writer_task = asyncio.create_task(self._write_loop())

    async def read(self) -> list[FrameEnvelope] | None:
        """读取一个字节块并解码完整帧；EOF 时返回 None。"""

        read = getattr(self._input, "read1", self._input.read)
        chunk = await asyncio.to_thread(read, READ_CHUNK_BYTES)
        if chunk == b"":
            return None
        return self._decoder.feed(chunk)

    async def send(self, frame: FrameEnvelope) -> None:
        """把协议帧放入有界发送队列，并在队列满时等待。"""

        if self._writer_task is None:
            raise RuntimeError("stdio writer is not running")
        await self._outbound.put(frame)

    async def close(self) -> None:
        """发送停止哨兵、排空既有帧并等待写任务结束。"""

        if self._writer_task is None:
            return
        await self._outbound.put(None)
        await self._writer_task
        self._writer_task = None

    async def _write_loop(self) -> None:
        """串行消费发送队列，保证 stdout 帧不会交错。"""

        while True:
            frame = await self._outbound.get()
            try:
                if frame is None:
                    return
                await asyncio.to_thread(self._write_frame, frame)
            finally:
                self._outbound.task_done()

    def _write_frame(self, frame: FrameEnvelope) -> None:
        """同步编码、写出并立即刷新单个协议帧。"""

        self._output.write(encode_frame(frame))
        self._output.flush()
