"""Own the bounded producer/consumer lifecycle for one Agent SSE response."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Protocol, cast
from uuid import UUID

from harness_shell_sidecar.agent.contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.agent.service import AgentServiceError
from harness_shell_sidecar.agent.streaming import (
    AgentTurnCompletedEvent,
    AgentTurnEventSink,
    AgentTurnFailedEvent,
    AgentTurnStartedEvent,
    AgentTurnStreamEvent,
    AgentTurnTextDeltaEvent,
    public_failure_message,
)
from harness_shell_sidecar.runtime.dispatcher import RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .sse import encode_sse_event


AGENT_SSE_QUEUE_CAPACITY = 64
MAX_AGENT_SSE_FRAME_BYTES = 65_536
MAX_AGENT_SSE_BODY_BYTES = 4_194_304
AGENT_SSE_TERMINAL_RESERVE_BYTES = 65_536
class AgentTurnApplicationProtocol(Protocol):
    """Describe the transport-independent turn application used by the stream."""

    async def run(
        self,
        context: RequestContext,
        raw_params: Mapping[str, object],
        event_sink: AgentTurnEventSink,
    ) -> None:
        """Validate and run one Agent turn through the supplied event sink."""


class _AgentEventPublisher:
    """Enforce event order, identity, size budgets, and queue backpressure."""

    def __init__(self, request_id: UUID) -> None:
        """Create a publisher whose queue does not exist before durable start."""

        self._request_id = request_id  # Correlation identity from the HTTP request.
        self._started_future: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self._queue: asyncio.Queue[AgentTurnStreamEvent] | None = None
        self._conversation_id: UUID | None = None  # Frozen at started.
        self._agent_run_id: UUID | None = None  # Frozen at started.
        self._sequence = 0  # Next contiguous JavaScript-safe event sequence.
        self._encoded_bytes = 0  # Accepted SSE bytes, excluding the end sentinel.
        self._text_parts: list[str] = []  # Exact accepted visible deltas.
        self._sealed = False  # Terminal event prevents further publications.
        self._consumer_abandoned = False  # Avoid blocking cleanup after disconnect.
        self._consumer_abandoned_event = asyncio.Event()
        self._terminal_sent = asyncio.Event()
        self._cancelled: asyncio.Event | None = None  # Dispatcher shutdown signal.

    @property
    def streamed_text(self) -> str:
        """Return the exact concatenation of successfully queued visible deltas."""

        return "".join(self._text_parts)

    async def started(self, run: AgentRun) -> None:
        """Create the bounded queue and publish sequence zero for a RUNNING Run."""

        if self._queue is not None or self._sealed:
            raise RuntimeError("Agent turn stream has already started")
        if run.status is not AgentRunStatus.RUNNING:
            raise RuntimeError("Agent turn stream must start from a RUNNING Run")

        queue: asyncio.Queue[AgentTurnStreamEvent] = asyncio.Queue(
            maxsize=AGENT_SSE_QUEUE_CAPACITY
        )
        event = AgentTurnStartedEvent(
            request_id=self._request_id,
            sequence=0,
            conversation_id=run.conversation_id,
            agent_run_id=run.agent_run_id,
        )
        encoded_size = self._validated_size(event, terminal=False)
        self._conversation_id = run.conversation_id
        self._agent_run_id = run.agent_run_id
        self._queue = queue
        await self._put(event)
        self._encoded_bytes += encoded_size
        self._sequence = 1
        self._started_future.set_result(None)

    async def text_delta(self, delta: str) -> None:
        """Queue one exact visible delta, blocking rather than dropping at capacity."""

        self._require_open()
        event = AgentTurnTextDeltaEvent(
            request_id=self._request_id,
            sequence=self._sequence,
            conversation_id=cast(UUID, self._conversation_id),
            agent_run_id=cast(UUID, self._agent_run_id),
            delta=delta,
        )
        encoded_size = self._validated_size(event, terminal=False)
        await self._put(event)
        self._encoded_bytes += encoded_size
        self._sequence += 1
        self._text_parts.append(delta)

    async def completed(self, run: AgentRun) -> None:
        """Queue success only for the matching durable COMPLETED Run."""

        self._require_terminal_run(run, {AgentRunStatus.COMPLETED})
        event = AgentTurnCompletedEvent(
            request_id=self._request_id,
            sequence=self._sequence,
            conversation_id=run.conversation_id,
            agent_run_id=run.agent_run_id,
            react_iteration=run.react_iteration,
        )
        await self._publish_terminal(event)

    async def failed(self, run: AgentRun) -> None:
        """Queue one safe failure after the matching Run is durably terminal."""

        self._require_terminal_run(
            run,
            {
                AgentRunStatus.FAILED,
                AgentRunStatus.LIMIT_REACHED,
                AgentRunStatus.CANCELLED,
            },
        )
        if run.error_code is None:
            raise RuntimeError("failed Agent Run requires an error code")
        event = AgentTurnFailedEvent(
            request_id=self._request_id,
            sequence=self._sequence,
            conversation_id=run.conversation_id,
            agent_run_id=run.agent_run_id,
            status=run.status.value,
            react_iteration=run.react_iteration,
            error_code=run.error_code,
            message=public_failure_message(run.error_code),
        )
        await self._publish_terminal(event)

    async def wait_started(self) -> None:
        """Wait for durable started publication or propagate a pre-start failure."""

        await self._started_future

    def fail_before_start(self, error: BaseException) -> None:
        """Release the HTTP startup barrier with the original application error."""

        if not self._started_future.done():
            self._started_future.set_exception(error)

    def bind_cancellation(self, cancelled: asyncio.Event) -> None:
        """Bind the dispatcher signal before application work can publish."""

        if self._cancelled is not None:
            raise RuntimeError("Agent turn stream cancellation is already bound")
        self._cancelled = cancelled

    def abandon(self) -> None:
        """Mark the consumer gone so cancellation cleanup never waits on its queue."""

        self._consumer_abandoned = True
        self._consumer_abandoned_event.set()

    async def wait_terminal_sent(self) -> None:
        """Keep dispatcher ownership until the consumer sends the terminal frame."""

        if not self._sealed:
            raise RuntimeError("Agent turn application returned without a terminal event")
        cancelled = cast(asyncio.Event, self._cancelled)
        sent_task = asyncio.create_task(self._terminal_sent.wait())
        cancel_task = asyncio.create_task(cancelled.wait())
        abandoned_task = asyncio.create_task(self._consumer_abandoned_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {sent_task, cancel_task, abandoned_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sent_task in done and self._terminal_sent.is_set():
                return
            raise asyncio.CancelledError
        finally:
            for task in (sent_task, cancel_task, abandoned_task):
                task.cancel()
            await asyncio.gather(
                sent_task,
                cancel_task,
                abandoned_task,
                return_exceptions=True,
            )

    async def events(
        self,
        worker: asyncio.Task[None],
    ) -> AsyncIterator[AgentTurnStreamEvent]:
        """Yield queue order, or surface a worker failure before terminal."""

        queue = self._queue
        if queue is None:
            raise RuntimeError("Agent turn stream has not started")
        while True:
            if not queue.empty():
                event = queue.get_nowait()
            elif worker.done():
                await worker
                raise RuntimeError("Agent turn worker ended without a terminal event")
            else:
                get_task = asyncio.create_task(queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        {get_task, worker},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_task in done:
                        event = get_task.result()
                    else:
                        get_task.cancel()
                        await asyncio.gather(get_task, return_exceptions=True)
                        if not queue.empty():
                            continue
                        await worker
                        raise RuntimeError(
                            "Agent turn worker ended without a terminal event"
                        )
                finally:
                    if not get_task.done():
                        get_task.cancel()
                        await asyncio.gather(get_task, return_exceptions=True)
            yield event
            if isinstance(event, (AgentTurnCompletedEvent, AgentTurnFailedEvent)):
                self._terminal_sent.set()
                return

    async def _publish_terminal(self, event: AgentTurnStreamEvent) -> None:
        """Validate, queue, account, and seal exactly one terminal event."""

        encoded_size = self._validated_size(event, terminal=True)
        await self._put(event)
        self._encoded_bytes += encoded_size
        self._sequence += 1
        self._sealed = True

    async def _put(self, event: AgentTurnStreamEvent) -> None:
        """Wait for queue capacity while remaining cancellable by dispatcher close."""

        queue = cast(asyncio.Queue[AgentTurnStreamEvent], self._queue)
        cancelled = self._cancelled
        if cancelled is None:
            raise RuntimeError("Agent turn stream cancellation is not bound")
        if cancelled.is_set():
            raise asyncio.CancelledError
        put_task = asyncio.create_task(queue.put(event))
        cancel_task = asyncio.create_task(cancelled.wait())
        abandoned_task = asyncio.create_task(self._consumer_abandoned_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {put_task, cancel_task, abandoned_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if (
                (cancel_task in done and cancelled.is_set())
                or (abandoned_task in done and self._consumer_abandoned)
            ):
                put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
                raise asyncio.CancelledError
            await put_task
        except asyncio.CancelledError:
            put_task.cancel()
            await asyncio.gather(put_task, return_exceptions=True)
            raise
        finally:
            cancel_task.cancel()
            abandoned_task.cancel()
            await asyncio.gather(
                cancel_task,
                abandoned_task,
                return_exceptions=True,
            )

    def _validated_size(
        self,
        event: AgentTurnStreamEvent,
        *,
        terminal: bool,
    ) -> int:
        """Reject frames or aggregate bodies that exceed the frozen contract."""

        encoded_size = len(encode_sse_event(event))
        if encoded_size > MAX_AGENT_SSE_FRAME_BYTES:
            raise AgentServiceError(
                "AGENT_RESPONSE_TOO_LARGE",
                "the encoded Agent SSE event exceeded the frame limit",
            )
        reserve = 0 if terminal else AGENT_SSE_TERMINAL_RESERVE_BYTES
        if self._encoded_bytes + encoded_size + reserve > MAX_AGENT_SSE_BODY_BYTES:
            raise AgentServiceError(
                "AGENT_RESPONSE_TOO_LARGE",
                "the encoded Agent SSE stream exceeded the body limit",
            )
        return encoded_size

    def _require_open(self) -> None:
        """Reject deltas before start or after a terminal event."""

        if self._queue is None:
            raise RuntimeError("Agent turn stream has not started")
        if self._sealed:
            raise RuntimeError("Agent turn stream is already terminal")

    def _require_terminal_run(
        self,
        run: AgentRun,
        allowed_statuses: set[AgentRunStatus],
    ) -> None:
        """Require an open stream and the exact frozen durable Run identity."""

        self._require_open()
        if run.status not in allowed_statuses:
            raise RuntimeError("Agent Run has an invalid terminal status")
        if (
            run.conversation_id != self._conversation_id
            or run.agent_run_id != self._agent_run_id
        ):
            raise RuntimeError("Agent Run identity changed during streaming")


class AgentTurnStreamSession:
    """Bridge one dispatcher-owned Agent turn to one cancellable SSE body."""

    def __init__(
        self,
        *,
        request_id: UUID,
        dispatcher: RequestDispatcher,
        application: AgentTurnApplicationProtocol,
        params: Mapping[str, object],
    ) -> None:
        """Capture immutable request inputs without starting application work."""

        self._request_id = request_id  # Dispatcher correlation identity.
        self._dispatcher = dispatcher  # Shared capacity and cancellation owner.
        self._application = application  # Transport-independent turn entrypoint.
        self._params = dict(params)  # Validated raw body snapshot.
        self._publisher = _AgentEventPublisher(request_id)
        self._worker: asyncio.Task[None] | None = None  # Sole turn execution task.
        self._closed = False  # Makes disconnect cleanup idempotent.

    @property
    def worker_done(self) -> bool:
        """Report whether the application worker has fully unwound."""

        return self._worker is not None and self._worker.done()

    async def start(self) -> None:
        """Cross the response boundary only after started is safely queued."""

        if self._worker is not None:
            raise RuntimeError("Agent turn stream session has already started")
        self._worker = asyncio.create_task(self._run_worker())
        try:
            await self._publisher.wait_started()
        except BaseException:
            await self.aclose()
            raise

    async def body(self) -> AsyncIterator[bytes]:
        """Yield encoded frames and propagate unexpected worker failures."""

        if self._worker is None:
            raise RuntimeError("Agent turn stream session has not started")
        try:
            async for event in self._publisher.events(self._worker):
                yield encode_sse_event(event)
            await self._worker
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Cancel and await the sole worker when the HTTP consumer disconnects."""

        if self._closed:
            return
        self._closed = True
        self._publisher.abandon()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)

    async def _run_worker(self) -> None:
        """Run application work under dispatcher ownership and close its queue."""

        try:
            await self._dispatcher.execute(
                self._request_id,
                self._run_application,
            )
        except BaseException as error:
            self._publisher.fail_before_start(error)
            raise

    async def _run_application(self, context: RequestContext) -> None:
        """Bind dispatcher cancellation before entering secret application work."""

        self._publisher.bind_cancellation(context.cancelled)
        await self._application.run(context, self._params, self._publisher)
        await self._publisher.wait_terminal_sent()


__all__ = ["AgentTurnApplicationProtocol", "AgentTurnStreamSession"]
