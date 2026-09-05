from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from harness_shell_sidecar.agent.contracts import (
    CommandExecutionResult,
    CommandToolEnvelope,
    ExecuteCommandArguments,
)
from harness_shell_sidecar.agent.tools import (
    CommandRejected,
    CommandSafetyReviewer,
    build_execute_command_tool_definition,
    tool_message,
)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda",
        ":(){ :|:& };:",
        "echo test > /dev/sda",
        "chmod -R 777 /",
    ],
)
def test_direct_danger_patterns_are_rejected(command: str) -> None:
    """Reject exactly the approved direct dangerous command examples."""

    with pytest.raises(CommandRejected) as error:
        CommandSafetyReviewer().review(command)

    assert error.value.error_code == "COMMAND_REJECTED_DANGEROUS_PATTERN"


@pytest.mark.parametrize(
    "command",
    ["ls -la", "docker --version", "docker ps", "pwd", "uname -a"],
)
def test_ordinary_examples_pass_the_regex(command: str) -> None:
    """Allow ordinary examples which do not match the approved regex."""

    CommandSafetyReviewer().review(command)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"command": 7},
        {"command": ""},
        {"command": "x" * 4097},
        {"command": "printf 'a\x00b'"},
        {"command": "pwd", "timeout": 1},
    ],
)
def test_execute_command_arguments_are_strict(value: dict[str, object]) -> None:
    """Reject implicit coercion, NUL, invalid lengths, and unknown options."""

    with pytest.raises(ValidationError):
        ExecuteCommandArguments.model_validate(value)


def test_execute_command_arguments_preserve_original_text() -> None:
    """Preserve whitespace and spelling before the safety regex sees a command."""

    command = "  printf 'MiXeD'  "
    validated = ExecuteCommandArguments(command=command)

    assert validated.command == command


def test_tool_message_uses_versioned_json_and_original_call_id() -> None:
    """Encode one immutable envelope as JSON paired to the model tool call."""

    envelope = CommandToolEnvelope(
        ok=True,
        code="COMMAND_COMPLETED",
        message="Remote command finished.",
        result=CommandExecutionResult(
            command="false",
            exit_code=1,
            exit_signal=None,
            stdout="",
            stderr="failed",
            timed_out=False,
            duration_ms=12,
        ),
    )

    message = tool_message("call-7", envelope)

    assert isinstance(message, ToolMessage)
    assert message.tool_call_id == "call-7"
    assert json.loads(message.content) == envelope.model_dump(mode="json")


def test_execute_command_tool_definition_is_provider_neutral_and_strict() -> None:
    """Expose one reviewed schema without binding it to LangChain or an API shape."""

    definition = build_execute_command_tool_definition()

    assert definition.name == "execute_command"
    assert definition.strict is True
    assert definition.parameters == {
        "additionalProperties": False,
        "description": "Validate the only model-controlled argument accepted by the SSH tool.",
        "properties": {
            "command": {
                "description": "Complete raw shell command passed without normalization.",
                "maxLength": 4096,
                "minLength": 1,
                "title": "Command",
                "type": "string",
            }
        },
        "required": ["command"],
        "title": "ExecuteCommandArguments",
        "type": "object",
    }
