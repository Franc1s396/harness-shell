"""滚动摘要只重试有限次数，并保留原始历史。"""
import asyncio
from uuid import uuid4
from collections.abc import Sequence
import pytest
from langchain_core.messages import HumanMessage, AIMessage, AnyMessage
from pydantic import SecretStr
from harness_shell_sidecar.agent.context_models import ContextError, TokenEstimate, ContextMessage, ContextSummary
from harness_shell_sidecar.agent.context_summaries import ContextSummaryRepository
from harness_shell_sidecar.agent.model_gateway import ModelGatewayError
from harness_shell_sidecar.agent.contracts import AgentRunStatus, ModelApiConfig
from .conftest import AgentStorage, valid_api_config_input


class ControlledBudget:
    """仅控制 Provider 规模估算，历史和存储使用真实实现。"""
    def estimate(self, config: ModelApiConfig, records: Sequence[ContextMessage],
                 summary: ContextSummary | None) -> TokenEstimate:
        """强制达到阈值，同时保留实际历史投影。"""
        return TokenEstimate(96000, "TOKENIZER_ESTIMATE")
    def should_compact(self, config: ModelApiConfig, tokens: int) -> bool:
        """本 fixture 始终覆盖摘要分支。"""
        return True
    def estimate_payload(self, payload: dict[str, object]) -> int:
        """为远程请求测试保留小型确定性输入。"""
        return 500
    def assert_fits(self, config: ModelApiConfig, tokens: int) -> None:
        """匹配默认配置的输入预算。"""
        if tokens > 119808:
            raise ContextError("CONTEXT_BUDGET_EXCEEDED", "too large")


class SummaryGateway:
    """仅模拟远程摘要调用，并记录实际次数。"""
    def __init__(self, failures: int) -> None:
        """让指定次数的初始尝试失败。"""
        self.failures = failures  # 模拟远程失败次数。
        self.calls = 0  # 包含失败在内的实际尝试次数。
    async def summarize_once(self, config: ModelApiConfig, api_key: SecretStr,
                             messages: Sequence[AnyMessage], cancelled: asyncio.Event) -> str:
        """每次调用返回一个远程结果。"""
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelGatewayError("MODEL_REQUEST_FAILED", "summary failed")
        return "Only the first completed turn was summarized."


@pytest.mark.parametrize("failures", [0, 2, 3])
def test_compaction_retries_and_preserves_messages(agent_storage: AgentStorage, failures: int) -> None:
    from harness_shell_sidecar.agent.context_compaction import ContextCompactor
    from .fakes import instant_sleep
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    for i in range(4):
        old = repo.start_run(conversation, uuid4(), config.api_config_id)
        repo.append_messages_atomic(old.agent_run_id, conversation,
            [HumanMessage(content=f"user-{i}"), AIMessage(content=f"answer-{i}")])
        repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="next"))
    before = repo.load_messages(conversation)
    summaries = ContextSummaryRepository(agent_storage.database)
    gateway = SummaryGateway(failures)
    compactor = ContextCompactor(summaries, ControlledBudget(), gateway, sleep=instant_sleep)
    async def perform() -> ContextSummary | None:
        """对持久化历史调用真实压缩器。"""
        return await compactor.compact(config=config, api_key=SecretStr("key"),
            records=repo.load_context_messages(conversation), summary=None,
            conversation_id=conversation, source_run_id=current.agent_run_id, cancelled=asyncio.Event())
    if failures == 3:
        with pytest.raises(ContextError) as error:
            asyncio.run(perform())
        assert error.value.error_code == "CONTEXT_COMPACTION_FAILED"
        assert summaries.load(conversation) is None
    else:
        saved = asyncio.run(perform())
        assert saved.covered_through_sequence == 2
        assert saved.revision == 1
    assert gateway.calls == min(failures + 1, 3)
    assert repo.load_messages(conversation) == before
    if failures == 3:
        repo.finish_run(current.agent_run_id, AgentRunStatus.FAILED, "CONTEXT_COMPACTION_FAILED")
        following = repo.start_run(conversation, uuid4(), config.api_config_id)
        repo.append_message(following.agent_run_id, conversation, HumanMessage(content="retry next turn"))
        async def retry_next_turn() -> ContextSummary | None:
            """上一轮失败不得永久禁用此会话。"""
            return await compactor.compact(config=config, api_key=SecretStr("key"),
                records=repo.load_context_messages(conversation), summary=None,
                conversation_id=conversation, source_run_id=following.agent_run_id, cancelled=asyncio.Event())
        retried = asyncio.run(retry_next_turn())
        assert retried.revision == 1
        assert gateway.calls == 4
    if failures == 0:
        repo.append_message(current.agent_run_id, conversation, AIMessage(content="current complete"))
        repo.finish_run(current.agent_run_id, AgentRunStatus.COMPLETED, None)
        following = repo.start_run(conversation, uuid4(), config.api_config_id)
        repo.append_message(following.agent_run_id, conversation, HumanMessage(content="another turn"))
        async def roll() -> ContextSummary | None:
            """只将新变旧的历史前缀与已有摘要一同压缩。"""
            return await compactor.compact(config=config, api_key=SecretStr("key"),
                records=repo.load_context_messages(conversation), summary=saved,
                conversation_id=conversation, source_run_id=following.agent_run_id, cancelled=asyncio.Event())
        rolled = asyncio.run(roll())
        assert rolled.revision == 2
        assert rolled.covered_through_sequence == 4


