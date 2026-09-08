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
    """精确拒绝批准的直接危险命令示例。"""

    with pytest.raises(CommandRejected) as error:
        CommandSafetyReviewer().review(command)

    assert error.value.error_code == "COMMAND_REJECTED_DANGEROUS_PATTERN"


@pytest.mark.parametrize(
    "command",
    ["ls -la", "docker --version", "docker ps", "pwd", "uname -a"],
)
def test_ordinary_examples_pass_the_regex(command: str) -> None:
    """允许未命中批准正则的普通示例。"""

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
    """拒绝隐式强制转换、NUL、非法长度和未知选项。"""

    with pytest.raises(ValidationError):
        ExecuteCommandArguments.model_validate(value)


def test_execute_command_arguments_preserve_original_text() -> None:
    """命令进入安全正则前保留空白和原始拼写。"""

    command = "  printf 'MiXeD'  "
    validated = ExecuteCommandArguments(command=command)

    assert validated.command == command


def test_tool_message_uses_versioned_json_and_original_call_id() -> None:
    """将不可变信封编码为 JSON，并与模型工具调用配对。"""

    envelope = CommandToolEnvelope(
        ok=True,
        code="COMMAND_COMPLETED",
        message="Remote command finished.",
        result=CommandExecutionResult(
                stdout_truncation=dict(truncated=False, original_chars=0, retained_chars=0, omitted_chars=0),
                stderr_truncation=dict(truncated=False, original_chars=6, retained_chars=6, omitted_chars=0),
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
    """暴露已审查 schema，不绑定 LangChain 或 API 结构。"""

    definition = build_execute_command_tool_definition()

    assert definition.name == "execute_command"
    assert definition.strict is True
    assert definition.parameters == {
        "additionalProperties": False,
        "description": "校验 SSH 工具唯一接受的模型控制参数。",
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


def test_clip_output_preserves_unicode_prefix_and_metadata() -> None:
    from harness_shell_sidecar.agent.tools import clip_output
    prefix, meta = clip_output("中🙂" * 3001, 6000)
    assert prefix == "中🙂" * 3000
    assert meta.model_dump() == {"truncated": True, "original_chars": 6002,
                                "retained_chars": 6000, "omitted_chars": 2}


@pytest.mark.parametrize("length", [0, 5999, 6000, 6001])
def test_clip_output_boundary(length: int) -> None:
    from harness_shell_sidecar.agent.tools import clip_output
    text, meta = clip_output("x" * length, 6000)
    assert len(text) == min(length, 6000)
    assert meta.truncated == (length > 6000)
    assert meta.omitted_chars == max(length - 6000, 0)


def test_tool_and_system_prompt_explain_ui_approval_without_extra_confirmation() -> None:
    from harness_shell_sidecar.agent.context import DEFAULT_SYSTEM_PROMPT
    definition = build_execute_command_tool_definition()
    assert "COMMAND_REJECTED_BY_USER" in definition.description
    assert "UI" in definition.description
    assert "审核气泡" in DEFAULT_SYSTEM_PROMPT
    assert "不要仅为获取执行授权" in DEFAULT_SYSTEM_PROMPT
    assert set(definition.parameters["properties"]) == {"command"}
