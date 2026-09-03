"""Transport-independent runtime lifecycle models."""

from __future__ import annotations

import re
from enum import StrEnum

# Shared encoded JSON boundary used by the HTTP request, response, and Agent
# result preflight paths. Keeping one value prevents transport-layer drift.
MAX_JSON_BODY_BYTES = 1_048_576
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class RuntimeInitializationFailure(RuntimeError):
    """Expose one validated, non-secret initialization failure."""

    def __init__(self, error_code: str, public_message: str) -> None:
        """Retain only a stable code and an explicitly public message."""

        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("runtime error code must use uppercase identifiers")
        super().__init__(public_message)
        self.error_code = error_code  # Stable HTTP-facing identifier.
        self.public_message = public_message  # Safe bounded failure text.


class RuntimePhase(StrEnum):
    """Describe the single runtime owner from live startup through cleanup."""

    #: Runtime resources are being opened and verified atomically.
    INITIALIZING = "INITIALIZING"
    #: All resources are valid and application operations may be dispatched.
    READY = "READY"
    #: New requests are rejected while active dispatcher work is cancelled.
    DRAINING = "DRAINING"
    #: Domain owners are converging and closing their remote resources.
    CONVERGING = "CONVERGING"
    #: Observability, keys, and local persistence are being closed.
    CLOSING = "CLOSING"
    #: The complete runtime graph has been released successfully.
    STOPPED = "STOPPED"
    #: Initialization or convergence failed and the runtime cannot be reused.
    FAILED = "FAILED"
