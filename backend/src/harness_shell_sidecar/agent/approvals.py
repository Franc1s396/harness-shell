"""同一事件循环内的审核决定注册表，不拥有图或恢复逻辑。"""

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from .approval_models import ApprovalDecision, ApprovalRequest, ApprovalResolution, InvalidationReason


class ApprovalError(RuntimeError):
    """以固定安全原因报告审核关联或状态冲突。"""

    def __init__(self, error_code: str, safe_message: str) -> None:
        """保存结构化错误，不包含命令或目标内容。"""
        super().__init__(safe_message)
        self.error_code = error_code  # HTTP/Service 的稳定错误分类。
        self.safe_message = safe_message  # 可公开且不含秘密的失败说明。


@dataclass(slots=True)
class _ApprovalEntry:
    """拥有一项不可变请求、决定通知及单次执行权。"""

    request: ApprovalRequest  # Run 内原样冻结的审核内容。
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # 等待者只借用此通知。
    resolution: ApprovalResolution | None = None  # None 表示仍在等待用户，无计时器。
    consumed: bool = False  # 一旦消费不可退回，即使随后执行失败。
    active: bool = True  # Run 结束/会话断开即禁止决定或消费。


class ApprovalRegistry:
    """拥有活动 Run 的决定记录，所有状态转换无 await 且只在事件循环执行。"""

    def __init__(self) -> None:
        """建立进程内索引，不打开数据库或创建后台任务。"""
        self._entries: dict[UUID, _ApprovalEntry] = {}  # Run 结束后删除，包括已决定记录。

    def register(self, request: ApprovalRequest) -> None:
        """登记一个新的 pending，拒绝重复 ID 或同 Run 第二个 pending。"""
        if request.approval_id in self._entries or any(
            entry.request.agent_run_id == request.agent_run_id and entry.resolution is None
            for entry in self._entries.values()
        ):
            raise ApprovalError("AGENT_APPROVAL_CONFLICT", "an approval is already pending or registered")
        self._entries[request.approval_id] = _ApprovalEntry(request)

    def _entry(self, approval_id: UUID) -> _ApprovalEntry:
        """查找原记录，不重建已经释放的授权。"""
        entry = self._entries.get(approval_id)
        if entry is None:
            raise ApprovalError("AGENT_APPROVAL_NOT_FOUND", "the approval does not exist")
        return entry

    def decide(self, approval_id: UUID, decision: ApprovalDecision) -> ApprovalResolution:
        """精确匹配身份并原子提交；同决定重复提交不再次唤醒恢复。"""
        entry = self._entry(approval_id)
        for name in ("conversation_id", "agent_run_id", "ssh_session_id", "tool_call_id"):
            if getattr(entry.request, name) != getattr(decision, name):
                raise ApprovalError("AGENT_APPROVAL_CONFLICT", "approval identity does not match")
        if not entry.active:
            raise ApprovalError("AGENT_APPROVAL_INACTIVE", "the approval is no longer active")
        status = "APPROVED" if decision.decision == "approve" else "REJECTED"
        if entry.resolution is not None:
            if entry.resolution.status != status:
                raise ApprovalError("AGENT_APPROVAL_CONFLICT", "a different decision was already submitted")
            return entry.resolution
        entry.resolution = ApprovalResolution(
            approval_id=approval_id, status=status,
            reason="user_approved" if decision.decision == "approve" else "user_rejected",
        )
        entry.changed.set()
        return entry.resolution

    async def wait(self, approval_id: UUID) -> ApprovalResolution:
        """不限时等待明确决定或失效，等待者取消不破坏记录。"""
        entry = self._entry(approval_id)
        await entry.changed.wait()
        if entry.resolution is None:
            raise RuntimeError("approval notification has no resolution")
        return entry.resolution

    def consume(self, request: ApprovalRequest) -> None:
        """派发前精确核对整个操作并消费一次执行权。"""
        entry = self._entry(request.approval_id)
        if entry.request != request:
            raise ApprovalError("AGENT_APPROVAL_CONFLICT", "approved operation does not match")
        if not entry.active or entry.resolution is None or entry.resolution.status != "APPROVED":
            raise ApprovalError("AGENT_APPROVAL_INACTIVE", "the operation is not approved and active")
        if entry.consumed:
            raise ApprovalError("AGENT_APPROVAL_CONFLICT", "the execution authorization was already consumed")
        entry.consumed = True

    def invalidate_run(self, agent_run_id: UUID, reason: InvalidationReason) -> None:
        """唤醒所有 pending 并阻止消费，保留已通过/拒绝的历史事实。"""
        for entry in self._entries.values():
            if entry.request.agent_run_id != agent_run_id:
                continue
            entry.active = False
            if entry.resolution is None:
                entry.resolution = ApprovalResolution(
                    approval_id=entry.request.approval_id, status="INVALIDATED", reason=reason,
                )
                entry.changed.set()

    def release_run(self, agent_run_id: UUID) -> None:
        """Run 结束后释放其全部记录，不留下跨轮可恢复授权。"""
        self.invalidate_run(agent_run_id, "run_cancelled")
        for approval_id in [key for key, value in self._entries.items() if value.request.agent_run_id == agent_run_id]:
            del self._entries[approval_id]
