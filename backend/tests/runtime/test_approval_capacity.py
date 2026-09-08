"""普通请求满载时仍能提交审批，且控制容量有界。"""

import asyncio
from uuid import uuid4

import pytest

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher


@pytest.mark.parametrize("capacity", [1, 16])
def test_approval_control_slot_is_independent_and_bounded(capacity: int) -> None:
    async def scenario() -> None:
        """两个容量池共享请求生命周期，控制槽不可无限增长。"""
        dispatcher = RequestDispatcher(capacity=capacity)
        normal_started, control_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def normal(context):
            """占满普通容量。"""
            normal_started.set()
            await release.wait()
            return {}

        async def control(context, params):
            """显式阻塞控制请求以测试第二个控制请求。"""
            control_started.set()
            await release.wait()
            return {}

        dispatcher.register("agent.approvals.decide", control)
        ordinary = [asyncio.create_task(dispatcher.execute(uuid4(), normal)) for _ in range(capacity)]
        await normal_started.wait()
        approval = asyncio.create_task(dispatcher.dispatch(uuid4(), "agent.approvals.decide", {}))
        try:
            await asyncio.wait_for(control_started.wait(), 1)
            with pytest.raises(DispatchError):
                await dispatcher.dispatch(uuid4(), "agent.approvals.decide", {})
            with pytest.raises(DispatchError):
                await dispatcher.execute(uuid4(), normal)
        finally:
            release.set()
            await asyncio.gather(*ordinary, approval)
        await dispatcher.close()
    asyncio.run(scenario())
