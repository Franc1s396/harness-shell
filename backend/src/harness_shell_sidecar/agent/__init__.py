"""Experimental ReAct Agent backend public contracts and repositories."""

from .api_configs import ApiConfigRepository, ApiConfigRepositoryError
from .contracts import (
    AgentRun,
    AgentRunStatus,
    AgentTurnInput,
    AgentTurnResult,
    ApiType,
    CommandExecutionResult,
    CommandToolEnvelope,
    ExecuteCommandArguments,
    ModelApiConfig,
    ModelApiConfigInput,
)
from .conversations import ConversationRepository, ConversationRepositoryError
from .context import ContextService, SYSTEM_MESSAGE
from .executor import AgentCancelled, SshCommandExecutor
from .graph import (
    AgentGraphContext,
    AgentGraphDependencies,
    AgentGraphState,
    build_agent_graph,
)
from .handlers import AgentTurnApplication, AgentTurnRequest, register_agent_handlers
from .model_gateway import (
    MODEL_REQUEST_TIMEOUT_SECONDS,
    MODEL_RETRY_DELAYS_SECONDS,
    ModelGateway,
    ModelGatewayError,
)
from .tools import (
    CommandRejected,
    CommandSafetyReviewer,
    ExecuteCommandToolDefinition,
    build_execute_command_tool_definition,
    tool_message,
)
from .service import AgentService, AgentServiceError

__all__ = [
    "AgentRun",
    "AgentRunStatus",
    "AgentTurnInput",
    "AgentTurnApplication",
    "AgentTurnRequest",
    "AgentTurnResult",
    "ApiConfigRepository",
    "ApiConfigRepositoryError",
    "ApiType",
    "CommandExecutionResult",
    "CommandRejected",
    "CommandSafetyReviewer",
    "ExecuteCommandToolDefinition",
    "build_execute_command_tool_definition",
    "CommandToolEnvelope",
    "ConversationRepository",
    "ConversationRepositoryError",
    "ContextService",
    "ExecuteCommandArguments",
    "ModelApiConfig",
    "ModelApiConfigInput",
    "MODEL_REQUEST_TIMEOUT_SECONDS",
    "MODEL_RETRY_DELAYS_SECONDS",
    "AgentCancelled",
    "AgentGraphContext",
    "AgentGraphDependencies",
    "AgentGraphState",
    "AgentService",
    "AgentServiceError",
    "ModelGateway",
    "ModelGatewayError",
    "SshCommandExecutor",
    "SYSTEM_MESSAGE",
    "build_agent_graph",
    "tool_message",
    "register_agent_handlers",
]
