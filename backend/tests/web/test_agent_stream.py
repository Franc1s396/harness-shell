from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.agent.contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.agent.streaming import AgentTurnTextDeltaEvent
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext
from harness_shell_sidecar.web.agent_stream import AgentTurnStreamSession
from harness_shell_sidecar.web.sse import encode_sse_event


def _run(status: AgentRunStatus, *, error_code: str | None = None) -> AgentRun:
    """为流所有权测试构建不可变持久化 Run 快照。"""

    now = datetime.now(UTC)
    return AgentRun(
        agent_run_id=uuid4(),
        conversation_id=uuid4(),
        ssh_session_id=uuid4(),
        api_config_id=uuid4(),
        status=status,
        react_iteration=0,
        error_code=error_code,
        started_at=now,
        ended_at=None if status is AgentRunStatus.RUNNING else now,
    )


def test_encoder_writes_fixed_three_lines_with_lf() -> None:
    """捕捉会使严格 React 解析器产生偏差的分帧变化。"""

    event = AgentTurnTextDeltaEvent(
        request_id=UUID("10000000-0000-4000-8000-000000000001"),
        sequence=1,
        conversation_id=UUID("20000000-0000-4000-8000-000000000002"),
        agent_run_id=UUID("30000000-0000-4000-8000-000000000003"),
        delta="你好\nworld",
    )

    encoded = encode_sse_event(event)

    assert encoded.startswith(b"event: agent.turn.text_delta\nid: 1\ndata: ")
    assert encoded.endswith(b"\n\n")
    assert b"\\nworld" in encoded
    assert b"\r" not in encoded
    assert encoded.count(b"\ndata: ") == 1


class FakeTurnApplication:
    """不访问 Provider、SSH 或持久化 I/O，驱动流生命周期。"""

    def __init__(
        self,
        *,
        fail_before_start: bool = False,
        block_after_start: bool = False,
        delta_count: int = 1,
    ) -> None:
        """配置确定性预检、取消或队列场景。"""

        self.fail_before_start = fail_before_start
        self.block_after_start = block_after_start
        self.delta_count = delta_count
        self.run_snapshot = _run(AgentRunStatus.RUNNING)
        self.accepted_deltas = 0
        self.attempted_deltas = 0
        self.started = asyncio.Event()
        self.terminal_published = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        _context: RequestContext,
        _params: Mapping[str, object],
        sink,
    ) -> None:
        """发布配置的生命周期并暴露生产者背压。"""

        if self.fail_before_start:
            raise DispatchError("MODEL_API_CONFIG_NOT_FOUND", "missing")
        await sink.started(self.run_snapshot)
        self.started.set()
        if self.block_after_start:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return
        for index in range(self.delta_count):
            self.attempted_deltas += 1
            await sink.text_delta(str(index))
            self.accepted_deltas += 1
        completed = self.run_snapshot.model_copy(
            update={
                "status": AgentRunStatus.COMPLETED,
                "ended_at": datetime.now(UTC),
            }
        )
        await sink.completed(completed)
        self.terminal_published.set()


def _session(application: FakeTurnApplication) -> AgentTurnStreamSession:
    """创建独立且归 dispatcher 所有的流会话。"""

    return AgentTurnStreamSession(
        request_id=uuid4(),
        dispatcher=RequestDispatcher(),
        application=application,
        params={},
    )


def test_session_surfaces_preflight_failure_before_a_body_exists() -> None:
    """持久化 RUNNING 前应用校验失败时禁止 HTTP 200。"""

    async def scenario() -> None:
        session = _session(FakeTurnApplication(fail_before_start=True))

        with pytest.raises(DispatchError) as error:
            await session.start()

        assert error.value.error_code == "MODEL_API_CONFIG_NOT_FOUND"
        assert session.worker_done

    asyncio.run(scenario())


def test_session_first_body_frame_is_started() -> None:
    """让持久化 RUNNING 关联成为首个字节可见流事件。"""

    async def scenario() -> None:
        session = _session(FakeTurnApplication())
        await session.start()

        body = session.body()
        first = await anext(body)

        assert first.startswith(b"event: agent.turn.started\nid: 0\n")
        await body.aclose()

    asyncio.run(scenario())


def test_session_close_cancels_and_awaits_the_worker() -> None:
    """防止 HTTP 消费者断连后遗留 Agent 工作。"""

    async def scenario() -> None:
        application = FakeTurnApplication(block_after_start=True)
        session = _session(application)
        await session.start()
        await application.started.wait()

        await session.aclose()

        assert application.cancelled.is_set()
        assert session.worker_done

    asyncio.run(scenario())


def test_queue_capacity_applies_backpressure_without_dropping_deltas() -> None:
    """容量满时阻塞生产者，消费者读取一次后恢复。"""

    async def scenario() -> None:
        application = FakeTurnApplication(delta_count=64)
        session = _session(application)
        await session.start()
        while application.attempted_deltas < 64:
            await asyncio.sleep(0)

        assert application.accepted_deltas == 63
        body = session.body()
        first = await anext(body)
        assert first.startswith(b"event: agent.turn.started")
        while application.accepted_deltas < 64:
            await asyncio.sleep(0)
        assert application.accepted_deltas == 64
        await body.aclose()

    asyncio.run(scenario())


