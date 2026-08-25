"""Top-level Sidecar runtime service."""

from __future__ import annotations

import logging
import sys

from harness_shell_sidecar.protocol import ProtocolViolation

from .router import Router
from .stdio import StdioTransport


LOGGER = logging.getLogger("harness_shell_sidecar.runtime")


class SidecarService:
    def __init__(self, transport: StdioTransport, router: Router | None = None) -> None:
        self._transport = transport
        self._router = router or Router()

    @classmethod
    def for_stdio(cls) -> SidecarService:
        return cls(StdioTransport(sys.stdin.buffer, sys.stdout.buffer))

    async def run(self) -> int:
        self._transport.start()
        exit_code = 0
        try:
            await self._transport.send(self._router.ready_event())
            while not self._router.should_stop:
                try:
                    frames = await self._transport.read()
                except ProtocolViolation:
                    await self._transport.send(
                        self._router.terminal_error(
                            "PROTOCOL_VIOLATION",
                            "input does not conform to protocol v1",
                        )
                    )
                    exit_code = 2
                    break

                if frames is None:
                    exit_code = 3
                    break

                for frame in frames:
                    try:
                        response = self._router.handle(frame)
                    except ProtocolViolation:
                        await self._transport.send(
                            self._router.terminal_error(
                                "SIDECAR_SEQUENCE_VIOLATION",
                                "input sequence is invalid",
                            )
                        )
                        exit_code = 2
                        break
                    await self._transport.send(response)
                    if self._router.should_stop:
                        break

                if exit_code != 0:
                    break
        except Exception:
            LOGGER.exception(
                '{"level":"ERROR","error_code":"SIDECAR_RUNTIME_FAILED"}'
            )
            exit_code = 1
        finally:
            await self._transport.close()
            self._router.mark_stopped()
        return exit_code

