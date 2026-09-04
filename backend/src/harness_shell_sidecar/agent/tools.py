"""Strict command schema, fixed safety review, and ToolMessage encoding."""

from __future__ import annotations

import re

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from .contracts import CommandToolEnvelope, ExecuteCommandArguments


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
    """Carry a stable code when the approved direct-danger regex matches."""

    def __init__(self, error_code: str, message: str) -> None:
        """Store the public code and reason without embedding the raw command."""

        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code  # Stable code consumed by the graph tool node.
        self.safe_message = message  # Reviewed reason without command text.


class CommandSafetyReviewer:
    """Apply the single approved regex directly to the original command text."""

    def review(self, command: str) -> None:
        """Reject a direct match without trim, normalization, parsing, or expansion."""

        if DANGEROUS_COMMAND_PATTERN.search(command) is not None:
            raise CommandRejected(
                "COMMAND_REJECTED_DANGEROUS_PATTERN",
                "the command matched the direct-danger safety policy",
            )


def _schema_only_execute_command(command: str) -> str:
    """Expose the model schema; custom graph execution owns the real call."""

    raise RuntimeError("execute_command must run through the Agent tool node")


def build_execute_command_schema_tool() -> BaseTool:
    """Build the only strict tool schema bound to either supported model API."""

    return StructuredTool.from_function(
        func=_schema_only_execute_command,
        name="execute_command",
        description=(
            "Execute one complete shell command on the SSH session bound to this agent run. "
            "The command can modify remote state and has a 30-second execution timeout."
        ),
        args_schema=ExecuteCommandArguments,
    )


def tool_message(tool_call_id: str, envelope: CommandToolEnvelope) -> ToolMessage:
    """Pair a canonical JSON envelope with its original model tool call ID."""

    return ToolMessage(
        content=envelope.model_dump_json(),
        tool_call_id=tool_call_id,
    )
