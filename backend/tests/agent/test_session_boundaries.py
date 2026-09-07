"""真实 Agent 编排在任何网络 await 前释放数据库事务。"""

import asyncio
from contextlib import contextmanager
from langchain_core.messages import AIMessage
from harness_shell_sidecar.agent.contracts import AgentRunStatus
from harness_shell_sidecar.agent.model_gateway import ModelGateway
from .fakes import FakeModelSequence, make_tool_call
from .test_graph import RecordingExecutor, _service, _run_turn


def test_concurrent_turns_release_sessions_before_network(agent_storage, monkeypatch) -> None:
    """两个会话共享工厂但不共享 Session，模型与工具只在事务外运行。"""
    database = agent_storage.database
    active = 0
    borrowed = []
    def track(factory):
        """包装真实上下文记录活动借用，不替代事务行为。"""
        @contextmanager
        def scope():
            """委托数据库原上下文并可靠归还测试计数。"""
            nonlocal active
            with factory() as session:
                active += 1
                borrowed.append(session)
                try:
                    yield session
                finally:
                    active -= 1
        return scope
    monkeypatch.setattr(database, "read_session", track(database.read_session))
    monkeypatch.setattr(database, "write_session", track(database.write_session))
    def outside_transaction():
        """远端执行边界不得持有任何当前数据库 Session。"""
        assert active == 0
    original = ModelGateway.invoke
    async def checked_invoke(self, *args, **kwargs):
        """在真实网关进入模型调用前后检查事务释放。"""
        outside_transaction()
        await asyncio.sleep(0)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(ModelGateway, "invoke", checked_invoke)
    async def scenario():
        """并行运行独立会话，覆盖模型、工具及完成终态。"""
        services = []
        for index in range(2):
            model = FakeModelSequence([
                AIMessage(content="", tool_calls=[make_tool_call(f"call-{index}", "pwd")]),
                AIMessage(content="done"),
            ])
            executor = RecordingExecutor(before_execute=outside_transaction)
            service, turn = _service(agent_storage, model, executor)
            services.append(_run_turn(agent_storage, service, turn))
        results = await asyncio.gather(*services)
        assert all(result.status == AgentRunStatus.COMPLETED for result in results)
    asyncio.run(scenario())
    assert active == 0
    assert len({id(session) for session in borrowed}) == len(borrowed)
