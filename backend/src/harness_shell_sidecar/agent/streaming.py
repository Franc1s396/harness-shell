"""单个 Agent 轮次流的严格公开事件和非持久化接收端。"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .contracts import AgentRun, ExecuteCommandArguments


VisibleDelta = Annotated[
    str,
    StringConstraints(min_length=1, max_length=65_536),
]
StableErrorCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$"),
]
SafeFailureMessage = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
]


class _AgentTurnEventBase(BaseModel):
    """每个事件携带不可变请求标识及持久化 Run 关联。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = Field(
        default=1,
        description="Frozen Agent turn SSE schema version.",
    )
    request_id: UUID = Field(
        description="HTTP request correlation identifier echoed by every event."
    )
    sequence: Annotated[int, Field(ge=0, le=2**53 - 1)] = Field(
        description="Contiguous JavaScript-safe event sequence number."
    )
    conversation_id: UUID = Field(
        description="Conversation identity frozen for the complete stream."
    )
    agent_run_id: UUID = Field(
        description="Durable Agent Run identity frozen for the complete stream."
    )


class AgentTurnStartedEvent(_AgentTurnEventBase):
    """持久化 Run 进入 RUNNING 后才开启流。"""

    type: Literal["agent.turn.started"] = Field(
        default="agent.turn.started",
        description="Started event discriminator.",
    )
    status: Literal["RUNNING"] = Field(
        default="RUNNING",
        description="Durable status at the HTTP success boundary.",
    )
    react_iteration: Literal[0] = Field(
        default=0,
        description="No tool loop has completed when a Run starts.",
    )


class AgentTurnToolStartedEvent(_AgentTurnEventBase):
    """记录即将进入执行器的工具与完整参数，不携带远程输出。"""

    type: Literal["agent.turn.tool_started"] = Field(
        default="agent.turn.tool_started", description="工具执行前的非持久化状态事件。",
    )

    tool_call_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] = Field(description="本次模型工具调用标识。")
    tool_name: Literal["execute_command"] = Field(description="通过本地校验的工具名称。")
    arguments: ExecuteCommandArguments = Field(description="传给执行器的完整已校验参数。")


class AgentTurnTextDeltaEvent(_AgentTurnEventBase):
    """携带一段精确非空的模型可见文本（包括工具前说明）。"""

    type: Literal["agent.turn.text_delta"] = Field(
        default="agent.turn.text_delta",
        description="Visible text delta event discriminator.",
    )
    delta: VisibleDelta = Field(
        description="Exact visible model text without trimming or post-processing."
    )


class AgentTurnTextReplaceEvent(_AgentTurnEventBase):
    """用完整快照替换当前可见文本，空字符串表示清空。"""

    type: Literal["agent.turn.text_replace"] = Field(
        default="agent.turn.text_replace", description="Full visible text replacement discriminator.",
    )
    text: Annotated[str, StringConstraints(max_length=65_536)] = Field(
        description="Replacement visible text; an empty string clears the provisional text."
    )


class AgentTurnCompletedEvent(_AgentTurnEventBase):
    """完整消息与成功 Run 均持久化后关闭流。"""

    type: Literal["agent.turn.completed"] = Field(
        default="agent.turn.completed",
        description="Successful terminal event discriminator.",
    )
    status: Literal["COMPLETED"] = Field(
        default="COMPLETED",
        description="Durable successful Run status.",
    )
    react_iteration: Annotated[int, Field(ge=0, le=128)] = Field(
        description="Number of completed ReAct tool loops."
    )
    error_code: None = Field(
        default=None,
        description="Successful terminal events never contain an error code.",
    )


class AgentTurnFailedEvent(_AgentTurnEventBase):
    """失败、受限或取消 Run 持久化后关闭流。"""

    type: Literal["agent.turn.failed"] = Field(
        default="agent.turn.failed",
        description="Failure terminal event discriminator.",
    )
    status: Literal["FAILED", "LIMIT_REACHED", "CANCELLED"] = Field(
        description="Durable unsuccessful Run status."
    )
    react_iteration: Annotated[int, Field(ge=0, le=128)] = Field(
        description="Number of completed ReAct tool loops before failure."
    )
    error_code: StableErrorCode = Field(
        description="Stable non-sensitive machine-readable failure code."
    )
    message: SafeFailureMessage = Field(
        description="Bounded safe failure explanation without remote output."
    )


AgentTurnStreamEvent: TypeAlias = Annotated[
    AgentTurnStartedEvent
    | AgentTurnToolStartedEvent
    | AgentTurnTextDeltaEvent
    | AgentTurnTextReplaceEvent
    | AgentTurnCompletedEvent
    | AgentTurnFailedEvent,
    Field(discriminator="type"),
]


class AgentTextDeltaSink(Protocol):
    """接收一次 Provider 调用的精确可见文本。"""

    @property
    def streamed_text(self) -> str:
        """返回已发布的当前文本快照。"""

    async def text_replace(self, text: str) -> None:
        """用快照更新当前可见文本，允许清空。"""

    async def text_delta(self, delta: str) -> None:
        """发布一段精确非空的可见文本增量。"""


class AgentTurnEventSink(AgentTextDeltaSink, Protocol):
    """接收一个持久化 Agent Run 的生命周期事件。"""

    @property
    def streamed_text(self) -> str:
        """返回应用增量和替换事件后的当前文本快照。"""

    async def started(self, run: AgentRun) -> None:
        """持久化 Run 已存在后才发布首个事件。"""

    async def tool_started(self, tool_call_id: str, arguments: ExecuteCommandArguments) -> None:
        """校验通过后、实际调用执行器之前发布工具状态。"""

    async def completed(self, run: AgentRun) -> None:
        """Run 和最终消息持久化后才发布成功事件。"""

    async def failed(self, run: AgentRun, message: str) -> None:
        """Run 终态持久化后发布经过审查的消息。"""


__all__ = [
    "AgentTextDeltaSink",
    "AgentTurnCompletedEvent",
    "AgentTurnEventSink",
    "AgentTurnFailedEvent",
    "AgentTurnStartedEvent",
    "AgentTurnToolStartedEvent",
    "AgentTurnStreamEvent",
    "AgentTurnTextDeltaEvent",
    "AgentTurnTextReplaceEvent",
]
