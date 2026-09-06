"""Agent 配置与使用秘密执行轮次的严格 dispatcher handler。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Annotated, Any, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
)

from harness_shell_sidecar.credentials import (
    CredentialCipherError,
    CredentialEnvelope,
    CredentialRepository,
    CredentialRepositoryError,
    RuntimeCredentialCipher,
    zeroize,
)
from harness_shell_sidecar.runtime.dispatcher import (
    DispatchError,
    Handler,
    RequestDispatcher,
)
from harness_shell_sidecar.runtime.request_context import RequestContext
from harness_shell_sidecar.storage import RuntimeDatabase

from .api_configs import ApiConfigRepository, ApiConfigRepositoryError
from .contracts import (
    AgentTurnInput,
    ModelApiConfig,
    ModelApiConfigFields,
    ModelApiConfigInput,
)
from .service import AgentServiceError
from .streaming import AgentTurnEventSink


_PUBLIC_REPOSITORY_ERRORS = {
    "MODEL_API_CONFIG_NOT_FOUND": (
        "MODEL_API_CONFIG_NOT_FOUND",
        "model API configuration was not found",
    ),
    "MODEL_API_CONFIG_IN_USE": (
        "MODEL_API_CONFIG_IN_USE",
        "model API configuration is in use",
    ),
    "MODEL_API_CONFIG_PERSISTENCE_FAILED": (
        "MODEL_API_CONFIG_PERSISTENCE_FAILED",
        "model API configuration persistence failed",
    ),
}
_UNKNOWN_REPOSITORY_ERROR = (
    "MODEL_API_CONFIG_PERSISTENCE_FAILED",
    "model API configuration persistence failed",
)
class _AgentServiceProtocol(Protocol):
    """描述 handler 所需的带秘密轮次服务接口。"""

    async def run_turn(
        self,
        request: AgentTurnInput,
        api_key: SecretStr,
        cancelled: asyncio.Event,
        *,
        expected_config: ModelApiConfig,
        event_sink: AgentTurnEventSink,
    ) -> object:
        """执行一个有界 Agent 轮次。"""


class _EmptyParams(BaseModel):
    """列表操作拒绝所有参数字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class _ApiConfigIdParams(BaseModel):
    """通过不透明标识定位模型 API 配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    api_config_id: UUID


class ModelApiConfigCreateRequest(ModelApiConfigFields):
    """携带 Provider 字段和使用当前密钥加密的 API Key。"""

    api_key_envelope: CredentialEnvelope = Field(
        description="API key encrypted for the current Runtime."
    )


class ModelApiConfigUpdateRequest(ModelApiConfigFields):
    """携带 Provider 字段及可选的替换 API Key。"""

    api_key_envelope: CredentialEnvelope | None = Field(
        default=None,
        description="Optional replacement API key encrypted for this Runtime.",
    )


class _ApiConfigUpdateParams(ModelApiConfigUpdateRequest):
    """组合 Provider 字段、标识及可选的替换 API Key。"""

    api_config_id: UUID = Field(description="Provider configuration to replace.")


class AgentTurnRequest(BaseModel):
    """校验 Agent 轮次仅包含标识的公共载荷。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: UUID | None = None
    ssh_session_id: UUID
    api_config_id: UUID
    user_message: Annotated[str, StringConstraints(min_length=1, max_length=65536)]

    def to_input(self) -> AgentTurnInput:
        """配置检查通过后构建非秘密 Agent 输入。"""

        return AgentTurnInput(
            conversation_id=self.conversation_id,
            ssh_session_id=self.ssh_session_id,
            api_config_id=self.api_config_id,
            user_message=self.user_message,
        )


