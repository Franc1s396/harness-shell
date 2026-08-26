from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from harness_shell_sidecar.protocol import (
    FrameEnvelope,
    MessageType,
    Sensitivity,
)
from harness_shell_sidecar.runtime.dispatcher import (
    DispatchError,
    DispatchResult,
    RequestDispatcher,
)


def request(method: str, *, request_id=None) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.REQUEST,
        request_id=request_id or uuid4(),
        task_id=None,
        workflow_run_id=None,
        sequence=2,
        timestamp=datetime.now(timezone.utc),
        sensitivity=Sensitivity.NORMAL,
        payload={"method": method},
    )


def test_dispatcher_allows_heartbeat_loop_to_continue_while_request_runs() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
            started.set()
            await release.wait()
            return {"result": "done", "cancelled": cancelled.is_set()}

        dispatcher = RequestDispatcher()
        dispatcher.register("slow.read", slow)
        active = asyncio.create_task(dispatcher.dispatch(request("slow.read")))
        await started.wait()

        heartbeat_progressed = True
        assert heartbeat_progressed is True
        assert active.done() is False
        release.set()
        assert await active == DispatchResult(
            message_type=MessageType.RESPONSE,
            payload={"result": "done", "cancelled": False},
        )

    asyncio.run(scenario())


def test_dispatcher_rejects_unknown_duplicate_and_over_capacity() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
            started.set()
            await release.wait()
            return {"result": "done"}

        dispatcher = RequestDispatcher(capacity=1)
        dispatcher.register("slow.read", slow)
        first_frame = request("slow.read")
        first = asyncio.create_task(dispatcher.dispatch(first_frame))
        await started.wait()

        with pytest.raises(DispatchError) as duplicate:
            await dispatcher.dispatch(
                request("slow.read", request_id=first_frame.request_id)
            )
        assert duplicate.value.error_code == "DUPLICATE_REQUEST_ID"

        with pytest.raises(DispatchError) as capacity:
            await dispatcher.dispatch(request("slow.read"))
        assert capacity.value.error_code == "REQUEST_CAPACITY_EXCEEDED"

        with pytest.raises(DispatchError) as unknown:
            await RequestDispatcher().dispatch(request("unknown"))
        assert unknown.value.error_code == "UNKNOWN_METHOD"

        release.set()
        await first

    asyncio.run(scenario())


def test_cancel_sets_only_the_target_request_event() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def wait_for_cancel(
            frame: FrameEnvelope, cancelled: asyncio.Event
        ) -> dict:
            started.set()
            await cancelled.wait()
            return {"cancelled": True}

        dispatcher = RequestDispatcher()
        dispatcher.register("wait", wait_for_cancel)
        frame = request("wait")
        active = asyncio.create_task(dispatcher.dispatch(frame))
        await started.wait()

        assert await dispatcher.cancel(uuid4()) is False
        assert await dispatcher.cancel(frame.request_id) is True
        assert (await active).payload == {"cancelled": True}
        assert await dispatcher.cancel(frame.request_id) is False

    asyncio.run(scenario())


def test_close_rejects_new_work_and_releases_active_handlers() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def wait_for_cancel(
            frame: FrameEnvelope, cancelled: asyncio.Event
        ) -> dict:
            started.set()
            await cancelled.wait()
            return {"cancelled": True}

        dispatcher = RequestDispatcher()
        dispatcher.register("wait", wait_for_cancel)
        active = asyncio.create_task(dispatcher.dispatch(request("wait")))
        await started.wait()
        await dispatcher.close()
        assert (await active).payload == {"cancelled": True}

        with pytest.raises(DispatchError) as stopped:
            await dispatcher.dispatch(request("wait"))
        assert stopped.value.error_code == "RUNTIME_STOPPING"

    asyncio.run(scenario())
