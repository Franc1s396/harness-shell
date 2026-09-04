"""ASGI lifespan owner for the unique initialized runtime resource graph."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from harness_shell_sidecar.runtime.models import RuntimePhase
from harness_shell_sidecar.runtime.resources import EventSink, RuntimeResources
from harness_shell_sidecar.runtime.settings import RuntimeSettings

from .websocket import RuntimeWebSocketGateway


ResourceFactory = Callable[[RuntimeSettings, EventSink], RuntimeResources]


class RuntimeOwnerError(RuntimeError):
    """Describe a stable invalid operation on the runtime lifecycle owner."""

    def __init__(self, error_code: str, message: str) -> None:
        """Retain only a stable code and safe public lifecycle message."""

        super().__init__(message)
        self.error_code = error_code  # HTTP mapping uses this stable identifier.
        self.public_message = message  # Safe bounded text without resource details.
        self.safe_message = message  # Internal diagnostic alias.


class RuntimeOwner:
    """Initialize, expose, and converge exactly one RuntimeResources instance."""

    def __init__(
        self,
        settings: RuntimeSettings,
        resource_factory: ResourceFactory,
    ) -> None:
        """Create an owner that must initialize before ASGI accepts requests."""

        self._settings = settings  # Fixed paths derived from the CLI data directory.
        self._resource_factory = resource_factory  # Atomic graph constructor.
        self._resources: RuntimeResources | None = None  # Sole graph reference.
        self._state = RuntimePhase.INITIALIZING  # Shared public phase.
        self._start_attempted = False  # Lifespan startup is one-shot.
        self.websocket_gateway = RuntimeWebSocketGateway()

    async def start(self, event_sink: EventSink) -> RuntimeResources:
        """Initialize all resources before ASGI accepts requests."""

        if self._start_attempted:
            raise RuntimeOwnerError(
                "RUNTIME_ALREADY_INITIALIZED",
                "Runtime initialization has already been attempted",
            )
        self._start_attempted = True
        try:
            resources = self._resource_factory(self._settings, event_sink)
        except BaseException:
            self._state = RuntimePhase.FAILED
            raise
        self._resources = resources
        self._state = RuntimePhase.READY
        return resources

    async def event_sink(self, event: dict[str, object]) -> None:
        """Apply bounded backpressure to domain events until WebSocket delivery."""

        await self.websocket_gateway.publish_domain_event(event)

    def state(self) -> RuntimePhase:
        """Return the current safe lifecycle phase."""

        return self._state

    def require_resources(self) -> RuntimeResources:
        """Return the ready graph or reject application work fail closed."""

        if self._state is not RuntimePhase.READY or self._resources is None:
            raise RuntimeOwnerError("RUNTIME_NOT_READY", "Runtime is not ready")
        return self._resources

    async def shutdown(self) -> RuntimePhase:
        """Converge the graph exactly once and retain terminal failure state."""

        resources = self._resources
        if resources is None:
            return self._state
        if self._state in (RuntimePhase.STOPPED, RuntimePhase.FAILED):
            return self._state
        self._state = RuntimePhase.DRAINING
        try:
            await resources.shutdown()
        except BaseException:
            self._state = RuntimePhase.FAILED
            raise
        finally:
            self._resources = None
        self._state = RuntimePhase.STOPPED
        return self._state


def default_resource_factory(
    settings: RuntimeSettings,
    event_sink: EventSink,
) -> RuntimeResources:
    """Construct the plaintext graph without injected Runtime keys."""

    return RuntimeResources.initialize_from_settings(settings, event_sink)


@asynccontextmanager
async def runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the unique RuntimeOwner for the entire ASGI application lifespan."""

    factory = app.state.resource_factory
    owner = RuntimeOwner(app.state.settings, factory)
    app.state.runtime_owner = owner
    await owner.start(owner.event_sink)
    try:
        yield
    finally:
        await owner.shutdown()
