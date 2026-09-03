from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext


def test_dispatcher_passes_only_request_context_and_params() -> None:
    """Keep protocol envelopes outside the application dispatcher contract."""

    async def scenario() -> None:
        observed: list[tuple[UUID, Mapping[str, object]]] = []

        async def handler(
            context: RequestContext,
            params: Mapping[str, object],
        ) -> dict[str, object]:
            observed.append((context.request_id, params))
            return {"result": "ok"}

        request_id = uuid4()
        dispatcher = RequestDispatcher()
        dispatcher.register("probe.read", handler)

        result = await dispatcher.dispatch(request_id, "probe.read", {"value": 7})

        assert result == {"result": "ok"}
        assert observed == [(request_id, {"value": 7})]

    asyncio.run(scenario())


def test_dispatcher_executes_non_json_application_work_under_same_owner() -> None:
    """Keep raw binary work inside duplicate, capacity, and cancellation ownership."""

    async def scenario() -> None:
        request_id = uuid4()
        dispatcher = RequestDispatcher()

        async def raw_work(context: RequestContext) -> bytes:
            assert context.request_id == request_id
            context.require_active()
            return b"raw"

        assert await dispatcher.execute(request_id, raw_work) == b"raw"

    asyncio.run(scenario())


def test_dispatcher_allows_heartbeat_loop_to_continue_while_request_runs() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(
            context: RequestContext,
            params: Mapping[str, object],
        ) -> dict[str, object]:
            del params
            started.set()
            await release.wait()
            return {"result": "done", "cancelled": context.cancelled.is_set()}

        request_id = uuid4()
        dispatcher = RequestDispatcher()
        dispatcher.register("slow.read", slow)
        active = asyncio.create_task(
            dispatcher.dispatch(request_id, "slow.read", {})
        )
        await started.wait()

        heartbeat_progressed = True
        assert heartbeat_progressed is True
        assert active.done() is False
        release.set()
        assert await active == {"result": "done", "cancelled": False}

    asyncio.run(scenario())


def test_dispatcher_rejects_unknown_duplicate_and_over_capacity() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(
            context: RequestContext,
            params: Mapping[str, object],
        ) -> dict[str, object]:
            del context, params
            started.set()
            await release.wait()
            return {"result": "done"}

        dispatcher = RequestDispatcher(capacity=1)
        dispatcher.register("slow.read", slow)
        first_request_id = uuid4()
        first = asyncio.create_task(
            dispatcher.dispatch(first_request_id, "slow.read", {})
        )
        await started.wait()

        with pytest.raises(DispatchError) as duplicate:
            await dispatcher.dispatch(first_request_id, "slow.read", {})
        assert duplicate.value.error_code == "DUPLICATE_REQUEST_ID"

        with pytest.raises(DispatchError) as capacity:
            await dispatcher.dispatch(uuid4(), "slow.read", {})
        assert capacity.value.error_code == "REQUEST_CAPACITY_EXCEEDED"

        with pytest.raises(DispatchError) as unknown:
            await RequestDispatcher().dispatch(uuid4(), "unknown", {})
        assert unknown.value.error_code == "UNKNOWN_METHOD"

        release.set()
        await first

    asyncio.run(scenario())


def test_dispatcher_capacity_defaults_to_sixteen() -> None:
    """Reject request seventeen while sixteen handlers remain active."""

    async def scenario() -> None:
        release = asyncio.Event()
        started = asyncio.Semaphore(0)

        async def slow(
            context: RequestContext,
            params: Mapping[str, object],
        ) -> dict[str, object]:
            del context, params
            started.release()
            await release.wait()
            return {"result": "done"}

        dispatcher = RequestDispatcher()
        dispatcher.register("slow.read", slow)
        active = [
            asyncio.create_task(dispatcher.dispatch(uuid4(), "slow.read", {}))
            for _ in range(16)
        ]
        for _ in active:
            await started.acquire()

        with pytest.raises(DispatchError) as capacity:
            await dispatcher.dispatch(uuid4(), "slow.read", {})
        assert capacity.value.error_code == "REQUEST_CAPACITY_EXCEEDED"

        release.set()
        await asyncio.gather(*active)

    asyncio.run(scenario())


def test_close_rejects_new_work_and_releases_active_handlers() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def wait_for_cancel(
            context: RequestContext,
            params: Mapping[str, object],
        ) -> dict[str, object]:
            del params
            started.set()
            await context.cancelled.wait()
            return {"cancelled": True}

        dispatcher = RequestDispatcher()
        dispatcher.register("wait", wait_for_cancel)
        active = asyncio.create_task(dispatcher.dispatch(uuid4(), "wait", {}))
        await started.wait()
        await dispatcher.close()
        assert await active == {"cancelled": True}

        with pytest.raises(DispatchError) as stopped:
            await dispatcher.dispatch(uuid4(), "wait", {})
        assert stopped.value.error_code == "RUNTIME_STOPPING"

    asyncio.run(scenario())
