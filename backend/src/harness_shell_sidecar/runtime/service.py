"""Top-level Sidecar runtime service."""

from __future__ import annotations

import logging
import sys
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

from harness_shell_sidecar.protocol import ProtocolViolation
from harness_shell_sidecar.storage import (
    AuditEvent,
    AuditLedger,
    EncryptedRecordStore,
    LocalTraceStore,
    RuntimeDatabase,
)
from harness_shell_sidecar.telemetry import build_local_tracer_provider

from .messages import (
    InitializeRequestPayload,
    RuntimeInitializationFailure,
    RuntimePhase,
)
from .router import Router
from .stdio import StdioTransport


LOGGER = logging.getLogger("harness_shell_sidecar.runtime")


class SidecarService:
    def __init__(self, transport: StdioTransport, router: Router | None = None) -> None:
        self._transport = transport
        self._database: RuntimeDatabase | None = None
        self._record_store: EncryptedRecordStore | None = None
        self._audit_ledger: AuditLedger | None = None
        self._trace_provider = None
        self._tracer = None
        self._ready_recorded = False
        self._correlation_id = uuid4()
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
                    if self._router.phase is RuntimePhase.READY:
                        self._record_ready()
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
        audit_ledger: AuditLedger | None = None
        trace_provider = None
        try:
            database = RuntimeDatabase.open(payload.runtime_db_path)
            record_store = EncryptedRecordStore(database, data_key)
            trace_provider = build_local_tracer_provider(LocalTraceStore(database))
            tracer = trace_provider.get_tracer("harness_shell_sidecar.runtime")
            with tracer.start_as_current_span("runtime.starting") as span:
                span.set_attribute("runtime.state", "HANDSHAKING")
            audit_ledger = AuditLedger(database, audit_hmac_key)
            with tracer.start_as_current_span("audit.verify") as span:
                span.set_attribute("db.operation", "audit_verify")
                verification = audit_ledger.verify_chain()
                if not verification.valid:
                    span.set_attribute("error.code", "AUDIT_CHAIN_INVALID")
                    span.set_status(Status(StatusCode.ERROR))
                    raise RuntimeInitializationFailure(
                        "AUDIT_CHAIN_INVALID", "audit chain verification failed"
                    )
            with tracer.start_as_current_span("storage.self_check") as span:
                span.set_attribute("db.operation", "self_check")
                span.set_attribute("runtime.state", "HANDSHAKING")
            audit_ledger.append(
                AuditEvent.runtime_started(correlation_id=self._correlation_id)
            )
        except RuntimeInitializationFailure:
            _zeroize(data_key)
            _zeroize(audit_hmac_key)
            if record_store is not None:
                record_store.zeroize()
            if audit_ledger is not None:
                audit_ledger.zeroize()
            if trace_provider is not None:
                trace_provider.shutdown()
            if database is not None:
                database.close()
            raise
        except Exception:
            _zeroize(data_key)
            _zeroize(audit_hmac_key)
            if record_store is not None:
                record_store.zeroize()
            if audit_ledger is not None:
                audit_ledger.zeroize()
            if trace_provider is not None:
                trace_provider.shutdown()
            if database is not None:
                database.close()
            raise

        self._database = database
        self._record_store = record_store
        self._audit_ledger = audit_ledger
        self._trace_provider = trace_provider
        self._tracer = tracer

    def _record_ready(self) -> None:
        if self._ready_recorded:
            return
        if self._audit_ledger is None or self._tracer is None:
            raise RuntimeError("runtime observability is not initialized")
        self._audit_ledger.append(
            AuditEvent.sidecar_ready(correlation_id=self._correlation_id)
        )
        with self._tracer.start_as_current_span("runtime.ready") as span:
            span.set_attribute("runtime.state", "READY")
        self._ready_recorded = True

    def _close_runtime(self) -> None:
        if self._audit_ledger is not None:
            if self._router.phase is RuntimePhase.STOPPING:
                self._audit_ledger.append(
                    AuditEvent.runtime_stopped(correlation_id=self._correlation_id)
                )
            elif self._router.phase is RuntimePhase.FAILED:
                self._audit_ledger.append(
                    AuditEvent.runtime_failed(
                        correlation_id=self._correlation_id,
                        error_code="SIDECAR_RUNTIME_FAILED",
                    )
                )
        if self._trace_provider is not None:
            self._trace_provider.force_flush()
            self._trace_provider.shutdown()
            self._trace_provider = None
            self._tracer = None
        if self._record_store is not None:
            self._record_store.zeroize()
            self._record_store = None
        if self._audit_ledger is not None:
            self._audit_ledger.zeroize()
            self._audit_ledger = None
        if self._database is not None:
            self._database.close()
            self._database = None


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
