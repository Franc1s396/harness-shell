"""Public protocol v1 contract."""

from .codec import FrameDecoder, encode_frame
from .errors import FrameTooLarge, ProtocolError, ProtocolViolation
from .models import (
    MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    PROTOCOL_VERSION,
    FrameEnvelope,
    MessageType,
    Sensitivity,
)

__all__ = [
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PROTOCOL_VERSION",
    "FrameDecoder",
    "FrameEnvelope",
    "FrameTooLarge",
    "MessageType",
    "ProtocolError",
    "ProtocolViolation",
    "Sensitivity",
    "encode_frame",
]

