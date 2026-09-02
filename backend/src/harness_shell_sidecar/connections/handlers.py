"""Validated dispatcher handlers for connection profile and Host Key storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher
from harness_shell_sidecar.runtime.request_context import RequestContext

from .models import ConnectionProfileInput, HostKeyCandidate
from .repository import ConnectionRepository, ConnectionRepositoryError


class _EmptyParams(BaseModel):
    """表示不接受任何业务参数的严格请求体。"""

    #: 拒绝调用方传入任何未声明参数。
    model_config = ConfigDict(extra="forbid", strict=True)


class _ConnectionIdParams(BaseModel):
    """仅按连接标识符定位连接的请求参数。"""

    #: 对 IPC 请求参数执行严格结构校验。
    model_config = ConfigDict(extra="forbid", strict=True)

    #: 目标连接配置的唯一标识符。
    connection_id: UUID


class _ConnectionUpdateParams(ConnectionProfileInput):
    """连接更新请求中“目标标识符 + 完整新配置”的组合。"""

    #: 要被更新的连接配置标识符。
    connection_id: UUID


class _HostKeyReplaceParams(HostKeyCandidate):
    """替换 Host Key 时携带的候选值与并发保护条件。"""

    #: 调用方预期当前仍生效的旧指纹，用于避免覆盖并发变化。
    expected_old_fingerprint: str


def register_connection_handlers(
    dispatcher: RequestDispatcher,
    repository: ConnectionRepository,
) -> None:
    async def list_connections(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        _params(raw_params, _EmptyParams)
        context.require_active()
        return {
            "connections": [
                profile.model_dump(mode="json") for profile in repository.list()
            ]
        }

    async def create_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        value = _params(raw_params, ConnectionProfileInput)
        context.require_active()
        return {
            "connection": repository.create(value).model_dump(mode="json")
        }

    async def get_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ConnectionIdParams)
        context.require_active()
        connection = repository.get(params.connection_id)
        if connection is None:
            raise DispatchError(
                "CONNECTION_NOT_FOUND", "connection profile was not found"
            )
        return {"connection": connection.model_dump(mode="json")}

    async def update_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ConnectionUpdateParams)
        context.require_active()
        value = ConnectionProfileInput.model_validate(
            params.model_dump(exclude={"connection_id"})
        )
        return {
            "connection": repository.update(
                params.connection_id, value
            ).model_dump(mode="json")
        }

    async def delete_connection(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _ConnectionIdParams)
        context.require_active()
        return {"deleted": repository.delete(params.connection_id)}

    async def confirm_host_key(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        candidate = _params(raw_params, HostKeyCandidate)
        context.require_active()
        return {
            "host_key": repository.trust_first_host_key(candidate).model_dump(
                mode="json"
            )
        }

    async def replace_host_key(
        context: RequestContext, raw_params: Mapping[str, object]
    ) -> dict[str, object]:
        params = _params(raw_params, _HostKeyReplaceParams)
        context.require_active()
        candidate = HostKeyCandidate.model_validate(
            params.model_dump(exclude={"expected_old_fingerprint"})
        )
        return {
            "host_key": repository.replace_host_key(
                candidate, params.expected_old_fingerprint
            ).model_dump(mode="json")
        }

    handlers = {
        "connections.list": list_connections,
        "connections.get": get_connection,
        "connections.create": create_connection,
        "connections.update": update_connection,
        "connections.delete": delete_connection,
        "host_key.confirm": confirm_host_key,
        "host_key.replace": replace_host_key,
    }
    for method, handler in handlers.items():
        dispatcher.register(method, _map_repository_errors(handler))


def _params(raw_params: Mapping[str, object], model: type[BaseModel]) -> Any:
    if not isinstance(raw_params, Mapping):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        # JSON-mode validation preserves strict UUID parsing from JSON strings.
        return model.model_validate_json(json.dumps(dict(raw_params)))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _map_repository_errors(handler):
    async def wrapped(
        context: RequestContext,
        raw_params: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            return await handler(context, raw_params)
        except ConnectionRepositoryError as exc:
            raise DispatchError(exc.error_code, str(exc)) from exc

    return wrapped
