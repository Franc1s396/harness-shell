"""Single owner for the complete initialized Sidecar runtime resource graph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

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
from harness_shell_sidecar.credentials import (
    CredentialRepository,
    CredentialService,
    RuntimeCredentialCipher,
)
from harness_shell_sidecar.manual_sftp.handlers import (
    ManualSftpApplication,
    register_manual_sftp_handlers,
)
from harness_shell_sidecar.manual_sftp.service import ManualSftpService
from harness_shell_sidecar.ssh.handlers import register_ssh_handlers
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.storage import PlaintextRecordStore, RuntimeDatabase
from harness_shell_sidecar.terminal import PtyManager
from harness_shell_sidecar.terminal.handlers import register_terminal_handlers

from .dispatcher import RequestDispatcher
from .models import RuntimeInitializationFailure, RuntimePhase
from .settings import RuntimeSettings


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
        record_store: PlaintextRecordStore,
        credential_repository: CredentialRepository,
        credential_cipher: RuntimeCredentialCipher,
    ) -> None:
        """Publish a fully verified resource graph; partial graphs never escape."""

        self.dispatcher = dispatcher  # Shared transport-independent operation router.
        self.database = database  # Shared schema-v6 SQLite owner.
        self.connection_repository = connection_repository  # Connection persistence.
        self.ssh_runtime = ssh_runtime  # SSH sessions and Host Key control plane.
        self.pty_manager = pty_manager  # Interactive PTY channel owner.
        self.manual_sftp_service = manual_sftp_service  # User-operated SFTP owner.
        self.manual_sftp_application = manual_sftp_application  # Raw-byte boundary.
        self.agent_api_configs = agent_api_configs  # Non-secret Provider config owner.
        self.agent_service = agent_service  # Per-turn experimental Agent orchestrator.
        self.record_store = record_store  # Generic record owner for domain stores.
        self.credential_repository = credential_repository  # Plain secret owner.
        self.credential_cipher = credential_cipher  # Ephemeral wire RSA owner.
        self.state = RuntimePhase.READY  # Published graphs are always fully ready.
        self._shutdown_started = False  # Make resource convergence exactly once.

    @classmethod
    def initialize_from_settings(
        cls,
        settings: RuntimeSettings,
        event_sink: EventSink,
        *,
        dispatcher: RequestDispatcher | None = None,
    ) -> RuntimeResources:
        """Build the target plaintext resource graph without injected keys."""

        runtime_dispatcher = dispatcher or RequestDispatcher()
        database: RuntimeDatabase | None = None

        async def publish_connection_status(status: Any) -> None:
            """Project one validated SSH status through the Runtime event sink."""

            await event_sink(
                {
                    "event": "ssh.connection.status",
                    "status": status.model_dump(mode="json"),
                }
            )

        try:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            database = RuntimeDatabase.open_plaintext(settings.database_path)
            record_store = PlaintextRecordStore(database)
            credential_repository = CredentialRepository(record_store)
            credential_cipher = RuntimeCredentialCipher.generate()
            connection_repository = ConnectionRepository(database)
            credential_service = CredentialService(
                connection_repository,
                credential_repository,
            )
            register_connection_handlers(
                runtime_dispatcher,
                connection_repository,
                credential_repository,
                credential_cipher,
                database,
            )
            ssh_runtime = SshRuntime(
                connection_repository,
                status_listener=publish_connection_status,
            )
            register_ssh_handlers(
                runtime_dispatcher,
                ssh_runtime,
                credential_service,
            )
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
            register_agent_handlers(
                runtime_dispatcher,
                api_configs,
                agent_service,
                credential_repository,
                credential_cipher,
                database,
            )
        except BaseException as exc:
            # No domain session can exist before publication; close only owners
            # already created by this synchronous construction sequence.
            if database is not None:
                database.close()
            if isinstance(exc, RuntimeInitializationFailure):
                raise
            raise RuntimeInitializationFailure(
                "RUNTIME_INITIALIZATION_FAILED",
                "Runtime initialization failed",
            ) from exc

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
            credential_repository=credential_repository,
            credential_cipher=credential_cipher,
        )

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
            self.database.close()
        except BaseException as exc:
            remember(exc)

        self.state = (
            RuntimePhase.STOPPED if first_error is None else RuntimePhase.FAILED
        )
        if first_error is not None:
            raise first_error
