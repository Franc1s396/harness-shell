"""Strict public events and non-persisted sinks for one Agent turn stream."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .contracts import AgentRun


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
    """Carry immutable request and durable Run correlation on every event."""

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
    """Open a stream only after its durable Run is in RUNNING state."""

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


class AgentTurnTextDeltaEvent(_AgentTurnEventBase):
    """Carry one exact non-empty piece of final visible model text."""

    type: Literal["agent.turn.text_delta"] = Field(
        default="agent.turn.text_delta",
        description="Visible text delta event discriminator.",
    )
    delta: VisibleDelta = Field(
        description="Exact visible model text without trimming or post-processing."
    )


class AgentTurnCompletedEvent(_AgentTurnEventBase):
    """Close a stream after the full message and successful Run are durable."""

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
    """Close a stream after a failed, limited, or cancelled Run is durable."""

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
    | AgentTurnTextDeltaEvent
    | AgentTurnCompletedEvent
    | AgentTurnFailedEvent,
    Field(discriminator="type"),
]


class AgentTextDeltaSink(Protocol):
    """Receive exact visible text from one Provider invocation."""

    async def text_delta(self, delta: str) -> None:
        """Publish one non-empty exact visible-text delta."""


class AgentTurnEventSink(AgentTextDeltaSink, Protocol):
    """Receive lifecycle events for one durable Agent Run."""

    @property
    def streamed_text(self) -> str:
        """Return the exact concatenation of emitted visible deltas."""

    async def started(self, run: AgentRun) -> None:
        """Publish the first event only after the durable Run exists."""

    async def completed(self, run: AgentRun) -> None:
        """Publish success only after the Run and final message are durable."""

    async def failed(self, run: AgentRun, message: str) -> None:
        """Publish one reviewed message after the Run is durably terminal."""


__all__ = [
    "AgentTextDeltaSink",
    "AgentTurnCompletedEvent",
    "AgentTurnEventSink",
    "AgentTurnFailedEvent",
    "AgentTurnStartedEvent",
    "AgentTurnStreamEvent",
    "AgentTurnTextDeltaEvent",
]
