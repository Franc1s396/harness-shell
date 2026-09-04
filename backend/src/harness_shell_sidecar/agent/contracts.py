"""Strict contracts shared by the experimental ReAct Agent backend."""

from __future__ import annotations

from enum import StrEnum
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
)


_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class ApiType(StrEnum):
    """Select the one explicitly configured OpenAI-compatible API surface."""

    CHAT_COMPLETIONS = "CHAT_COMPLETIONS"
    RESPONSES = "RESPONSES"


class ModelApiConfigFields(BaseModel):
    """Validate Provider fields which do not contain stored credential identity."""

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
        Field(description="Normalized HTTP(S) base URL supplied to ChatOpenAI."),
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

    @field_validator("base_url")
    @classmethod
    def normalize_http_base_url(cls, value: str) -> str:
        """Require an HTTP(S) URL and persist its canonical Pydantic form."""

        return str(_HTTP_URL_ADAPTER.validate_python(value))


class ModelApiConfigInput(ModelApiConfigFields):
    """Represent the complete internal value written by the Provider repository."""

    api_key_credential_id: UUID = Field(
        description="Opaque Python credential reference; never API key plaintext."
    )


class AgentRunStatus(StrEnum):
    """Represent the only persisted lifecycle states for an Agent run."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LIMIT_REACHED = "LIMIT_REACHED"
    CANCELLED = "CANCELLED"


class ModelApiConfig(ModelApiConfigInput):
    """Represent one persisted API configuration without secret material."""

    api_config_id: UUID = Field(description="Stable configuration identity.")
    created_at: AwareDatetime = Field(description="UTC creation timestamp.")
    updated_at: AwareDatetime = Field(description="UTC last-update timestamp.")


class AgentRun(BaseModel):
    """Represent one immutable view of a persisted Agent run."""

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
    """Describe one user turn after Rust has frozen trusted opaque identities."""

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
    """Represent the bounded internal terminal projection of one Agent turn."""

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
    """Validate the only model-controlled argument accepted by the SSH tool."""

    model_config = ConfigDict(extra="forbid", strict=True)

    command: Annotated[
        str,
        StringConstraints(min_length=1, max_length=4096),
        Field(description="Complete raw shell command passed without normalization."),
    ]

    @field_validator("command")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        """Reject NUL without otherwise rewriting the model-supplied command."""

        if "\x00" in value:
            raise ValueError("command cannot contain NUL")
        return value


class CommandExecutionResult(BaseModel):
    """Represent a determined completion or the partial output of a timeout."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    command: str = Field(description="Original command dispatched to AsyncSSH.")
    exit_code: int | None = Field(description="Remote exit status when determined.")
    exit_signal: str | None = Field(description="Remote signal name when determined.")
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
    """Provide the stable versioned JSON contract returned to the model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = Field(
        default=1,
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
