"""Transport-independent request identity and cooperative cancellation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID


class RequestCancelledError(RuntimeError):
    """Signal that the dispatcher-owned cancel event won before dispatch."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Carry one request identity and its dispatcher-owned cancel signal."""

    #: Stable identity shared by the inbound adapter, application, and response.
    request_id: UUID
    #: Dispatcher-owned signal checked before an external side effect begins.
    cancelled: asyncio.Event

    def require_active(self) -> None:
        """Raise the stable dispatch cancellation error once cancellation wins."""

        if self.cancelled.is_set():
            raise RequestCancelledError(
                "request was cancelled before application execution"
            )
