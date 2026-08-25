"""Bounded asynchronous adapter for binary stdin/stdout."""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from harness_shell_sidecar.protocol import FrameDecoder, FrameEnvelope, encode_frame


OUTBOUND_QUEUE_CAPACITY = 64
READ_CHUNK_BYTES = 65_536


class StdioTransport:
    def __init__(self, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
        self._input = input_stream
        self._output = output_stream
        self._decoder = FrameDecoder()
        self._outbound: asyncio.Queue[FrameEnvelope | None] = asyncio.Queue(
            maxsize=OUTBOUND_QUEUE_CAPACITY
        )
        self._writer_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._writer_task is not None:
            raise RuntimeError("stdio writer already started")
        self._writer_task = asyncio.create_task(self._write_loop())

    async def read(self) -> list[FrameEnvelope] | None:
        read = getattr(self._input, "read1", self._input.read)
        chunk = await asyncio.to_thread(read, READ_CHUNK_BYTES)
        if chunk == b"":
            return None
        return self._decoder.feed(chunk)

    async def send(self, frame: FrameEnvelope) -> None:
        if self._writer_task is None:
            raise RuntimeError("stdio writer is not running")
        await self._outbound.put(frame)

    async def close(self) -> None:
        if self._writer_task is None:
            return
        await self._outbound.put(None)
        await self._writer_task
        self._writer_task = None

    async def _write_loop(self) -> None:
        while True:
            frame = await self._outbound.get()
            try:
                if frame is None:
                    return
                await asyncio.to_thread(self._write_frame, frame)
            finally:
                self._outbound.task_done()

    def _write_frame(self, frame: FrameEnvelope) -> None:
        self._output.write(encode_frame(frame))
        self._output.flush()

