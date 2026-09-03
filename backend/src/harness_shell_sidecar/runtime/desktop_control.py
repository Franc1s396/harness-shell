"""Strict inherited-pipe control protocol for the packaged desktop Runtime."""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from io import FileIO
from typing import BinaryIO
from uuid import UUID

import msvcrt


READY_FRAME_VERSION = 1
READY_FRAME_MAX_JSON_BYTES = 4_096
GRACEFUL_SHUTDOWN_BYTE = b"\x01"


class DesktopControlProtocolError(RuntimeError):
    """Report a strict ready/control pipe protocol violation."""


@dataclass(frozen=True, slots=True)
class DesktopReadyFrame:
    """Validated readiness data published exactly once to the Launcher."""

    #: Protocol version understood by both the Launcher and Sidecar.
    version: int
    #: Unique identity for this Sidecar process instance.
    instance_id: UUID
    #: Actual loopback port selected by the pre-bound listener.
    port: int


def _reject_duplicate_fields(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Build one JSON object while rejecting duplicate names explicitly."""

    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"ready payload contains duplicate field: {name}")
        result[name] = value
    return result


def decode_ready_payload(payload: bytes) -> DesktopReadyFrame:
    """Decode one bounded strict UTF-8 JSON readiness payload."""

    if not payload or len(payload) > READY_FRAME_MAX_JSON_BYTES:
        raise ValueError("ready payload length is outside the accepted bounds")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("ready payload must be strict UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_fields)
    except json.JSONDecodeError as error:
        raise ValueError("ready payload must be one JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("ready payload must be one JSON object")

    expected_fields = {"version", "instance_id", "port"}
    unknown_fields = set(value) - expected_fields
    if unknown_fields:
        raise ValueError(
            f"ready payload contains unknown field: {sorted(unknown_fields)[0]}"
        )
    missing_fields = expected_fields - set(value)
    if missing_fields:
        raise ValueError(
            f"ready payload is missing field: {sorted(missing_fields)[0]}"
        )

    version = value["version"]
    port = value["port"]
    instance_id = value["instance_id"]
    if type(version) is not int or version != READY_FRAME_VERSION:
        raise ValueError("ready payload version must be integer 1")
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("ready payload port must be an integer between 1 and 65535")
    if not isinstance(instance_id, str):
        raise ValueError("ready payload instance_id must be a UUID string")
    try:
        parsed_instance_id = UUID(instance_id)
    except ValueError as error:
        raise ValueError("ready payload instance_id must be a UUID string") from error
    return DesktopReadyFrame(
        version=version,
        instance_id=parsed_instance_id,
        port=port,
    )


def encode_ready_frame(*, instance_id: UUID, port: int) -> bytes:
    """Encode one canonical length-prefixed ready frame."""

    ready = DesktopReadyFrame(
        version=READY_FRAME_VERSION,
        instance_id=instance_id,
        port=port,
    )
    if not 1 <= ready.port <= 65_535:
        raise ValueError("ready port must be between 1 and 65535")
    payload = json.dumps(
        {
            "instance_id": str(ready.instance_id),
            "port": ready.port,
            "version": ready.version,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > READY_FRAME_MAX_JSON_BYTES:
        raise ValueError("ready payload exceeds the maximum encoded length")
    return struct.pack(">I", len(payload)) + payload


class DesktopControl:
    """Own inherited pipe handles after transferring each to one Python fd."""

    def __init__(
        self,
        control_read_handle: int,
        ready_write_handle: int,
    ) -> None:
        """Transfer each inherited Windows handle exactly once into Python."""

        self._control_reader: BinaryIO | None = None
        self._ready_writer: BinaryIO | None = None
        self._ready_published = False

        control_fd = msvcrt.open_osfhandle(
            control_read_handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            self._control_reader = FileIO(control_fd, mode="rb", closefd=True)
            ready_fd = msvcrt.open_osfhandle(
                ready_write_handle,
                os.O_WRONLY | os.O_BINARY,
            )
            self._ready_writer = FileIO(ready_fd, mode="wb", closefd=True)
        except BaseException:
            if self._control_reader is not None:
                self._control_reader.close()
            else:
                os.close(control_fd)
            raise

    def publish_ready(self, *, instance_id: UUID, port: int) -> None:
        """Write and flush exactly one ready frame, then close its pipe end."""

        writer = self._ready_writer
        if writer is None or writer.closed or self._ready_published:
            raise DesktopControlProtocolError(
                "desktop readiness may only be published once"
            )
        frame = encode_ready_frame(instance_id=instance_id, port=port)
        view = memoryview(frame)
        while view:
            written = writer.write(view)
            if written is None or written <= 0:
                raise OSError("ready pipe stopped accepting bytes")
            view = view[written:]
        writer.flush()
        self._ready_published = True
        writer.close()

    def wait_for_shutdown(self) -> None:
        """Block for graceful byte or EOF and reject every other control byte."""

        reader = self._control_reader
        if reader is None or reader.closed:
            raise DesktopControlProtocolError("desktop control pipe is closed")
        control = reader.read(1)
        if control in (b"", GRACEFUL_SHUTDOWN_BYTE):
            return
        raise DesktopControlProtocolError(
            f"desktop control pipe received invalid byte 0x{control.hex()}"
        )

    def close(self) -> None:
        """Close only the Python-owned descriptors, once each."""

        for stream in (self._ready_writer, self._control_reader):
            if stream is not None and not stream.closed:
                stream.close()


__all__ = [
    "DesktopControl",
    "DesktopControlProtocolError",
    "DesktopReadyFrame",
    "GRACEFUL_SHUTDOWN_BYTE",
    "READY_FRAME_MAX_JSON_BYTES",
    "decode_ready_payload",
    "encode_ready_frame",
]
