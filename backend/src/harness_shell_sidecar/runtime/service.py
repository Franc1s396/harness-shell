"""Top-level Sidecar runtime service."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

from harness_shell_sidecar.connections.handlers import register_connection_handlers
from harness_shell_sidecar.connections.repository import ConnectionRepository
from harness_shell_sidecar.protocol import MessageType, ProtocolViolation
from harness_shell_sidecar.storage import (
    AuditEvent,
    AuditLedger,
    EncryptedRecordStore,
    LocalTraceStore,
    RuntimeDatabase,
)
from harness_shell_sidecar.telemetry import build_local_tracer_provider
from harness_shell_sidecar.ssh.handlers import register_ssh_handlers
from harness_shell_sidecar.ssh.runtime import SshRuntime
from harness_shell_sidecar.terminal import PtyManager
from harness_shell_sidecar.terminal.handlers import register_terminal_handlers

from .messages import (
    InitializeRequestPayload,
    RuntimeInitializationFailure,
    RuntimePhase,
)
from .dispatcher import DispatchError, RequestDispatcher
from .router import Router
from .stdio import StdioTransport


if TYPE_CHECKING:
    from harness_shell_sidecar.manual_sftp.service import ManualSftpService


LOGGER = logging.getLogger("harness_shell_sidecar.runtime")


class SidecarService:
    """协调传输、协议状态机、业务分发器及所有运行时资源的顶层服务。"""

    def __init__(
        self,
        transport: StdioTransport,
        router: Router | None = None,
        dispatcher: RequestDispatcher | None = None,
    ) -> None:
        """注入可测试组件，并初始化尚未握手的资源占位状态。"""

        self._transport = transport  # Sidecar 私有 stdin/stdout 帧传输。
        self._database: RuntimeDatabase | None = None  # 当前运行时 SQLite 数据库。
        # 连接配置和 Host Key 的持久化入口。
        self._connection_repository: ConnectionRepository | None = None
        self._ssh_runtime: SshRuntime | None = None  # SSH 连接与会话生命周期管理器。
        self._pty_manager: PtyManager | None = None  # 交互式 PTY channel 管理器。
        # 仅由用户显式操作、绑定活动 SSH Session 的 SFTP 所有者。
        self._manual_sftp_service: ManualSftpService | None = None
        # 持有运行时数据加密 Key 的认证加密记录仓储。
        self._record_store: EncryptedRecordStore | None = None
        self._audit_ledger: AuditLedger | None = None  # HMAC 链式审计账本。
        self._trace_provider = None  # 仅写本地数据库的 OpenTelemetry Provider。
        self._tracer = None  # 当前运行时模块使用的 Tracer。
        self._ready_recorded = False  # 防止 ready 审计与 Span 被重复写入。
        self._correlation_id = uuid4()  # 关联当前 Sidecar 进程生命周期的标识符。
        # 协议状态机；默认把敏感 initialize 负载交给本服务建立资源。
        self._router = router or Router(initializer=self._initialize_runtime)
        self._dispatcher = dispatcher or RequestDispatcher()  # 有界应用请求分发器。
        # 与主帧读取循环并行执行的业务请求任务集合。
        self._application_tasks: set[asyncio.Task[None]] = set()

    @classmethod
    def for_stdio(cls) -> SidecarService:
        """使用当前进程的二进制 stdin/stdout 创建生产服务实例。"""

        return cls(StdioTransport(sys.stdin.buffer, sys.stdout.buffer))

    async def run(self) -> int:
        """运行帧读取、路由和资源收敛循环，并返回稳定进程退出码。"""

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
                        if self._is_application_request(frame):
                            self._router.validate_inbound(frame)
                            self._start_application_request(frame)
                            continue
                        if frame.message_type is MessageType.CANCEL:
                            self._router.validate_inbound(frame)
                            response = await self._handle_cancel(frame)
                        elif frame.message_type is MessageType.REQUEST and frame.payload.get(
                            "method"
                        ) == "shutdown":
                            self._router.validate_inbound(frame)
                            await self._dispatcher.close()
                            if self._pty_manager is not None:
                                await self._pty_manager.close_all()
                            if self._manual_sftp_service is not None:
                                await self._manual_sftp_service.close_all()
                            if self._ssh_runtime is not None:
                                await self._ssh_runtime.close_all()
                            response = self._router.handle_validated(frame)
                        else:
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
            cleanup_error: BaseException | None = None

            def remember(error: BaseException) -> None:
                nonlocal cleanup_error
                if cleanup_error is None:
                    cleanup_error = error

            try:
                await self._dispatcher.close()
            except BaseException as exc:
                remember(exc)
            if self._application_tasks:
                try:
                    await asyncio.gather(
                        *self._application_tasks, return_exceptions=True
                    )
                except BaseException as exc:
                    remember(exc)
            if self._pty_manager is not None:
                try:
                    await self._pty_manager.close_all()
                except BaseException as exc:
                    remember(exc)
            if self._manual_sftp_service is not None:
                try:
                    await self._manual_sftp_service.close_all()
                except BaseException as exc:
                    remember(exc)
            if self._ssh_runtime is not None:
                try:
                    await self._ssh_runtime.close_all()
                except BaseException as exc:
                    remember(exc)
            try:
                await self._transport.close()
            except BaseException as exc:
                remember(exc)
            try:
                self._close_runtime()
            except BaseException as exc:
                remember(exc)
            try:
                self._router.mark_stopped()
            except BaseException as exc:
                remember(exc)
            if cleanup_error is not None:
                raise cleanup_error
        return exit_code

    def _is_application_request(self, frame: FrameEnvelope) -> bool:
        """判断请求 method 是否属于已注册的并行业务处理器。"""

        return (
            frame.message_type is MessageType.REQUEST
            and self._dispatcher.handles(frame.payload.get("method"))
        )

    def _start_application_request(self, frame: FrameEnvelope) -> None:
        """启动业务请求任务并在完成时从跟踪集合移除。"""

        task = asyncio.create_task(self._dispatch_application_request(frame))
        self._application_tasks.add(task)
        task.add_done_callback(self._application_tasks.discard)

    async def _dispatch_application_request(self, frame: FrameEnvelope) -> None:
        """执行应用处理器并把所有预期或意外失败转换为协议响应。"""

        try:
            result = await self._dispatcher.dispatch(frame)
            response = self._router.application_response(
                frame, result.message_type, result.payload
            )
        except DispatchError as exc:
            payload = {"error_code": exc.error_code, "message": str(exc)}
            payload.update(exc.details)
            response = self._router.application_response(
                frame,
                MessageType.ERROR,
                payload,
            )
        except Exception:
            LOGGER.exception(
                '{"level":"ERROR","error_code":"REQUEST_HANDLER_FAILED"}'
            )
            response = self._router.application_response(
                frame,
                MessageType.ERROR,
                {
                    "error_code": "REQUEST_HANDLER_FAILED",
                    "message": "application request handler failed",
                },
            )
        await self._transport.send(response)

    async def _handle_cancel(self, frame: FrameEnvelope) -> FrameEnvelope:
        """校验取消负载并通知分发器中的活动请求协作式停止。"""

        try:
            target = self._router.cancel_target(frame)
        except (ValueError, TypeError, AttributeError):
            return self._router.application_response(
                frame,
                MessageType.ERROR,
                {
                    "error_code": "INVALID_CANCEL_PAYLOAD",
                    "message": "cancel payload is invalid",
                },
            )
        if not await self._dispatcher.cancel(target):
            return self._router.application_response(
                frame,
                MessageType.ERROR,
                {
                    "error_code": "CANCEL_TARGET_NOT_FOUND",
                    "message": "target request is not active",
                },
            )
        return self._router.application_response(
            frame,
            MessageType.RESPONSE,
            {"result": "cancellation_requested", "target_request_id": str(target)},
        )

    def _initialize_runtime(self, payload: InitializeRequestPayload) -> None:
        """按 fail-closed 顺序打开、校验并发布全部运行时资源。"""

        # 局部导入避免领域 handler 直接导入 dispatcher 时触发 runtime package 环。
        from harness_shell_sidecar.manual_sftp.handlers import (
            register_manual_sftp_handlers,
        )
        from harness_shell_sidecar.manual_sftp.service import ManualSftpService

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
            connection_repository = ConnectionRepository(database)
            register_connection_handlers(self._dispatcher, connection_repository)
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
            ssh_runtime = SshRuntime(
                connection_repository,
                audit_ledger=audit_ledger,
                tracer=tracer,
                status_listener=self._publish_connection_status,
            )
            register_ssh_handlers(self._dispatcher, ssh_runtime)
            pty_manager = PtyManager(
                ssh_runtime.sessions,
                event_listener=self._publish_pty_event,
            )
            register_terminal_handlers(self._dispatcher, pty_manager)
            manual_sftp_service = ManualSftpService(
                ssh_runtime.sessions,
                record_store,
                event_listener=self._publish_manual_sftp_event,
            )
            register_manual_sftp_handlers(self._dispatcher, manual_sftp_service)
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
        self._connection_repository = connection_repository
        self._ssh_runtime = ssh_runtime
        self._pty_manager = pty_manager
        self._manual_sftp_service = manual_sftp_service
        self._record_store = record_store
        self._audit_ledger = audit_ledger
        self._trace_provider = trace_provider
        self._tracer = tracer

    def _record_ready(self) -> None:
        """在首次进入 READY 后写入一次审计事件和本地 Span。"""

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

    async def _publish_connection_status(self, status) -> None:
        """把 SSH 连接状态模型发布为无请求关联的应用事件。"""

        await self._transport.send(
            self._router.application_event(
                {
                    "event": "ssh.connection.status",
                    "status": status.model_dump(mode="json"),
                }
            )
        )

    async def _publish_pty_event(self, event: dict) -> None:
        """把 PTY 输出或关闭信息发布为应用事件。"""

        await self._transport.send(self._router.application_event(event))

    async def _publish_manual_sftp_event(self, event: dict) -> None:
        """发布仅由人工 SFTP 状态机生成的白名单候选事件。"""

        await self._transport.send(self._router.application_event(event))

    def _close_runtime(self) -> None:
        """记录终态、刷新遥测、清零 Key 并关闭数据库，保留首个错误。"""

        first_error: BaseException | None = None

        def remember(error: BaseException) -> None:
            nonlocal first_error
            if first_error is None:
                first_error = error

        if self._audit_ledger is not None:
            try:
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
            except BaseException as exc:
                remember(exc)
        if self._trace_provider is not None:
            try:
                self._trace_provider.force_flush()
            except BaseException as exc:
                remember(exc)
            try:
                self._trace_provider.shutdown()
            except BaseException as exc:
                remember(exc)
            finally:
                self._trace_provider = None
                self._tracer = None
        if self._record_store is not None:
            try:
                self._record_store.zeroize()
            except BaseException as exc:
                remember(exc)
            finally:
                self._record_store = None
        if self._audit_ledger is not None:
            try:
                self._audit_ledger.zeroize()
            except BaseException as exc:
                remember(exc)
            finally:
                self._audit_ledger = None
        if self._database is not None:
            try:
                self._database.close()
            except BaseException as exc:
                remember(exc)
            finally:
                self._database = None
                self._connection_repository = None
                self._ssh_runtime = None
                self._pty_manager = None
                self._manual_sftp_service = None
        if first_error is not None:
            raise first_error


def _zeroize(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
