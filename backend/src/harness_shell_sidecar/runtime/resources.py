"""Single owner for the complete initialized Sidecar runtime resource graph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from opentelemetry.trace import Status, StatusCode

from harness_shell_sidecar.agent import (
    AgentService,
    ApiConfigRepository,
    ContextService,
    ConversationRepository,
    ModelGateway,
    SshCommandExecutor,
    register_agent_handlers,
)
from harness_shell_sidecar.connections.handlers import register_connection_handlers
from harness_shell_sidecar.connections.repository import ConnectionRepository
from harness_shell_sidecar.manual_sftp.handlers import (
    ManualSftpApplication,
    register_manual_sftp_handlers,
)
from harness_shell_sidecar.manual_sftp.service import ManualSftpService
from harness_shell_sidecar.ssh.handlers import register_ssh_handlers
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.storage import (
    AuditEvent,
    AuditLedger,
    EncryptedRecordStore,
    LocalTraceStore,
    RuntimeDatabase,
)
from harness_shell_sidecar.telemetry import build_local_tracer_provider
from harness_shell_sidecar.terminal import PtyManager
from harness_shell_sidecar.terminal.handlers import register_terminal_handlers

from .dispatcher import RequestDispatcher
from .models import RuntimeInitializationFailure
from .models import RuntimeInitializeRequest, RuntimePhase


EventSink = Callable[[dict[str, object]], Awaitable[None]]


class RuntimeResources:
    """Own initialization, domain dispatch, convergence, keys, and persistence."""

    def __init__(
        self,
        *,
        dispatcher: RequestDispatcher,
        database: RuntimeDatabase,
        connection_repository: ConnectionRepository,
        ssh_runtime: SshRuntime,
        pty_manager: PtyManager,
        manual_sftp_service: ManualSftpService,
        manual_sftp_application: ManualSftpApplication,
        agent_api_configs: ApiConfigRepository,
        agent_service: AgentService,
        record_store: EncryptedRecordStore,
        audit_ledger: AuditLedger,
        trace_provider: Any,
        tracer: Any,
        runtime_data_key: bytearray,
        audit_hmac_key: bytearray,
        correlation_id: UUID,
    ) -> None:
        """Publish a fully verified resource graph; partial graphs never escape."""

        self.dispatcher = dispatcher  # Shared transport-independent operation router.
        self.database = database  # Shared schema-v4 SQLite owner.
        self.connection_repository = connection_repository  # Connection persistence.
        self.ssh_runtime = ssh_runtime  # SSH sessions and Host Key control plane.
        self.pty_manager = pty_manager  # Interactive PTY channel owner.
        self.manual_sftp_service = manual_sftp_service  # User-operated SFTP owner.
        self.manual_sftp_application = manual_sftp_application  # Raw-byte boundary.
        self.agent_api_configs = agent_api_configs  # Non-secret Provider config owner.
        self.agent_service = agent_service  # Per-turn experimental Agent orchestrator.
        self.record_store = record_store  # Authenticated encrypted records.
        self.audit_ledger = audit_ledger  # HMAC audit-chain owner.
        self.trace_provider = trace_provider  # Local-only OpenTelemetry provider.
        self.tracer = tracer  # Runtime tracer from the local provider.
        self._runtime_data_key = runtime_data_key  # Mutable buffer for final zeroize.
        self._audit_hmac_key = audit_hmac_key  # Mutable buffer for final zeroize.
        self.correlation_id = correlation_id  # One identifier per resource graph.
        self.state = RuntimePhase.READY  # Published graphs are always fully ready.
        self._ready_recorded = False  # Prevent duplicate ready audit events.
        self._shutdown_started = False  # Make resource convergence exactly once.

    @classmethod
    def initialize(
        cls,
        payload: RuntimeInitializeRequest,
        event_sink: EventSink,
        *,
        dispatcher: RequestDispatcher | None = None,
    ) -> RuntimeResources:
        """Build and verify every runtime resource before publishing the graph."""

        runtime_dispatcher = dispatcher or RequestDispatcher()
        runtime_data_key = bytearray(payload.runtime_data_key())
        audit_hmac_key = bytearray(payload.audit_hmac_key())
        correlation_id = uuid4()
        database: RuntimeDatabase | None = None
        record_store: EncryptedRecordStore | None = None
        audit_ledger: AuditLedger | None = None
        trace_provider: Any = None

        async def publish_connection_status(status: Any) -> None:
            await event_sink(
                {
                    "event": "ssh.connection.status",
                    "status": status.model_dump(mode="json"),
                }
            )

        try:
            database = RuntimeDatabase.open(payload.runtime_db_path)
            connection_repository = ConnectionRepository(database)
            register_connection_handlers(runtime_dispatcher, connection_repository)
            record_store = EncryptedRecordStore(database, runtime_data_key)
            trace_provider = build_local_tracer_provider(LocalTraceStore(database))
            tracer = trace_provider.get_tracer("harness_shell_sidecar.runtime")
            with tracer.start_as_current_span("runtime.starting") as span:
                span.set_attribute("runtime.state", RuntimePhase.INITIALIZING.value)
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
                span.set_attribute("runtime.state", RuntimePhase.INITIALIZING.value)
            audit_ledger.append(
                AuditEvent.runtime_started(correlation_id=correlation_id)
            )

            ssh_runtime = SshRuntime(
                connection_repository,
                audit_ledger=audit_ledger,
                tracer=tracer,
                status_listener=publish_connection_status,
            )
            register_ssh_handlers(runtime_dispatcher, ssh_runtime)
            pty_manager = PtyManager(
                ssh_runtime.sessions,
                event_listener=event_sink,
            )
            register_terminal_handlers(runtime_dispatcher, pty_manager)
            manual_sftp_service = ManualSftpService(
                ssh_runtime.sessions,
                record_store,
                event_listener=event_sink,
            )
            register_manual_sftp_handlers(runtime_dispatcher, manual_sftp_service)
            manual_sftp_application = ManualSftpApplication(manual_sftp_service)

            api_configs = ApiConfigRepository(database)
            conversations = ConversationRepository(database, record_store)
            executor = SshCommandExecutor(ssh_runtime.sessions)
            gateway = ModelGateway()
            context = ContextService(conversations)
            agent_service = AgentService(
                api_configs,
                conversations,
                executor,
                gateway,
                context,
                ssh_runtime.sessions.is_connected,
            )
            register_agent_handlers(runtime_dispatcher, api_configs, agent_service)
        except BaseException:
            # No asynchronous domain resources can exist before publication: all
            # SSH/SFTP/PTY sessions are opened only by later dispatched requests.
            _zeroize(runtime_data_key)
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

        return cls(
            dispatcher=runtime_dispatcher,
            database=database,
            connection_repository=connection_repository,
            ssh_runtime=ssh_runtime,
            pty_manager=pty_manager,
            manual_sftp_service=manual_sftp_service,
            manual_sftp_application=manual_sftp_application,
            agent_api_configs=api_configs,
            agent_service=agent_service,
            record_store=record_store,
            audit_ledger=audit_ledger,
            trace_provider=trace_provider,
            tracer=tracer,
            runtime_data_key=runtime_data_key,
            audit_hmac_key=audit_hmac_key,
            correlation_id=correlation_id,
        )

    @property
    def runtime_data_key_zeroized(self) -> bool:
        """Expose only whether the runtime-data key buffer has been wiped."""

        return all(value == 0 for value in self._runtime_data_key)

    @property
    def audit_hmac_key_zeroized(self) -> bool:
        """Expose only whether the audit HMAC key buffer has been wiped."""

        return all(value == 0 for value in self._audit_hmac_key)

    def record_ready(self) -> None:
        """Persist the Sidecar ready boundary exactly once after transport readiness."""

        if self._ready_recorded:
            return
        self.audit_ledger.append(
            AuditEvent.runtime_ready(correlation_id=self.correlation_id)
        )
        with self.tracer.start_as_current_span("runtime.ready") as span:
            span.set_attribute("runtime.state", RuntimePhase.READY.value)
        self._ready_recorded = True

    async def shutdown(self) -> None:
        """Converge every owner in order while retaining the first cleanup error."""

        if self._shutdown_started:
            return
        self._shutdown_started = True
        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        self.state = RuntimePhase.DRAINING
        try:
            await self.dispatcher.close()
        except BaseException as exc:
            remember(exc)

        # Dispatcher convergence guarantees no Agent handler can still use these
        # references when the remote channel owners start closing.
        self.agent_service = None  # type: ignore[assignment]
        self.agent_api_configs = None  # type: ignore[assignment]
        self.state = RuntimePhase.CONVERGING
        for owner in (
            self.pty_manager,
            self.manual_sftp_service,
            self.ssh_runtime,
        ):
            try:
                await owner.close_all()
            except BaseException as exc:
                remember(exc)

        self.state = RuntimePhase.CLOSING
        try:
            self.audit_ledger.append(
                AuditEvent.runtime_stopped(correlation_id=self.correlation_id)
            )
        except BaseException as exc:
            remember(exc)
        try:
            self.trace_provider.force_flush()
        except BaseException as exc:
            remember(exc)
        try:
            self.trace_provider.shutdown()
        except BaseException as exc:
            remember(exc)
        try:
            self.record_store.zeroize()
        except BaseException as exc:
            remember(exc)
        try:
            self.audit_ledger.zeroize()
        except BaseException as exc:
            remember(exc)
        _zeroize(self._runtime_data_key)
        _zeroize(self._audit_hmac_key)
        try:
            self.database.close()
        except BaseException as exc:
            remember(exc)

        self.state = (
            RuntimePhase.STOPPED if first_error is None else RuntimePhase.FAILED
        )
        if first_error is not None:
            raise first_error


def _zeroize(value: bytearray) -> None:
    """Overwrite a mutable secret buffer in place."""

    for index in range(len(value)):
        value[index] = 0
