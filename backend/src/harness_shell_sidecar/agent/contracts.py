"""实验性 ReAct Agent 后端共享的严格契约。"""

from __future__ import annotations

from enum import StrEnum
from math import floor
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class ApiType(StrEnum):
    """选择唯一显式配置的 OpenAI 兼容 API 类型。"""

    CHAT_COMPLETIONS = "CHAT_COMPLETIONS"
    RESPONSES = "RESPONSES"


class ModelApiConfigFields(BaseModel):
    """校验不含已保存凭据标识的 Provider 字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
        Field(description="User-facing label for this provider configuration."),
    ]
    api_type: ApiType = Field(
        description="Explicit API selection; automatic probing is forbidden."
    )
    base_url: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
        Field(description="Normalized HTTP(S) base URL supplied to AsyncOpenAI."),
    ]
    model: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
        Field(description="Provider model identifier passed without inference."),
    ]
    enabled: bool = Field(
        default=True,
        description="Whether new Agent runs may use this configuration.",
    )

    context_window_size: int = Field(default=128000, gt=0, le=9007199254740991,
        description="Maximum configured context window in tokens.")
    context_compaction_threshold_ratio: float = Field(default=0.75, gt=0, lt=1,
        allow_inf_nan=False, description="Fraction of the window triggering compaction.")
    max_output_tokens: int = Field(default=8192, gt=0, le=9007199254740991,
        description="Reserved and requested maximum output tokens per invocation.")

    @model_validator(mode="after")
    def validate_context_budget(self) -> ModelApiConfigFields:
        """拒绝未留输入空间或压缩阈值超出输入上限的预算。"""
        budget = self.context_window_size - self.max_output_tokens
        trigger = floor(self.context_window_size * self.context_compaction_threshold_ratio)
        if not 1 <= trigger <= budget:
            raise ValueError("compaction trigger must fit the input budget")
        return self

    @field_validator("base_url")
    @classmethod
    def normalize_http_base_url(cls, value: str) -> str:
        """要求 HTTP(S) URL，并持久化 Pydantic 标准化后的形式。"""

        return str(_HTTP_URL_ADAPTER.validate_python(value))


class ModelApiConfigInput(ModelApiConfigFields):
    """表示 Provider 仓库写入的完整内部值。"""

    api_key_credential_id: UUID = Field(
        description="Opaque Python credential reference; never API key plaintext."
    )


class AgentRunStatus(StrEnum):
    """表示 Agent Run 唯一允许持久化的生命周期状态。"""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LIMIT_REACHED = "LIMIT_REACHED"
    CANCELLED = "CANCELLED"


class ModelApiConfig(ModelApiConfigInput):
    """表示一条不含秘密材料的持久化 API 配置。"""

    api_config_id: UUID = Field(description="Stable configuration identity.")
    created_at: AwareDatetime = Field(description="UTC creation timestamp.")
    updated_at: AwareDatetime = Field(description="UTC last-update timestamp.")


class AgentRun(BaseModel):
    """表示已持久化 Agent Run 的不可变视图。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    agent_run_id: UUID = Field(description="Stable identity of this run.")
    conversation_id: UUID = Field(description="Conversation history owned by this run.")
    ssh_session_id: UUID = Field(description="Frozen live SSH Session identity.")
    api_config_id: UUID = Field(description="Frozen model configuration identity.")
    status: AgentRunStatus = Field(description="Current persisted run lifecycle state.")
    react_iteration: Annotated[
        int,
        Field(
            ge=0,
            le=128,
            strict=True,
            description="Completed Tool Call to ToolMessage loops.",
        ),
    ]
    error_code: str | None = Field(description="Stable terminal failure code, if any.")
    started_at: AwareDatetime = Field(description="UTC run start timestamp.")
    ended_at: AwareDatetime | None = Field(description="UTC terminal timestamp, if ended.")


class AgentTurnInput(BaseModel):
    """描述调用方冻结可信不透明标识后的单个用户轮次。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    conversation_id: UUID | None = Field(
        default=None,
        description="Existing conversation identity or None to create one.",
    )
    ssh_session_id: UUID = Field(description="Connected SSH Session frozen by Rust Core.")
    api_config_id: UUID = Field(description="Selected non-secret model configuration.")
    user_message: Annotated[
        str,
        StringConstraints(min_length=1, max_length=65536),
        Field(description="User text for this streamed Agent turn."),
    ]


class AgentTurnResult(BaseModel):
    """表示单个 Agent 轮次有界的内部终态投影。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    conversation_id: UUID = Field(description="Conversation receiving the turn.")
    agent_run_id: UUID = Field(description="Run which produced this result.")
    status: AgentRunStatus = Field(description="Terminal run status.")
    final_text: str | None = Field(description="Final model text when successfully available.")
    react_iteration: Annotated[
        int,
        Field(
            ge=0,
            le=128,
            strict=True,
            description="Completed Tool Call to ToolMessage loops.",
        ),
    ]
    error_code: str | None = Field(description="Stable terminal failure code, if any.")


class ExecuteCommandArguments(BaseModel):
    """校验 SSH 工具唯一接受的模型控制参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    command: Annotated[
        str,
        StringConstraints(min_length=1, max_length=4096),
        Field(description="Complete raw shell command passed without normalization."),
    ]

    @field_validator("command")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        """拒绝 NUL，其余部分不改写模型提供的命令。"""

        if "\x00" in value:
            raise ValueError("command cannot contain NUL")
        return value


class OutputTruncation(BaseModel):
    """精确描述一个输出流丢弃的内容量。"""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    truncated: bool = Field(description="Whether characters were omitted.")
    original_chars: int = Field(ge=0, description="Original Unicode code point count.")
    retained_chars: int = Field(ge=0, description="Retained prefix code point count.")
    omitted_chars: int = Field(ge=0, description="Discarded code point count.")

    @model_validator(mode="after")
    def validate_counts(self) -> OutputTruncation:
        """拒绝不一致的输出裁剪统计。"""
        if (self.original_chars != self.retained_chars + self.omitted_chars
                or self.truncated != (self.omitted_chars > 0)):
            raise ValueError("inconsistent output truncation counts")
        return self


class CommandExecutionResult(BaseModel):
    """表示已确定的完成结果，或超时前获得的部分输出。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    command: str = Field(description="Original command dispatched to AsyncSSH.")
    exit_code: int | None = Field(description="Remote exit status when determined.")
    exit_signal: str | None = Field(description="Remote signal name when determined.")
    stdout_truncation: OutputTruncation = Field(description="Stdout prefix provenance.")
    stderr_truncation: OutputTruncation = Field(description="Stderr prefix provenance.")
    stdout: str = Field(description="Strict UTF-8 standard output.")
    stderr: str = Field(description="Strict UTF-8 standard error.")
    timed_out: bool = Field(description="Whether the 30-second wait expired.")
    duration_ms: Annotated[
        int,
        Field(
            ge=0,
            strict=True,
            description="Local monotonic elapsed time in milliseconds.",
        ),
    ]


class CommandToolEnvelope(BaseModel):
    """定义返回给模型的稳定、带版本 JSON 契约。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[2] = Field(
        default=2,
        description="Tool result schema version.",
    )
    ok: bool = Field(
        description="Whether execution produced a determined result, not exit success."
    )
    code: str = Field(description="Stable machine-readable tool result code.")
    message: str = Field(description="Non-sensitive model-facing explanation.")
    result: CommandExecutionResult | None = Field(
        description="Structured command data when the result contract permits it."
    )
