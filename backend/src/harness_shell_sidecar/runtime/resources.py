"""完整已初始化 Sidecar 运行时资源图的唯一管理者。"""

from __future__ import annotations

from harness_shell_sidecar.agent.context_models import AgentContextPolicy, ContextError
from harness_shell_sidecar.agent.context_budget import ContextBudget
from harness_shell_sidecar.agent.tokenizer import load_local_encoding, tokenizer_resource_dir

from collections.abc import Awaitable, Callable
from typing import Any

from harness_shell_sidecar.agent import (
    AgentService,
    AgentTurnApplication,
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
    """负责初始化、领域派发、收敛、密钥与持久化。"""

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
        agent_turn_application: AgentTurnApplication,
        record_store: PlaintextRecordStore,
        credential_repository: CredentialRepository,
        credential_cipher: RuntimeCredentialCipher,
    ) -> None:
        """只发布完全验证的资源图，不暴露部分初始化状态。"""

        self.dispatcher = dispatcher  # 共享且独立于传输的操作路由器。
        self.database = database  # 共享 schema v7 SQLite 管理者。
        self.connection_repository = connection_repository  # 连接持久化仓库。
        self.ssh_runtime = ssh_runtime  # SSH 会话和 Host Key 控制面。
        self.pty_manager = pty_manager  # 交互式 PTY 通道管理者。
        self.manual_sftp_service = manual_sftp_service  # 用户手动 SFTP 管理者。
        self.manual_sftp_application = manual_sftp_application  # 原始字节边界。
        self.agent_api_configs = agent_api_configs  # 非秘密 Provider 配置管理者。
        self.agent_service = agent_service  # 按轮执行的实验性 Agent 编排器。
        self.agent_turn_application = agent_turn_application  # 流式应用边界。
        self.record_store = record_store  # 领域存储共用的通用记录管理者。
        self.credential_repository = credential_repository  # 明文秘密管理者。
        self.credential_cipher = credential_cipher  # 临时传输 RSA 密钥管理者。
        self.state = RuntimePhase.READY  # 发布的资源图必须完全就绪。
        self._shutdown_started = False  # 确保资源收敛仅执行一次。

    @classmethod
    def initialize_from_settings(
        cls,
        settings: RuntimeSettings,
        event_sink: EventSink,
        *,
        dispatcher: RequestDispatcher | None = None,
    ) -> RuntimeResources:
        """构建目标明文资源图，不注入密钥。"""

        # 1. 准备唯一 dispatcher；资源图完整验证前不向调用方发布。
        runtime_dispatcher = dispatcher or RequestDispatcher()
        database: RuntimeDatabase | None = None

        async def publish_connection_status(status: Any) -> None:
            """通过 Runtime 事件接收端投影已校验的 SSH 状态。"""

            await event_sink(
                {
                    "event": "ssh.connection.status",
                    "status": status.model_dump(mode="json"),
                }
            )

        try:
            # 2. 建立固定目录并打开严格 schema 数据库，再绑定凭据与连接仓库。
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
            # 3. 按 SSH、PTY、手动 SFTP 的依赖顺序建立远程资源管理者。
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

            # 4. 装配 Agent 仓库、离线预算和执行器，复用同一数据库与 SSH 会话。
            api_configs = ApiConfigRepository(database)
            conversations = ConversationRepository(database, record_store)
            context_policy = AgentContextPolicy()
            context_budget = ContextBudget(load_local_encoding(
                tokenizer_resource_dir(), context_policy.tokenizer_encoding), context_policy)
            executor = SshCommandExecutor(ssh_runtime.sessions, policy=context_policy)
            gateway = ModelGateway()
            context = ContextService(conversations)
            agent_service = AgentService(
                api_configs,
                conversations,
                executor,
                gateway,
                context,
                ssh_runtime.sessions.is_connected,
                budget=context_budget,
            )
            agent_turn_application = register_agent_handlers(
                runtime_dispatcher,
                api_configs,
                agent_service,
                credential_repository,
                credential_cipher,
                database,
            )
        except BaseException as exc:
            # 发布前不可能存在领域会话，因此只关闭
            # 同步构建序列中已经创建的资源管理者。
            if database is not None:
                database.close()
            if isinstance(exc, ContextError):
                raise RuntimeInitializationFailure(exc.error_code, exc.safe_message) from exc
            if isinstance(exc, RuntimeInitializationFailure):
                raise
            raise RuntimeInitializationFailure(
                "RUNTIME_INITIALIZATION_FAILED",
                "Runtime initialization failed",
            ) from exc

        # 5. 所有构造与校验成功后才返回完整就绪资源图。
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
            agent_turn_application=agent_turn_application,
            record_store=record_store,
            credential_repository=credential_repository,
            credential_cipher=credential_cipher,
        )

    async def shutdown(self) -> None:
        """按顺序收敛全部管理者，同时保留首个清理错误。"""

        # 1. 封闭重复关闭入口，整个收敛流程只执行一次。
        if self._shutdown_started:
            return
        self._shutdown_started = True
        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        # 2. 先停止 dispatcher 并等待活动请求退出，避免远程资源仍被使用。
        self.state = RuntimePhase.DRAINING
        try:
            await self.dispatcher.close()
        except BaseException as exc:
            remember(exc)

        # dispatcher 收敛后，Agent handler 不再使用这些引用，
        # 此时才开始关闭远程通道管理者。
        self.agent_service = None  # type: ignore[assignment]
        self.agent_turn_application = None  # type: ignore[assignment]
        self.agent_api_configs = None  # type: ignore[assignment]
        # 3. 再按依赖顺序关闭 SFTP、PTY 和 SSH，清理失败不阻止后续清理。
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

        # 4. 最后关闭共享数据库，依据首个失败决定 CLOSED 或 FAILED。
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
