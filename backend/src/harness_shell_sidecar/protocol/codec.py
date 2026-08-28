"""Length-prefixed protocol v1 framing."""

from pydantic import ValidationError

from .errors import FrameTooLarge, ProtocolViolation
from .models import MAX_HEADER_BYTES, MAX_PAYLOAD_BYTES, FrameEnvelope


_HEADER_DELIMITER = b"\r\n\r\n"
_CONTENT_LENGTH_PREFIX = b"Content-Length: "


def encode_frame(frame: FrameEnvelope) -> bytes:
    body = frame.model_dump_json(exclude_none=False).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise FrameTooLarge(len(body), MAX_PAYLOAD_BYTES)
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class FrameDecoder:
    """Incrementally decode complete frames from arbitrary byte chunks."""

    def __init__(self) -> None:
        """创建一个尚未接收任何协议字节的增量解码器。"""

        # 保存尚不足以组成完整帧的跨 chunk 字节。
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[FrameEnvelope]:
        """追加字节并返回本次能够完整解码出的所有协议帧。"""

        self._buffer.extend(chunk)
        frames: list[FrameEnvelope] = []

        try:
            while True:
                header_end = self._buffer.find(_HEADER_DELIMITER)
                if header_end < 0:
                    if len(self._buffer) > MAX_HEADER_BYTES:
                        raise ProtocolViolation(
                            f"frame header exceeds {MAX_HEADER_BYTES} bytes"
                        )
                    return frames

                if header_end > MAX_HEADER_BYTES:
                    raise ProtocolViolation(
                        f"frame header exceeds {MAX_HEADER_BYTES} bytes"
                    )

                content_length = self._parse_header(bytes(self._buffer[:header_end]))
                if content_length > MAX_PAYLOAD_BYTES:
                    raise FrameTooLarge(content_length, MAX_PAYLOAD_BYTES)

                body_start = header_end + len(_HEADER_DELIMITER)
                frame_end = body_start + content_length
                if len(self._buffer) < frame_end:
                    return frames

                body = bytes(self._buffer[body_start:frame_end])
                del self._buffer[:frame_end]
                frames.append(self._parse_body(body))
        except (ProtocolViolation, FrameTooLarge):
            self._buffer.clear()
            raise

    @staticmethod
    def _parse_header(header: bytes) -> int:
        """严格解析单行 Content-Length 帧头并返回正文长度。"""

        lines = header.split(b"\r\n")
        if len(lines) != 1 or not lines[0].startswith(_CONTENT_LENGTH_PREFIX):
            raise ProtocolViolation(
                "frame header must contain exactly one case-sensitive Content-Length"
            )

        value = lines[0][len(_CONTENT_LENGTH_PREFIX) :]
        if not value or not value.isdigit():
            raise ProtocolViolation("Content-Length must be an unsigned decimal integer")

        return int(value)

    @staticmethod
    def _parse_body(body: bytes) -> FrameEnvelope:
        """把 UTF-8 JSON 正文校验为唯一允许的 v1 信封模型。"""

        try:
            return FrameEnvelope.model_validate_json(body)
        except (ValidationError, UnicodeDecodeError, ValueError) as exc:
            raise ProtocolViolation("frame payload is not a valid v1 envelope") from exc
