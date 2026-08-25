from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from harness_shell_sidecar.protocol import (
    FrameDecoder,
    FrameEnvelope,
    FrameTooLarge,
    MessageType,
    ProtocolViolation,
    Sensitivity,
    encode_frame,
)


def heartbeat(sequence: int = 1) -> FrameEnvelope:
    return FrameEnvelope(
        protocol_version=1,
        message_type=MessageType.HEARTBEAT,
        request_id=UUID("018f3f83-7a53-7b5d-9c4e-1b2f68e27911"),
        task_id=None,
        workflow_run_id=None,
        sequence=sequence,
        timestamp=datetime(2026, 8, 25, tzinfo=timezone.utc),
        sensitivity=Sensitivity.NORMAL,
        payload={"kind": "ping"},
    )


def test_round_trip_survives_every_chunk_boundary() -> None:
    encoded = encode_frame(heartbeat())

    for split in range(1, len(encoded)):
        decoder = FrameDecoder()
        assert decoder.feed(encoded[:split]) == []
        assert decoder.feed(encoded[split:]) == [heartbeat()]


def test_decoder_returns_multiple_frames_from_one_chunk() -> None:
    decoder = FrameDecoder()

    frames = decoder.feed(encode_frame(heartbeat(1)) + encode_frame(heartbeat(2)))

    assert frames == [heartbeat(1), heartbeat(2)]


def test_decoder_rejects_payload_over_one_megabyte() -> None:
    decoder = FrameDecoder()

    with pytest.raises(FrameTooLarge, match="1048576"):
        decoder.feed(b"Content-Length: 1048577\r\n\r\n")


@pytest.mark.parametrize(
    "wire",
    (
        b"Content-Length: 2\r\nX-Test: 1\r\n\r\n{}",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"content-length: 2\r\n\r\n{}",
        b"Content-Length: -1\r\n\r\n",
        b"Content-Length: abc\r\n\r\n",
    ),
)
def test_decoder_rejects_invalid_headers(wire: bytes) -> None:
    decoder = FrameDecoder()

    with pytest.raises(ProtocolViolation):
        decoder.feed(wire)


def test_decoder_rejects_header_over_limit_before_delimiter() -> None:
    decoder = FrameDecoder()

    with pytest.raises(ProtocolViolation, match="8192"):
        decoder.feed(b"Content-Length: " + b"1" * 8_192)


@pytest.mark.parametrize(
    "body",
    (
        b"\xff",
        b"{",
        b"[]",
        b'{"protocol_version":1}',
    ),
)
def test_decoder_rejects_invalid_payloads(body: bytes) -> None:
    decoder = FrameDecoder()
    wire = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    with pytest.raises(ProtocolViolation):
        decoder.feed(wire)


def test_decoder_discards_buffer_after_terminal_violation() -> None:
    decoder = FrameDecoder()

    with pytest.raises(ProtocolViolation):
        decoder.feed(b"Content-Length: 2\r\nX-Test: 1\r\n\r\n{}")

    assert decoder.feed(encode_frame(heartbeat())) == [heartbeat()]

