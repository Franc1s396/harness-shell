"""Typed deterministic fakes shared by Agent graph and gateway tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, AnyMessage

from harness_shell_sidecar.agent.contracts import AgentTurnInput


class FakeBoundModel:
    """Return or raise queued outcomes while recording tool binding and calls."""

    def __init__(self, outcomes: Sequence[object] | None = None) -> None:
        """Copy deterministic outcomes into an isolated mutable queue."""

        self.outcomes = list(outcomes or [])  # Remaining invoke outcomes.
        self.calls = 0  # Number of ainvoke attempts.
        self.message_calls: list[list[AnyMessage]] = []  # Inputs per attempt.
        self.bound_tools: list[object] = []  # Tools supplied to bind_tools.
        self.bind_kwargs: dict[str, object] = {}  # Strict binding options.

    def bind_tools(
        self,
        tools: Sequence[object],
        **kwargs: object,
    ) -> FakeBoundModel:
        """Record the schema binding and keep this fake as the bound runnable."""

        self.bound_tools = list(tools)
        self.bind_kwargs = dict(kwargs)
        return self

    async def ainvoke(self, messages: Sequence[AnyMessage]) -> object:
        """Consume one queued outcome, awaiting an Event when cancellation tests block."""

        self.calls += 1
        self.message_calls.append(list(messages))
        if not self.outcomes:
            raise AssertionError("FakeBoundModel outcome queue is empty")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, asyncio.Event):
            await outcome.wait()
            return AIMessage(content="released")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingModelBuilder:
    """Record ChatOpenAI constructor kwargs and return one fixed fake model."""

    def __init__(self, model: FakeBoundModel) -> None:
        """Store the fake returned by every builder invocation."""

        self.model = model  # Constructor result used by the gateway.
        self.kwargs: dict[str, object] = {}  # Most recent constructor kwargs.
        self.calls = 0  # Number of model construction attempts.

    def __call__(self, **kwargs: object) -> FakeBoundModel:
        """Capture exact constructor arguments without contacting a provider."""

        self.calls += 1
        self.kwargs = dict(kwargs)
        return self.model


class FakeModelSequence(FakeBoundModel):
    """Queue graph-level model messages while retaining every input sequence."""

    def queue(self, *outcomes: object) -> None:
        """Append deterministic model outcomes in invocation order."""

        self.outcomes.extend(outcomes)


class CancellationAwareModel(FakeModelSequence):
    """Block one invocation and expose whether task cancellation reached it."""

    def __init__(self) -> None:
        """Create lifecycle events for deterministic outer-cancellation tests."""

        super().__init__()
        self.started = asyncio.Event()  # Signals entry into the provider operation.
        self.stopped = asyncio.Event()  # Signals its cancellation-safe finalizer.
        self.release = asyncio.Event()  # Test-only escape hatch for failed cleanup.

    async def ainvoke(self, messages: Sequence[AnyMessage]) -> object:
        """Remain active until released or cancelled, always recording finalization."""

        self.calls += 1
        self.message_calls.append(list(messages))
        self.started.set()
        try:
            await self.release.wait()
            return AIMessage(content="released")
        finally:
            self.stopped.set()


async def instant_sleep(_: float) -> None:
    """Complete retry delays immediately in deterministic tests."""


def make_tool_call(call_id: str, command: str) -> dict[str, object]:
    """Build one canonical LangChain execute_command call."""

    return {
        "name": "execute_command",
        "args": {"command": command},
        "id": call_id,
        "type": "tool_call",
    }


def make_turn_input(*, conversation_id: UUID | None = None) -> AgentTurnInput:
    """Build a valid graph-service input with fresh opaque IDs."""

    return AgentTurnInput(
        conversation_id=conversation_id,
        ssh_session_id=uuid4(),
        api_config_id=uuid4(),
        user_message="inspect the remote host",
    )
