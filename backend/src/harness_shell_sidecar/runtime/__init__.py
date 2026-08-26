"""Public Sidecar runtime API."""

from .dispatcher import DispatchError, DispatchResult, RequestDispatcher
from .messages import (
    InitializeRequestPayload,
    RuntimeCapabilities,
    RuntimeInitializationFailure,
    RuntimePhase,
)
from .router import Router
from .service import SidecarService
from .stdio import StdioTransport

__all__ = [
    "DispatchError",
    "DispatchResult",
    "InitializeRequestPayload",
    "Router",
    "RuntimeCapabilities",
    "RuntimeInitializationFailure",
    "RuntimePhase",
    "RequestDispatcher",
    "SidecarService",
    "StdioTransport",
]
