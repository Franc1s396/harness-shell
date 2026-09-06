"""管理单个 Agent SSE 响应的有界生产者与消费者生命周期。"""

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
)
from harness_shell_sidecar.runtime.dispatcher import RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .sse import encode_sse_event


AGENT_SSE_QUEUE_CAPACITY = 64
MAX_AGENT_SSE_FRAME_BYTES = 65_536
MAX_AGENT_SSE_BODY_BYTES = 4_194_304
AGENT_SSE_TERMINAL_RESERVE_BYTES = 65_536
class AgentTurnApplicationProtocol(Protocol):
    """描述流使用的独立于传输的轮次应用接口。"""

    async def run(
        self,
        context: RequestContext,
        raw_params: Mapping[str, object],
        event_sink: AgentTurnEventSink,
    ) -> None:
        """通过提供的事件接收端校验并执行 Agent 轮次。"""


class _AgentEventPublisher:
    """约束事件顺序、标识、大小预算及队列背压。"""

    def __init__(self, request_id: UUID) -> None:
        """创建发布器；持久化启动之前不创建队列。"""

        self._request_id = request_id  # HTTP 请求提供的关联标识。
        self._started_future: asyncio.Future[None] = (
            asyncio.get_running_loop().create_future()
        )
        self._queue: asyncio.Queue[AgentTurnStreamEvent] | None = None
        self._conversation_id: UUID | None = None  # started 时冻结。
        self._agent_run_id: UUID | None = None  # started 时冻结。
        self._sequence = 0  # 下一连续且在 JavaScript 安全整数范围内的事件序号。
        self._encoded_bytes = 0  # 已接受的 SSE 字节数，不包含结束哨兵。
        self._text_parts: list[str] = []  # 精确的已接受可见增量。
        self._sealed = False  # 终止事件阻止后续发布。
        self._consumer_abandoned = False  # 避免断连后清理被阻塞。
        self._consumer_abandoned_event = asyncio.Event()
        self._terminal_sent = asyncio.Event()
        self._cancelled: asyncio.Event | None = None  # dispatcher 关闭信号。

    @property
    def streamed_text(self) -> str:
        """返回已成功入队可见增量的精确拼接结果。"""

        return "".join(self._text_parts)

    async def started(self, run: AgentRun) -> None:
        """为 RUNNING Run 创建有界队列并发布序号零。"""

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
        """入队精确可见增量；容量已满时阻塞而非丢弃。"""

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
        """只为匹配且已持久化的 COMPLETED Run 入队成功事件。"""

        self._require_terminal_run(run, {AgentRunStatus.COMPLETED})
        event = AgentTurnCompletedEvent(
            request_id=self._request_id,
            sequence=self._sequence,
            conversation_id=run.conversation_id,
            agent_run_id=run.agent_run_id,
            react_iteration=run.react_iteration,
        )
        await self._publish_terminal(event)

    async def failed(self, run: AgentRun, message: str) -> None:
        """Run 终态持久化后入队已审查失败原因。"""

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
            message=message,
        )
        await self._publish_terminal(event)

    async def wait_started(self) -> None:
        """等待持久化 started 发布，或传播启动前失败。"""

        await self._started_future

    def fail_before_start(self, error: BaseException) -> None:
        """使用原始应用错误释放 HTTP 启动屏障。"""

        if not self._started_future.done():
            self._started_future.set_exception(error)

    def bind_cancellation(self, cancelled: asyncio.Event) -> None:
        """在应用工作能够发布事件前绑定 dispatcher 信号。"""

        if self._cancelled is not None:
            raise RuntimeError("Agent turn stream cancellation is already bound")
        self._cancelled = cancelled

    def abandon(self) -> None:
        """标记消费者已离开，避免取消清理等待队列。"""

        self._consumer_abandoned = True
        self._consumer_abandoned_event.set()

    async def wait_terminal_sent(self) -> None:
        """消费者发送终止帧前始终保留 dispatcher 所有权。"""

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
        """按队列顺序产出，或暴露终止前 worker 失败。"""

        # 1. 要求已完成启动；优先交付队列中已有的事件，保留精确顺序。
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
                # 2. 队列为空时让读取与 worker 完成竞争，暴露缺少终止事件的异常结束。
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
            # 3. 先交付事件，终止帧发送恢复后再通知 worker 释放 dispatcher 容量。
            yield event
            if isinstance(event, (AgentTurnCompletedEvent, AgentTurnFailedEvent)):
                self._terminal_sent.set()
                return

    async def _publish_terminal(self, event: AgentTurnStreamEvent) -> None:
        """校验、入队、计量并封闭且仅封闭一个终止事件。"""

        encoded_size = self._validated_size(event, terminal=True)
        await self._put(event)
        self._encoded_bytes += encoded_size
        self._sequence += 1
        self._sealed = True

    async def _put(self, event: AgentTurnStreamEvent) -> None:
        """等待队列容量，同时允许 dispatcher 关闭时取消。"""

        # 1. 要求取消信号已绑定且尚未触发，再申请队列容量。
        queue = cast(asyncio.Queue[AgentTurnStreamEvent], self._queue)
        cancelled = self._cancelled
        if cancelled is None:
            raise RuntimeError("Agent turn stream cancellation is not bound")
        if cancelled.is_set():
            raise asyncio.CancelledError
        # 2. 让入队、dispatcher 取消和消费者离开竞争，不丢弃或合并事件。
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
        # 3. 所有退出路径均回收辅助等待任务；取消时也等待入队任务结束。
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
        """拒绝超过冻结契约限制的帧或累计正文。"""

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
        """拒绝启动前或终止事件后的增量。"""

        if self._queue is None:
            raise RuntimeError("Agent turn stream has not started")
        if self._sealed:
            raise RuntimeError("Agent turn stream is already terminal")

    def _require_terminal_run(
        self,
        run: AgentRun,
        allowed_statuses: set[AgentRunStatus],
    ) -> None:
        """要求流仍打开，且持久化 Run 标识与冻结值精确一致。"""

        self._require_open()
        if run.status not in allowed_statuses:
            raise RuntimeError("Agent Run has an invalid terminal status")
        if (
            run.conversation_id != self._conversation_id
            or run.agent_run_id != self._agent_run_id
        ):
            raise RuntimeError("Agent Run identity changed during streaming")


