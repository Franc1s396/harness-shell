"""Protocol-specific exceptions."""


class ProtocolError(ValueError):
    """Base class for terminal protocol failures."""


class ProtocolViolation(ProtocolError):
    """Raised when bytes do not conform to protocol v1."""


class FrameTooLarge(ProtocolViolation):
    """Raised when a declared or encoded payload exceeds the hard limit."""

    def __init__(self, actual: int, maximum: int) -> None:
        """记录实际大小和协议上限，并构造可诊断的错误消息。"""

        super().__init__(
            f"frame payload is {actual} bytes; maximum is {maximum} bytes"
        )
        self.actual = actual  # 实际声明或编码出的正文大小（字节）。
        self.maximum = maximum  # 协议允许的最大正文大小（字节）。
