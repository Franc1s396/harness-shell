"""Protocol-specific exceptions."""


class ProtocolError(ValueError):
    """Base class for terminal protocol failures."""


class ProtocolViolation(ProtocolError):
    """Raised when bytes do not conform to protocol v1."""


class FrameTooLarge(ProtocolViolation):
    """Raised when a declared or encoded payload exceeds the hard limit."""

    def __init__(self, actual: int, maximum: int) -> None:
        super().__init__(
            f"frame payload is {actual} bytes; maximum is {maximum} bytes"
        )
        self.actual = actual
        self.maximum = maximum

