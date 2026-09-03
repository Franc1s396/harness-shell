from __future__ import annotations

import json
import struct
from uuid import UUID

import pytest

from harness_shell_sidecar.runtime.desktop_control import (
    READY_FRAME_MAX_JSON_BYTES,
    decode_ready_payload,
    encode_ready_frame,
)


INSTANCE_ID = UUID("01234567-89ab-4def-8123-456789abcdef")


def test_ready_frame_is_length_prefixed_and_strict() -> None:
    frame = encode_ready_frame(instance_id=INSTANCE_ID, port=43_123)
    (payload_length,) = struct.unpack(">I", frame[:4])
    payload = frame[4:]

    assert payload_length == len(payload)
    assert payload_length <= READY_FRAME_MAX_JSON_BYTES
    assert json.loads(payload.decode("utf-8")) == {
        "instance_id": str(INSTANCE_ID),
        "port": 43_123,
        "version": 1,
    }
    assert decode_ready_payload(payload).instance_id == INSTANCE_ID

    with pytest.raises(ValueError, match="unknown field"):
        decode_ready_payload(
            b'{"version":1,"instance_id":"01234567-89ab-4def-8123-456789abcdef",'
            b'"port":43123,"extra":true}'
        )
    with pytest.raises(ValueError, match="UTF-8"):
        decode_ready_payload(b"\xff")
    with pytest.raises(ValueError, match="duplicate field"):
        decode_ready_payload(
            b'{"version":1,"version":1,'
            b'"instance_id":"01234567-89ab-4def-8123-456789abcdef",'
            b'"port":43123}'
        )
