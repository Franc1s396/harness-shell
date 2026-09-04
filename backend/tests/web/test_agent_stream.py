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
    """Build one immutable durable Run snapshot for stream ownership tests."""

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
    """Catch framing changes that would make the strict React parser diverge."""

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
    """Drive one stream lifecycle without Provider, SSH, or persistence I/O."""

    def __init__(
        self,
        *,
        fail_before_start: bool = False,
        block_after_start: bool = False,
        delta_count: int = 1,
    ) -> None:
        """Configure one deterministic preflight, cancellation, or queue scenario."""

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
        """Publish the configured lifecycle and expose producer backpressure."""

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
    """Create one isolated dispatcher-owned stream session."""

    return AgentTurnStreamSession(
        request_id=uuid4(),
        dispatcher=RequestDispatcher(),
        application=application,
        params={},
    )


def test_session_surfaces_preflight_failure_before_a_body_exists() -> None:
    """Prevent HTTP 200 when application validation fails before durable RUNNING."""

    async def scenario() -> None:
        session = _session(FakeTurnApplication(fail_before_start=True))

        with pytest.raises(DispatchError) as error:
            await session.start()

        assert error.value.error_code == "MODEL_API_CONFIG_NOT_FOUND"
        assert session.worker_done

    asyncio.run(scenario())


def test_session_first_body_frame_is_started() -> None:
    """Make durable RUNNING correlation the first byte-visible stream event."""

    async def scenario() -> None:
        session = _session(FakeTurnApplication())
        await session.start()

        body = session.body()
        first = await anext(body)

        assert first.startswith(b"event: agent.turn.started\nid: 0\n")
        await body.aclose()

    asyncio.run(scenario())


def test_session_close_cancels_and_awaits_the_worker() -> None:
    """Prevent a disconnected HTTP consumer from orphaning Agent work."""

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
    """Block the producer at capacity and resume it after one consumer read."""

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
    """Reject an oversized visible event at the producer boundary."""

    class OversizedApplication(FakeTurnApplication):
        """Publish one delta which cannot fit in a single SSE frame."""

        async def run(
            self,
            _context: RequestContext,
            _params: Mapping[str, object],
            sink,
        ) -> None:
            """Trigger the publisher's frame limit after started."""

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
    """Converge shutdown even when no HTTP consumer drains a full queue."""

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
    """Do not retain application work when HTTP startup itself is cancelled."""

    class StartupBlockingApplication(FakeTurnApplication):
        """Block before durable start so the route startup task can be cancelled."""

        async def run(
            self,
            _context: RequestContext,
            _params: Mapping[str, object],
            _sink,
        ) -> None:
            """Expose cancellation before the first event is published."""

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
    """Keep a terminal-full producer under dispatcher cancellation ownership."""

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
    """Reject correlation reuse while the terminal frame is still queued."""

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
    """Count a queued terminal stream until its consumer reaches clean EOF."""

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
    """Complete one dispatcher capacity probe without application effects."""
