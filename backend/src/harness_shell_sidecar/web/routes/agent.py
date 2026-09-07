"""Provider 配置和流式 Agent 轮次的 typed HTTP 路由。"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import StreamingResponse

from harness_shell_sidecar.agent.contracts import ModelApiConfig
from harness_shell_sidecar.agent.handlers import (
    AgentTurnRequest,
    ModelApiConfigCreateRequest,
    ModelApiConfigUpdateRequest,
)
from harness_shell_sidecar.runtime.dispatcher import DispatchError

from ..agent_stream import AgentTurnStreamSession
from ..dependencies import (
    dispatch_application,
    dispatch_error_problem,
    model_from_result,
    require_ready_resources,
    require_request_id,
    runtime_owner,
    set_correlation,
    validate_json_model,
)
from ..errors import HttpProblem, build_problem
from ..lifespan import RuntimeOwner
from ..limits import ResponseLimitRoute
from ..models import (
    AgentApiConfigListResponse,
    AgentApiConfigResponse,
    DeleteResponse,
)


router = APIRouter(route_class=ResponseLimitRoute)
CorrelationId = Annotated[UUID, Depends(require_request_id)]
Owner = Annotated[RuntimeOwner, Depends(runtime_owner)]


@router.get("/v1/agent/api-configs", response_model=AgentApiConfigListResponse)
async def list_api_configs(
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigListResponse:
    """返回全部非秘密 Provider 配置。"""

    result = await dispatch_application(
        owner, request_id, "agent.api_configs.list", {}
    )
    configs = [
        model_from_result(value, ModelApiConfig) for value in result["configs"]
    ]
    set_correlation(response, request_id)
    return AgentApiConfigListResponse(request_id=request_id, configs=configs)


@router.post(
    "/v1/agent/api-configs",
    response_model=AgentApiConfigResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_config(
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigResponse:
    """持久化 Provider 配置及其加密传输的 API Key。"""

    value = validate_json_model(payload, ModelApiConfigCreateRequest, request_id)
    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.create",
        value.model_dump(mode="json"),
    )
    set_correlation(response, request_id)
    return AgentApiConfigResponse(
        request_id=request_id,
        config=model_from_result(result["config"], ModelApiConfig),
    )


@router.patch(
    "/v1/agent/api-configs/{api_config_id}",
    response_model=AgentApiConfigResponse,
)
async def update_api_config(
    api_config_id: UUID,
    payload: dict[str, object],
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> AgentApiConfigResponse:
    """替换完整非秘密 Provider 配置。"""

    value = validate_json_model(payload, ModelApiConfigUpdateRequest, request_id)
    params = value.model_dump(mode="json")
    params["api_config_id"] = str(api_config_id)
    result = await dispatch_application(
        owner, request_id, "agent.api_configs.update", params
    )
    set_correlation(response, request_id)
    return AgentApiConfigResponse(
        request_id=request_id,
        config=model_from_result(result["config"], ModelApiConfig),
    )


@router.delete(
    "/v1/agent/api-configs/{api_config_id}",
    response_model=DeleteResponse,
)
async def delete_api_config(
    api_config_id: UUID,
    response: Response,
    request_id: CorrelationId,
    owner: Owner,
) -> DeleteResponse:
    """原子删除 Provider 元数据及其拥有的凭据。"""

    result = await dispatch_application(
        owner,
        request_id,
        "agent.api_configs.delete",
        {"api_config_id": str(api_config_id)},
    )
    set_correlation(response, request_id)
    return DeleteResponse(request_id=request_id, deleted=bool(result["deleted"]))


@router.post("/v1/agent/turns", response_model=None)
async def run_agent_turn(
    payload: dict[str, object],
    request: Request,
    request_id: CorrelationId,
    owner: Owner,
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """持久化 Run 已存在后才启动 Agent SSE 响应。"""

    # 1. 先要求客户端明确协商 SSE，协商失败仍返回普通 Problem。
    if accept is None or accept.strip().lower() != "text/event-stream":
        raise HttpProblem(
            build_problem(
                request_id=request_id,
                status=406,
                error_code="AGENT_STREAM_ACCEPT_REQUIRED",
                title="Agent stream accept required",
                message="Accept must be text/event-stream",
            )
        )
    # 2. 校验请求和就绪资源，创建绑定共享 dispatcher 的流会话。
    value = validate_json_model(payload, AgentTurnRequest, request_id)
    resources = require_ready_resources(owner, request_id)
    session = AgentTurnStreamSession(
        request_id=request_id,
        dispatcher=resources.dispatcher,
        application=resources.agent_turn_application,
        params=value.model_dump(mode="json"),
    )
    try:
        # 3. 等待 started 启动屏障，持久化启动前失败映射为 HTTP 错误。
        await _start_while_connected(session, request)
    except DispatchError as error:
        raise dispatch_error_problem(request_id, error) from None
    # 4. 启动成功后返回 SSE 正文，后续终态由同一流发布。
    return StreamingResponse(
        session.body(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "X-Request-ID": str(request_id),
            "Cache-Control": "no-store",
        },
    )


async def _start_while_connected(
    session: AgentTurnStreamSession, request: Request,
) -> None:
    """启动屏障期间拥有断连监听，交给 StreamingResponse 前收回监听任务。"""

    async def wait_disconnected() -> None:
        """请求体已由 FastAPI 消费，只等待 ASGI 的真实断连通知。"""
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    # 1. HTTP 200 尚未返回，StreamingResponse 此时还不能监听断连。
    start_task = asyncio.create_task(session.start())
    disconnect_task = asyncio.create_task(wait_disconnected())
    try:
        done, _ = await asyncio.wait(
            (start_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED,
        )
        if start_task in done:
            await start_task  # 已发生的启动错误优先传播，不被取消覆盖。
        if disconnect_task in done:
            await disconnect_task
            raise DispatchError(
                "AGENT_CANCELLED", "the client disconnected before the Agent stream started",
            )
    except BaseException:
        # 2. 取消并等待 start，再关闭可能已经跨过屏障的 worker。
        # session.start 自身也拥有取消清理；aclose 的幂等性覆盖启动竞态。
        start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)
        await session.aclose()
        raise
    finally:
        # 3. 只有一个 receive owner；正常启动后交回 StreamingResponse。
        disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)
