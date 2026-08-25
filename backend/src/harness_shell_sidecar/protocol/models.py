"""Strict protocol v1 envelope models."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 8_192
MAX_PAYLOAD_BYTES = 1_048_576


class MessageType(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    ERROR = "error"
    CANCEL = "cancel"
    HEARTBEAT = "heartbeat"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SECRET = "secret"


class FrameEnvelope(BaseModel):
    """The only JSON envelope accepted on the private stdio transport."""

    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal[1]
    message_type: MessageType
    request_id: UUID
    task_id: UUID | None
    workflow_run_id: UUID | None
    sequence: Annotated[int, Field(gt=0)]
    timestamp: AwareDatetime
    sensitivity: Sensitivity
    payload: dict[str, JsonValue]