def test_single_frame_limit_fails_without_truncating_delta() -> None:
    """在生产者边界拒绝超大可见事件。"""

    class OversizedApplication(FakeTurnApplication):
        """发布无法装入单个 SSE 帧的增量。"""

        async def run(
            self,
            _context: RequestContext,
            _params: Mapping[str, object],
            sink,
        ) -> None:
            """started 后触发发布器帧上限。"""

            await sink.started(self.run_snapshot)
            await sink.text_delta("x" * 65_536)

    async def scenario() -> None:
        session = _session(OversizedApplication())
        await session.start()
        body = session.body()
        assert (await anext(body)).startswith(b"event: agent.turn.started")

        with pytest.raises(AgentServiceError) as error:
            await anext(body)

        assert error.value.error_code == "AGENT_RESPONSE_TOO_LARGE"

    asyncio.run(scenario())


def test_dispatcher_shutdown_interrupts_a_backpressured_producer() -> None:
    """即使没有 HTTP 消费者排空满队列也能收敛关闭。"""

    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        application = FakeTurnApplication(delta_count=64)
        session = AgentTurnStreamSession(
            request_id=uuid4(),
            dispatcher=dispatcher,
            application=application,
            params={},
        )
        await session.start()
        while application.attempted_deltas < 64:
            await asyncio.sleep(0)

        await asyncio.wait_for(dispatcher.close(), timeout=0.2)

        assert session.worker_done
        await session.aclose()

    asyncio.run(scenario())


def test_cancelling_start_cancels_and_awaits_the_worker() -> None:
    """HTTP 启动本身取消时不保留应用工作。"""

    class StartupBlockingApplication(FakeTurnApplication):
        """在持久化启动前阻塞，让路由启动任务可被取消。"""

        async def run(
            self,
            _context: RequestContext,
            _params: Mapping[str, object],
            _sink,
        ) -> None:
            """首个事件发布前暴露取消。"""

            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario() -> None:
        application = StartupBlockingApplication()
        session = _session(application)
        start_task = asyncio.create_task(session.start())
        await application.started.wait()

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=0.2)
        assert application.cancelled.is_set()
        assert session.worker_done

    asyncio.run(scenario())


def test_shutdown_converges_after_terminal_refills_a_full_queue() -> None:
    """终止队列已满的生产者仍归 dispatcher 取消管理。"""

    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        application = FakeTurnApplication(delta_count=63)
        session = AgentTurnStreamSession(
            request_id=uuid4(),
            dispatcher=dispatcher,
            application=application,
            params={},
        )
        await session.start()
        while application.attempted_deltas < 63:
            await asyncio.sleep(0)
        body = session.body()
        assert (await anext(body)).startswith(b"event: agent.turn.started")
        await application.terminal_published.wait()

        await asyncio.wait_for(dispatcher.close(), timeout=0.2)

        assert session.worker_done
        await body.aclose()

    asyncio.run(scenario())


def test_duplicate_request_id_remains_active_until_terminal_is_sent() -> None:
    """终止帧仍排队时拒绝复用关联标识。"""

    async def scenario() -> None:
        dispatcher = RequestDispatcher()
        request_id = uuid4()
        application = FakeTurnApplication(delta_count=0)
        session = AgentTurnStreamSession(
            request_id=request_id,
            dispatcher=dispatcher,
            application=application,
            params={},
        )
        await session.start()
        await application.terminal_published.wait()

        with pytest.raises(DispatchError) as error:
            await dispatcher.execute(request_id, _no_op_work)

        assert error.value.error_code == "DUPLICATE_REQUEST_ID"
        async for _frame in session.body():
            pass
        assert session.worker_done

    asyncio.run(scenario())


def test_dispatcher_capacity_releases_only_after_consumer_completion() -> None:
    """消费者到达正常 EOF 前，已入队终止流仍占用容量。"""

    async def scenario() -> None:
        dispatcher = RequestDispatcher(capacity=1)
        application = FakeTurnApplication(delta_count=0)
        session = AgentTurnStreamSession(
            request_id=uuid4(),
            dispatcher=dispatcher,
            application=application,
            params={},
        )
        await session.start()
        await application.terminal_published.wait()

        with pytest.raises(DispatchError) as error:
            await dispatcher.execute(uuid4(), _no_op_work)
        assert error.value.error_code == "REQUEST_CAPACITY_EXCEEDED"

        async for _frame in session.body():
            pass
        assert await dispatcher.execute(uuid4(), _no_op_work) is None

    asyncio.run(scenario())


async def _no_op_work(_context: RequestContext) -> None:
    """完成 dispatcher 容量探测，不产生应用副作用。"""