class AgentTurnApplication:
    """解析冻结的 Provider 秘密并调用流式 Agent 服务。"""

    def __init__(
        self,
        api_configs: ApiConfigRepository,
        agent_service: _AgentServiceProtocol,
        credential_repository: CredentialRepository,
    ) -> None:
        """绑定非秘密配置、持久化服务和明文秘密的管理者。"""

        self._api_configs = api_configs  # 冻结 Provider 元数据的权威来源。
        self._agent_service = agent_service  # 持久化 Agent Run 的管理者。
        self._credential_repository = credential_repository  # 明文 API Key 的管理者。

    async def run(
        self,
        context: RequestContext,
        raw_params: Mapping[str, object],
        event_sink: AgentTurnEventSink,
    ) -> None:
        """解析、使用并清零密钥，不暴露 JSON 结果。"""

        # 1. 严格解析轮次输入并冻结当前启用的 Provider 配置。
        params = _params(raw_params, AgentTurnRequest)
        config = self._api_configs.get(params.api_config_id)
        if config is None:
            raise DispatchError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        if not config.enabled:
            raise DispatchError(
                "MODEL_API_CONFIG_DISABLED",
                "model API configuration is disabled",
            )
        context.require_active()

        try:
            # 2. 按模型 API Key 用途解析临时秘密，拒绝类型或记录不一致。
            decoded = self._credential_repository.resolve(
                config.api_key_credential_id,
                "api_key",
            )
        except CredentialRepositoryError as error:
            raise DispatchError(
                error.error_code,
                "Agent credential resolution failed",
            ) from None

        api_key_text = ""
        api_key: SecretStr | None = None
        try:
            # 3. 秘密解析后复核配置，再把短生命周期密钥交给轮次服务。
            if self._api_configs.get(params.api_config_id) != config:
                raise DispatchError(
                    "MODEL_API_CONFIG_CHANGED",
                    "model API configuration changed before turn dispatch",
                )
            try:
                api_key_text = decoded.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise DispatchError(
                    "INVALID_REQUEST_PAYLOAD",
                    "API key must be valid UTF-8",
                ) from error
            api_key = SecretStr(api_key_text)
            await self._agent_service.run_turn(
                params.to_input(),
                api_key,
                context.cancelled,
                expected_config=config,
                event_sink=event_sink,
            )
        except AgentServiceError as error:
            raise DispatchError(
                error.error_code,
                error.safe_message,
            ) from None
        except DispatchError:
            raise
        except Exception:
            raise DispatchError(
                "AGENT_TURN_FAILED",
                "Agent turn failed",
            ) from None
        # 4. 无论成功、失败还是取消，都清零解码缓冲区并释放秘密引用。
        finally:
            api_key = None
            api_key_text = ""
            zeroize(decoded)


