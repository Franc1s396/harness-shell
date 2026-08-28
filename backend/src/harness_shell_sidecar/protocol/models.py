"""Strict protocol v1 envelope models."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


PROTOCOL_VERSION = 1
MAX_HEADER_BYTES = 8_192
MAX_PAYLOAD_BYTES = 1_048_576


class MessageType(StrEnum):
    """协议帧在一次请求生命周期中的语义类型。"""

    #: 发起一项需要响应的操作。
    REQUEST = "request"
    #: 返回请求的成功结果。
    RESPONSE = "response"
    #: 发布不直接结束请求的异步状态或数据。
    EVENT = "event"
    #: 返回请求或协议处理失败信息。
    ERROR = "error"
    #: 请求取消仍在执行的目标操作。
    CANCEL = "cancel"
    #: 探测或维持 Sidecar 存活状态。
    HEARTBEAT = "heartbeat"


class Sensitivity(StrEnum):
    """标记协议帧是否包含必须受限处理的敏感内容。"""

    #: 可按普通业务数据处理的内容。
    NORMAL = "normal"
    #: 包含凭据等不得记录或持久化的秘密内容。
    SECRET = "secret"


class FrameEnvelope(BaseModel):
    """The only JSON envelope accepted on the private stdio transport."""

    #: 禁止额外字段和隐式类型转换，确保跨进程协议 fail closed。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 协议主版本；当前固定为 1。
    protocol_version: Literal[1]
    #: 当前帧的请求、响应、事件或控制语义。
    message_type: MessageType
    #: 将响应、错误和取消帧关联到原始请求的标识符。
    request_id: UUID
    #: 可选的上层任务标识符。
    task_id: UUID | None
    #: 可选的工作流运行标识符。
    workflow_run_id: UUID | None
    #: 单调递增的传输序号，用于检测乱序或重放。
    sequence: Annotated[int, Field(gt=0)]
    #: 帧创建时刻，必须包含时区。
    timestamp: AwareDatetime
    #: 内容敏感级别，决定日志与持久化边界。
    sensitivity: Sensitivity
    #: 与 message_type 对应的 JSON 业务负载。
    payload: dict[str, JsonValue]
