"""Top-level Sidecar runtime service."""

from __future__ import annotations

import logging
import sys

from harness_shell_sidecar.protocol import ProtocolViolation
from harness_shell_sidecar.storage import EncryptedRecordStore, RuntimeDatabase

from .messages import InitializeRequestPayload, RuntimePhase
from .router import Router
from .stdio import StdioTransport


LOGGER = logging.getLogger("harness_shell_sidecar.runtime")


class SidecarService:
    def __init__(self, transport: StdioTransport, router: Router | None = None) -> None:
        self._transport = transport
        self._database: RuntimeDatabase | None = None
        self._record_store: EncryptedRecordStore | None = None
        self._audit_hmac_key: bytearray | None = None
        self._router = router or Router(initializer=self._initialize_runtime)

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
                    if self._router.phase is RuntimePhase.FAILED:
                        exit_code = 1
                        break
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
            self._close_runtime()
            self._router.mark_stopped()
        return exit_code

    def _initialize_runtime(self, payload: InitializeRequestPayload) -> None:
        if self._database is not None:
            raise RuntimeError("runtime storage is already initialized")

        data_key = bytearray(payload.runtime_data_key())
        audit_hmac_key = bytearray(payload.audit_hmac_key())
        database: RuntimeDatabase | None = None
        record_store: EncryptedRecordStore | None = None
        try:
            database = RuntimeDatabase.open(payload.runtime_db_path)
            record_store = EncryptedRecordStore(database, data_key)
        except Exception:
            _zeroize(data_key)
            _zeroize(audit_hmac_key)
            if database is not None:
                database.close()
            raise

        self._database = database
        self._record_store = record_store
        self._audit_hmac_key = audit_hmac_key

    def _close_runtime(self) -> None:
        if self._record_store is not None:
            self._record_store.zeroize()
            self._record_store = None
        if self._audit_hmac_key is not None:
            _zeroize(self._audit_hmac_key)
            self._audit_hmac_key = None
        if self._database is not None:
            self._database.close()
            self._database = None


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
