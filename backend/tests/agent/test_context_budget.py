"""usage 锚点不得重复计算自身回复或跨摘要版本复用。"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, AIMessage
from harness_shell_sidecar.agent.context import ContextService
from harness_shell_sidecar.agent.context_budget import ContextBudget
from harness_shell_sidecar.agent.context_models import ContextMessage, AgentContextPolicy, ContextError, ContextSummary
from harness_shell_sidecar.agent.model_gateway import model_input_payload
from harness_shell_sidecar.agent.tokenizer import load_local_encoding, tokenizer_resource_dir
from .test_model_gateway import chat_config


def test_budget_boundary() -> None:
    ContextBudget.assert_fits(chat_config(), 119808)
    with pytest.raises(ContextError):
        ContextBudget.assert_fits(chat_config(), 119809)
    assert ContextBudget.should_compact(chat_config(), 96000)
    assert not ContextBudget.should_compact(chat_config(), 95999)


def test_usage_anchor_does_not_recount_answer() -> None:
    budget = ContextBudget(load_local_encoding(tokenizer_resource_dir(), "o200k_base"), AgentContextPolicy())
    config = chat_config()
    run_id = uuid4()
    reply = AIMessage(content="very long answer" * 1000,
        usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        additional_kwargs={"harness_context_anchor": {"schema_version": 1,
            "context_revision": 0, "request_identity": budget.request_identity(config)}})
    record = ContextMessage(3, run_id, reply)
    assert budget.estimate(config, [record], ()).tokens == 1200
    assert budget.estimate(config, [record], ()).source == "PROVIDER_USAGE"
    changed = config.model_copy(update={"model": "other"})
    assert budget.estimate(changed, [record], ()).source == "TOKENIZER_ESTIMATE"
    following = HumanMessage(content="next <|endoftext|> special token literal")
    records = [record, ContextMessage(4, run_id, following)]
    increment = budget.estimate_payload(model_input_payload(config, [following], include_tools=False))
    assert budget.estimate(config, records, ()).tokens == 1200 + increment
    now = datetime.now(timezone.utc)
    summary = ContextSummary(uuid4(), 1, 2, "old summary", run_id, now, now)
    assert budget.estimate(config, records, (summary,)).source == "TOKENIZER_ESTIMATE"


def test_projection_retains_all_unsummarized_turns() -> None:
    records = [ContextMessage(i + 1, uuid4(), HumanMessage(content=str(i))) for i in range(25)]
    assert len(ContextService.project(records, ())) == 26
    assert [r.sequence for r in ContextService.compactable_prefix(records)] == list(range(1, 16))


def test_every_old_summary_contributes_to_input_budget() -> None:
    budget = ContextBudget(load_local_encoding(tokenizer_resource_dir(), "o200k_base"), AgentContextPolicy())
    config = chat_config()
    now = datetime.now(timezone.utc)
    conversation, run = uuid4(), uuid4()
    summaries = (ContextSummary(conversation, 1, 2, "large summary " * 1000, run, now, now),
                 ContextSummary(conversation, 2, 4, "new summary", run, now, now))
    records = [ContextMessage(5, run, HumanMessage(content="current"))]
    full = budget.estimate(config, records, summaries)
    latest_only = budget.estimate(config, records, summaries[1:])
    assert full.tokens > latest_only.tokens + 1000
    limited = config.model_copy(update={"context_window_size": full.tokens + 8191})
    with pytest.raises(ContextError, match="input token budget"):
        budget.assert_fits(limited, full.tokens)
