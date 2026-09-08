"""审核决定的关联、单次消费和等待取消边界。"""

import asyncio
from uuid import uuid4

import pytest

from harness_shell_sidecar.agent.approval_models import ApprovalDecision, ApprovalRequest, ApprovalTarget
from harness_shell_sidecar.agent.approvals import ApprovalError, ApprovalRegistry
from harness_shell_sidecar.agent.contracts import ExecuteCommandArguments


def approval_request() -> ApprovalRequest:
    """构建独立且不接触真实主机的冻结请求。"""
    return ApprovalRequest(
        approval_id=uuid4(), conversation_id=uuid4(), agent_run_id=uuid4(),
        ssh_session_id=uuid4(), tool_call_id="call-1",
        arguments=ExecuteCommandArguments(command="touch /tmp/hitl-probe"),
        target=ApprovalTarget(display_name="test", host="localhost", port=22, username="tester"),
    )


def decision_for(request: ApprovalRequest, decision: str = "approve") -> ApprovalDecision:
    """按原请求身份生成决定，未知 decision 由严格模型拒绝。"""
    return ApprovalDecision(**request.model_dump(include={
        "conversation_id", "agent_run_id", "ssh_session_id", "tool_call_id",
    }), decision=decision)


def test_approval_can_only_be_consumed_once() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    assert registry.decide(request.approval_id, decision_for(request)).status == "APPROVED"
    assert registry.decide(request.approval_id, decision_for(request)).status == "APPROVED"
    registry.consume(request)
    with pytest.raises(ApprovalError, match="already consumed"):
        registry.consume(request)


def test_reject_cannot_execute_or_change_decision() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    registry.decide(request.approval_id, decision_for(request, "reject"))
    with pytest.raises(ApprovalError):
        registry.consume(request)
    with pytest.raises(ApprovalError):
        registry.decide(request.approval_id, decision_for(request))


def test_wrong_identity_and_second_pending_are_rejected() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    wrong = decision_for(request).model_copy(update={"ssh_session_id": uuid4()})
    with pytest.raises(ApprovalError):
        registry.decide(request.approval_id, wrong)
    with pytest.raises(ApprovalError):
        registry.register(request.model_copy(update={"approval_id": uuid4()}))


def test_cancel_after_approve_prevents_dispatch_and_release_removes_record() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    registry.decide(request.approval_id, decision_for(request))
    registry.invalidate_run(request.agent_run_id, "run_cancelled")
    with pytest.raises(ApprovalError):
        registry.consume(request)
    registry.release_run(request.agent_run_id)
    with pytest.raises(ApprovalError) as error:
        registry.decide(request.approval_id, decision_for(request))
    assert error.value.error_code == "AGENT_APPROVAL_NOT_FOUND"


@pytest.mark.anyio
async def test_wait_has_no_timeout_and_cancellation_does_not_destroy_decision() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    waiter = asyncio.create_task(registry.wait(request.approval_id))
    await asyncio.sleep(0)
    assert not waiter.done()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    registry.decide(request.approval_id, decision_for(request))
    assert (await registry.wait(request.approval_id)).status == "APPROVED"


@pytest.mark.anyio
async def test_invalidation_wakes_pending_waiter() -> None:
    registry = ApprovalRegistry()
    request = approval_request()
    registry.register(request)
    registry.invalidate_run(request.agent_run_id, "session_unavailable")
    assert (await registry.wait(request.approval_id)).status == "INVALIDATED"


@pytest.fixture
def anyio_backend() -> str:
    """与应用一致，仅在 asyncio 后端运行等待测试。"""
    return "asyncio"