@pytest.mark.parametrize("failure_stage", ["source", "candidate", "cancel_retry"])
def test_failed_candidate_preserves_existing_summary(
    agent_storage: AgentStorage, failure_stage: str,
) -> None:
    from harness_shell_sidecar.agent.context_compaction import ContextCompactor
    from harness_shell_sidecar.agent.executor import AgentCancelled
    repo = agent_storage.conversations
    config = agent_storage.api_configs.create(valid_api_config_input())
    conversation = repo.create_conversation()
    for i in range(5):
        old = repo.start_run(conversation, uuid4(), config.api_config_id)
        repo.append_messages_atomic(old.agent_run_id, conversation,
            [HumanMessage(content=f"user-{i}"), AIMessage(content=f"answer-{i}")])
        repo.finish_run(old.agent_run_id, AgentRunStatus.COMPLETED, None)
    current = repo.start_run(conversation, uuid4(), config.api_config_id)
    repo.append_message(current.agent_run_id, conversation, HumanMessage(content="next"))
    summaries = ContextSummaryRepository(agent_storage.database)
    previous = summaries.commit_candidate(conversation_id=conversation, expected_revision=0,
        covered_through_sequence=2, summary_text="Previously committed summary", source_run_id=current.agent_run_id)
    before = repo.load_messages(conversation)

    class StageBudget(ControlledBudget):
        """使摘要输入或候选准入失败，不伪造存储。"""
        def __init__(self) -> None:
            """按流水线顺序统计本地准入估算。"""
            self.calls = 0  # 首次为摘要输入，第二次为候选投影。
        def estimate_payload(self, payload: dict[str, object]) -> int:
            """只让指定准入阶段超限。"""
            self.calls += 1
            failing_call = {"source": 1, "candidate": 2}.get(failure_stage)
            return 120000 if self.calls == failing_call else 500

    async def scenario() -> None:
        """退避期间取消必须阻止下一次 Provider 请求。"""
        cancelled = asyncio.Event()
        async def cancel_retry(delay: float) -> None:
            """在等待重试时钟时模拟用户取消。"""
            cancelled.set()
            await asyncio.sleep(0)
        gateway = SummaryGateway(3 if failure_stage == "cancel_retry" else 0)
        compactor = ContextCompactor(summaries, StageBudget(), gateway, sleep=cancel_retry)
        expected_error = AgentCancelled if failure_stage == "cancel_retry" else ContextError
        with pytest.raises(expected_error):
            await compactor.compact(config=config, api_key=SecretStr("key"),
                records=repo.load_context_messages(conversation), summary=previous,
                conversation_id=conversation, source_run_id=current.agent_run_id, cancelled=cancelled)
        assert gateway.calls == (0 if failure_stage == "source" else 1)
        assert summaries.load(conversation) == previous
        assert repo.load_messages(conversation) == before
    asyncio.run(scenario())


def test_summary_input_excludes_usage_and_opaque_replay() -> None:
    from harness_shell_sidecar.agent.context_compaction import build_summary_messages
    run_id = uuid4()
    prefix = [ContextMessage(1, run_id, HumanMessage(content="inspect service")),
        ContextMessage(2, run_id, AIMessage(content="service is running",
            usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105},
            additional_kwargs={"harness_responses_replay": "opaque-marker"}))]
    text = build_summary_messages(None, prefix)[1].content
    assert "inspect service" in text
    assert "service is running" in text
    assert "opaque-marker" not in text
    assert "input_tokens" not in text