def register_agent_handlers(
    dispatcher: RequestDispatcher,
    api_configs: ApiConfigRepository,
    agent_service: _AgentServiceProtocol,
    credential_repository: CredentialRepository,
    credential_cipher: RuntimeCredentialCipher,
    database: RuntimeDatabase,
) -> AgentTurnApplication:
    """注册聚合 Provider CRUD 与仅传标识的 Agent 轮次操作。"""

    turn_application = AgentTurnApplication(
        api_configs,
        agent_service,
        credential_repository,
    )

    async def list_configs(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """返回有序的非秘密模型配置元数据。"""

        _params(raw_params, _EmptyParams)
        context.require_active()
        return {
            "configs": [config.model_dump(mode="json") for config in api_configs.list()]
        }

    async def create_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """原子持久化 API Key 凭据及其 Provider 元数据。"""

        params = _params(raw_params, ModelApiConfigCreateRequest)
        context.require_active()
        with database.transaction():
            credential_id = _create_api_key(
                credential_cipher,
                credential_repository,
                params.api_key_envelope,
            )
            value = _api_config_input(params, credential_id)
            created = api_configs.create(value)
        return {"config": created.model_dump(mode="json")}

    async def update_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """原子替换 Provider 元数据及可选的 API Key。"""

        params = _params(raw_params, _ApiConfigUpdateParams)
        context.require_active()
        current = api_configs.get(params.api_config_id)
        if current is None:
            raise ApiConfigRepositoryError(
                "MODEL_API_CONFIG_NOT_FOUND",
                "model API configuration was not found",
            )
        with database.transaction():
            credential_id = current.api_key_credential_id
            if params.api_key_envelope is not None:
                credential_id = _create_api_key(
                    credential_cipher,
                    credential_repository,
                    params.api_key_envelope,
                )
            value = _api_config_input(params, credential_id)
            updated = api_configs.update(params.api_config_id, value)
            if credential_id != current.api_key_credential_id:
                _delete_owned_credential(
                    credential_repository, current.api_key_credential_id
                )
        return {"config": updated.model_dump(mode="json")}

    async def delete_config(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """原子删除 Provider 元数据及其拥有的 API Key 凭据。"""

        params = _params(raw_params, _ApiConfigIdParams)
        context.require_active()
        current = api_configs.get(params.api_config_id)
        if current is None:
            return {"deleted": False}
        with database.transaction():
            deleted = api_configs.delete(params.api_config_id)
            if not deleted:
                raise ApiConfigRepositoryError(
                    "MODEL_API_CONFIG_PERSISTENCE_FAILED",
                    "model API configuration changed during deletion",
                )
            _delete_owned_credential(
                credential_repository, current.api_key_credential_id
            )
        return {"deleted": True}

    handlers = {
        "agent.api_configs.list": list_configs,
        "agent.api_configs.create": create_config,
        "agent.api_configs.update": update_config,
        "agent.api_configs.delete": delete_config,
    }
    for method, handler in handlers.items():
        dispatcher.register(method, _map_repository_errors(handler))
    return turn_application


def _params(raw_params: Mapping[str, object], model: type[BaseModel]) -> Any:
    """严格校验 JSON 参数，同时允许标准 UUID JSON 字符串。"""

    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params must be an object",
        )
    try:
        return model.model_validate_json(json.dumps(dict(raw_params)))
    except (TypeError, ValueError, ValidationError) as error:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD",
            "request params are invalid",
        ) from error


def _map_repository_errors(handler: Handler) -> Handler:
    """只将稳定仓库失败转换为公开派发错误。"""

    async def wrapped(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        """调用 handler，并将仓库错误码与诊断信息收敛为安全错误。"""

        try:
            return await handler(context, raw_params)
        except ApiConfigRepositoryError as error:
            error_code, message = _PUBLIC_REPOSITORY_ERRORS.get(
                error.error_code,
                _UNKNOWN_REPOSITORY_ERROR,
            )
            raise DispatchError(error_code, message) from None
        except (CredentialCipherError, CredentialRepositoryError) as error:
            raise DispatchError(error.error_code, "credential mutation failed") from None

    return wrapped


def _create_api_key(
    cipher: RuntimeCredentialCipher,
    repository: CredentialRepository,
    envelope: CredentialEnvelope,
) -> UUID:
    """解密、持久化并及时清零 Provider API Key 缓冲区。"""

    secret = cipher.decrypt(envelope)
    text = ""
    try:
        try:
            text = secret.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CredentialRepositoryError(
                "CREDENTIAL_SECRET_INVALID",
                "the decrypted model API key is not valid UTF-8",
            ) from None
        return repository.create("api_key", text)
    finally:
        text = ""
        zeroize(secret)


def _api_config_input(
    params: ModelApiConfigCreateRequest | _ApiConfigUpdateParams,
    credential_id: UUID,
) -> ModelApiConfigInput:
    """聚合 API Key 已存在后构建仓库写入值。"""

    return ModelApiConfigInput(
        context_window_size=params.context_window_size,
        context_compaction_threshold_ratio=params.context_compaction_threshold_ratio,
        max_output_tokens=params.max_output_tokens,
        display_name=params.display_name,
        api_type=params.api_type,
        base_url=params.base_url,
        model=params.model,
        api_key_credential_id=credential_id,
        enabled=params.enabled,
    )


def _delete_owned_credential(
    repository: CredentialRepository,
    credential_id: UUID,
) -> None:
    """删除聚合拥有的凭据，或暴露持久化状态不一致。"""

    if not repository.delete(credential_id):
        raise CredentialRepositoryError(
            "CREDENTIAL_NOT_FOUND",
            "the model API configuration credential could not be deleted",
        )
