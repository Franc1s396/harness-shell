"""Validated dispatcher handlers for connection profile and Host Key storage."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from harness_shell_sidecar.protocol import FrameEnvelope
from harness_shell_sidecar.runtime.dispatcher import DispatchError, RequestDispatcher

from .models import ConnectionProfileInput, HostKeyCandidate
from .repository import ConnectionRepository, ConnectionRepositoryError


class _EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ConnectionIdParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connection_id: UUID


class _ConnectionUpdateParams(ConnectionProfileInput):
    connection_id: UUID


class _HostKeyReplaceParams(HostKeyCandidate):
    expected_old_fingerprint: str


def register_connection_handlers(
    dispatcher: RequestDispatcher,
    repository: ConnectionRepository,
) -> None:
    async def list_connections(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        _params(frame, _EmptyParams)
        _require_active(cancelled)
        return {
            "connections": [
                profile.model_dump(mode="json") for profile in repository.list()
            ]
        }

    async def create_connection(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        value = _params(frame, ConnectionProfileInput)
        _require_active(cancelled)
        return {
            "connection": repository.create(value).model_dump(mode="json")
        }

    async def get_connection(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _ConnectionIdParams)
        _require_active(cancelled)
        connection = repository.get(params.connection_id)
        if connection is None:
            raise DispatchError(
                "CONNECTION_NOT_FOUND", "connection profile was not found"
            )
        return {"connection": connection.model_dump(mode="json")}

    async def update_connection(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _ConnectionUpdateParams)
        _require_active(cancelled)
        value = ConnectionProfileInput.model_validate(
            params.model_dump(exclude={"connection_id"})
        )
        return {
            "connection": repository.update(
                params.connection_id, value
            ).model_dump(mode="json")
        }

    async def delete_connection(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _ConnectionIdParams)
        _require_active(cancelled)
        return {"deleted": repository.delete(params.connection_id)}

    async def confirm_host_key(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        candidate = _params(frame, HostKeyCandidate)
        _require_active(cancelled)
        return {
            "host_key": repository.trust_first_host_key(candidate).model_dump(
                mode="json"
            )
        }

    async def replace_host_key(
        frame: FrameEnvelope, cancelled: asyncio.Event
    ) -> dict:
        params = _params(frame, _HostKeyReplaceParams)
        _require_active(cancelled)
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


def _params(frame: FrameEnvelope, model: type[BaseModel]) -> Any:
    params = frame.payload.get("params")
    if not isinstance(params, dict):
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params must be an object"
        )
    try:
        return model.model_validate_json(json.dumps(params))
    except (TypeError, ValueError, ValidationError) as exc:
        raise DispatchError(
            "INVALID_REQUEST_PAYLOAD", "request params are invalid"
        ) from exc


def _require_active(cancelled: asyncio.Event) -> None:
    if cancelled.is_set():
        raise DispatchError("REQUEST_CANCELLED", "request was cancelled")


def _map_repository_errors(handler):
    async def wrapped(frame: FrameEnvelope, cancelled: asyncio.Event) -> dict:
        try:
            return await handler(frame, cancelled)
        except ConnectionRepositoryError as exc:
            raise DispatchError(exc.error_code, str(exc)) from exc

    return wrapped
