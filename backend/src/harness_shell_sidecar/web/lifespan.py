"""ASGI lifespan owner for the unique initialized runtime resource graph."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI

from harness_shell_sidecar.runtime.models import (
    RuntimeInitializeRequest,
    RuntimePhase,
)
from harness_shell_sidecar.runtime.resources import EventSink, RuntimeResources

from .websocket import RuntimeWebSocketGateway


ResourceFactory = Callable[
    [RuntimeInitializeRequest, EventSink], RuntimeResources
]


class RuntimeOwnerError(RuntimeError):
    """Describe a stable invalid operation on the runtime lifecycle owner."""

    def __init__(self, error_code: str, message: str) -> None:
        """Retain only a stable code and safe public lifecycle message."""

        super().__init__(message)
        self.error_code = error_code  # HTTP mapping uses this stable identifier.
        self.public_message = message  # Safe bounded text without resource details.


class RuntimeOwner:
    """Initialize, expose, and converge exactly one RuntimeResources instance."""

    def __init__(self, resource_factory: ResourceFactory) -> None:
        """Create a live but not initialized owner and bounded future event queue."""

        self._resource_factory = resource_factory  # Atomic graph constructor.
        self._resources: RuntimeResources | None = None  # Sole graph reference.
        self._state = RuntimePhase.LIVE_NOT_INITIALIZED  # Shared public phase.
        self._initialize_lock = Lock()  # Serialize the unique sync initialization.
        self.websocket_gateway = RuntimeWebSocketGateway()

    async def event_sink(self, event: dict[str, object]) -> None:
        """Apply bounded backpressure to domain events until WebSocket delivery."""

        await self.websocket_gateway.publish_domain_event(event)

    def initialize_once(
        self,
        payload: RuntimeInitializeRequest,
        event_sink: EventSink,
    ) -> RuntimeResources:
        """Construct and publish one complete graph or enter terminal FAILED."""

        with self._initialize_lock:
            if self._state is not RuntimePhase.LIVE_NOT_INITIALIZED:
                raise RuntimeOwnerError(
                    "RUNTIME_ALREADY_INITIALIZED",
                    "Runtime initialization has already been attempted",
                )
            self._state = RuntimePhase.INITIALIZING
            try:
                resources = self._resource_factory(payload, event_sink)
            except BaseException:
                self._state = RuntimePhase.FAILED
                raise
            self._resources = resources
            self._state = RuntimePhase.READY
            return resources

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
            if self._state is RuntimePhase.LIVE_NOT_INITIALIZED:
                self._state = RuntimePhase.STOPPED
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
    payload: RuntimeInitializeRequest,
    event_sink: EventSink,
) -> RuntimeResources:
    """Construct the production runtime graph without adding import side effects."""

    return RuntimeResources.initialize(payload, event_sink)


@asynccontextmanager
async def runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the unique RuntimeOwner for the entire ASGI application lifespan."""

    factory = app.state.resource_factory
    owner = RuntimeOwner(factory)
    app.state.runtime_owner = owner
    try:
        yield
    finally:
        await owner.shutdown()