class AgentTurnStreamSession:
    """将 dispatcher 拥有的 Agent 轮次桥接为可取消的 SSE 正文。"""

    def __init__(
        self,
        *,
        request_id: UUID,
        dispatcher: RequestDispatcher,
        application: AgentTurnApplicationProtocol,
        params: Mapping[str, object],
    ) -> None:
        """捕获不可变请求输入，不启动应用工作。"""

        self._request_id = request_id  # dispatcher 关联标识。
        self._dispatcher = dispatcher  # 共享容量和取消的管理者。
        self._application = application  # 独立于传输的轮次入口。
        self._params = dict(params)  # 已校验的原始请求体快照。
        self._publisher = _AgentEventPublisher(request_id)
        self._worker: asyncio.Task[None] | None = None  # 唯一轮次执行任务。
        self._closed = False  # 确保断连清理幂等。

    @property
    def worker_done(self) -> bool:
        """报告应用 worker 是否已完全退出。"""

        return self._worker is not None and self._worker.done()

    async def start(self) -> None:
        """started 安全入队后才跨过响应边界。"""

        # 1. 拒绝重复启动，再创建本流唯一 worker。
        if self._worker is not None:
            raise RuntimeError("Agent turn stream session has already started")
        self._worker = asyncio.create_task(self._run_worker())
        try:
            # 2. 等待持久化 Run 和 started 入队；此屏障通过后才允许 HTTP 200。
            await self._publisher.wait_started()
        # 3. 启动失败或取消时关闭会话并等待 worker，原始失败继续传播。
        except BaseException:
            await self.aclose()
            raise

    async def body(self) -> AsyncIterator[bytes]:
        """产出编码帧并传播意外 worker 失败。"""

        if self._worker is None:
            raise RuntimeError("Agent turn stream session has not started")
        try:
            async for event in self._publisher.events(self._worker):
                yield encode_sse_event(event)
            await self._worker
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """HTTP 消费者断连时取消并等待唯一 worker。"""

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
        """在 dispatcher 所有权下运行应用工作并关闭队列。"""

        try:
            await self._dispatcher.execute(
                self._request_id,
                self._run_application,
            )
        except BaseException as error:
            self._publisher.fail_before_start(error)
            raise

    async def _run_application(self, context: RequestContext) -> None:
        """进入使用秘密的应用工作前绑定 dispatcher 取消信号。"""

        # 1. 在应用能够发布任何事件前绑定 dispatcher 的取消信号。
        self._publisher.bind_cancellation(context.cancelled)
        # 2. 执行领域轮次，由应用负责 Run 持久化和事件发布。
        await self._application.run(context, self._params, self._publisher)
        # 3. 即使终止事件已入队，也要等消费者实际发送后才释放请求所有权。
        await self._publisher.wait_terminal_sent()


__all__ = ["AgentTurnApplicationProtocol", "AgentTurnStreamSession"]
