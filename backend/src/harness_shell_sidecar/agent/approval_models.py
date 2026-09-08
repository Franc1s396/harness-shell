"""审核请求及决定的严格内存与传输契约。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .contracts import ExecuteCommandArguments

ApprovalStatus = Literal["APPROVED", "REJECTED", "INVALIDATED"]
InvalidationReason = Literal["session_unavailable", "run_cancelled"]


class ApprovalTarget(BaseModel):
    """已建立 SSH 会话的非秘密目标快照，不重新解析可变配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=80)] = Field(description="建立会话时的显示名。")
    host: Annotated[str, StringConstraints(min_length=1, max_length=255)] = Field(description="建立会话时的目标主机。")
    port: Annotated[int, Field(ge=1, le=65535)] = Field(description="目标 SSH 端口。")
    username: Annotated[str, StringConstraints(min_length=1, max_length=128)] = Field(description="已认证的 SSH 用户。")


class ApprovalIdentity(BaseModel):
    """冻结审核所属的对话、Run、SSH 会话和工具调用。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    conversation_id: UUID = Field(description="所属对话，不可由恢复请求更换。")
    agent_run_id: UUID = Field(description="唯一 Run，内存 checkpoint 的所有者。")
    ssh_session_id: UUID = Field(description="执行目标会话，不允许重新绑定。")
    tool_call_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(description="模型调用关联 ID。")


class ApprovalRequest(ApprovalIdentity):
    """需要人工决定的一次完整操作，仅存在于本 Run 内存。"""

    approval_id: UUID = Field(description="本次申请的随机单次授权标识。")
    arguments: ExecuteCommandArguments = Field(description="原样冻结的待执行命令参数。")
    target: ApprovalTarget = Field(description="来自已建立会话的显示快照。")


class ApprovalDecision(ApprovalIdentity):
    """用户仅能决定通过或拒绝，不携带可替换命令。"""

    decision: Literal["approve", "reject"] = Field(description="当前操作的用户决定。")


class ApprovalResolution(BaseModel):
    """决定已提交或等待已失效，不代表远程执行成功。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    approval_id: UUID = Field(description="原审核标识。")
    status: ApprovalStatus = Field(description="不可逆的审核终态。")
    reason: Literal["user_approved", "user_rejected", "session_unavailable", "run_cancelled"] = Field(description="安全的决定或失效原因。")
