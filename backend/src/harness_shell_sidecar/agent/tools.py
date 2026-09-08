"""严格命令 schema、固定安全审查与 ToolMessage 编码。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.messages import ToolMessage

from .contracts import CommandToolEnvelope, ExecuteCommandArguments, OutputTruncation


DANGEROUS_COMMAND_PATTERN = re.compile(
    r"(?:"
    r"\brm\s+-rf\s+/"
    r"|\bdd\s+if="
    r"|\bmkfs\."
    r"|:\(\)\s*\{"
    r"|>\s*/dev/sd"
    r"|\bchmod\s+-R\s+777\s+/"
    r")",
    re.IGNORECASE,
)


class CommandRejected(RuntimeError):
    """批准的直接危险命令正则命中时携带稳定错误码。"""

    def __init__(self, error_code: str, message: str) -> None:
        """保存公开错误码和原因，不嵌入原始命令。"""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # 图中工具节点使用的稳定错误码。
        self.safe_message = message  # 经过审查且不含命令文本的原因。


class CommandSafetyReviewer:
    """直接对原始命令文本应用唯一批准的正则表达式。"""

    def review(self, command: str) -> None:
        """直接命中时拒绝，不做 trim、规范化、解析或展开。"""

        if DANGEROUS_COMMAND_PATTERN.search(command) is not None:
            raise CommandRejected(
                "COMMAND_REJECTED_DANGEROUS_PATTERN",
                "the command matched the direct-danger safety policy",
            )


def tool_message(tool_call_id: str, envelope: CommandToolEnvelope) -> ToolMessage:
    """将规范 JSON 信封与原始模型工具调用 ID 配对。"""

    return ToolMessage(
        content=envelope.model_dump_json(),
        tool_call_id=tool_call_id,
    )


@dataclass(frozen=True, slots=True)
class ExecuteCommandToolDefinition:
    """描述唯一面向模型的工具，不选择 Provider 协议。"""

    name: str  # Agent 图接受的稳定函数名。
    description: str  # 经过审查的模型可见能力与超时说明。
    parameters: dict[str, object]  # Pydantic 生成的严格 JSON Schema。
    strict: bool  # Provider 支持时要求其执行 schema 约束。


def build_execute_command_tool_definition() -> ExecuteCommandToolDefinition:
    """为两种模型 API 构建规范 execute_command 定义。"""

    return ExecuteCommandToolDefinition(
        name="execute_command",
        description=("""
        Commands requiring approval pause for a human decision in the UI before
        execution. Rejection returns COMMAND_REJECTED_BY_USER without execution.
        Do not ask for a separate conversational execution confirmation when the
        task and scope are clear. Submission is not proof of execution; inspect
        the tool result. Submit one complete shell command on the SSH session bound to this
        agent run. The command can modify remote state and has a 30-second
        execution timeout. stdout/stderr contain only a bounded prefix;
        inspect truncation metadata and use explicit range or filtered commands
        to query further output. Subsequent queries can observe changed state.
        """
                     ),
        parameters=ExecuteCommandArguments.model_json_schema(),
        strict=True,
    )


def clip_output(text: str, limit: int) -> tuple[str, OutputTruncation]:
    """保留精确首部，并说明丢弃的 Unicode 码点数。"""
    if type(limit) is not int or limit <= 0:
        raise ValueError("output limit must be a positive integer")
    kept = text[:limit]
    return kept, OutputTruncation(truncated=len(text) > limit,
        original_chars=len(text), retained_chars=len(kept),
        omitted_chars=len(text) - len(kept))
