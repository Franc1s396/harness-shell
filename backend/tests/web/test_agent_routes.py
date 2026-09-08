from __future__ import annotations

from harness_shell_sidecar.agent.api_configs import ApiConfigRepository

from ..storage_support import RepositoryClient, sql

import base64
import asyncio
from collections.abc import Mapping
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from harness_shell_sidecar.agent.contracts import AgentRun, AgentRunStatus
from harness_shell_sidecar.agent.handlers import AgentTurnRequest
from harness_shell_sidecar.runtime.request_context import RequestContext
from harness_shell_sidecar.agent.streaming import AgentTurnEventSink
from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app


def request_headers() -> dict[str, str]:
    """创建合法请求关联头。"""

    return {"X-Request-ID": str(uuid4())}


def agent_stream_headers(request_id: str | None = None) -> dict[str, str]:
    """创建严格 Agent SSE 协商与关联请求头。"""

    return {
        "Accept": "text/event-stream",
        "X-Request-ID": request_id or str(uuid4()),
    }


def parse_sse_events(wire: str) -> list[dict[str, object]]:
    """解码 Backend 固定三行帧，用于路由断言。"""

    events: list[dict[str, object]] = []
    for frame in wire.split("\n\n"):
        if not frame:
            continue
        event_line, id_line, data_line = frame.split("\n")
        value = json.loads(data_line.removeprefix("data: "))
        assert event_line == f"event: {value['type']}"
        assert id_line == f"id: {value['sequence']}"
        events.append(value)
    return events


class SuccessfulTurnApplication:
    """为路由集成发布确定性成功生命周期。"""

    def __init__(self) -> None:
        """冻结所有发出事件共用的 Run 标识。"""

        now = datetime.now(UTC)
        self.run_snapshot = AgentRun(
            agent_run_id=uuid4(),
            conversation_id=uuid4(),
            ssh_session_id=uuid4(),
            api_config_id=uuid4(),
            status=AgentRunStatus.RUNNING,
            react_iteration=0,
            error_code=None,
            started_at=now,
            ended_at=None,
        )

    async def run(
        self,
        _context: RequestContext,
        _params: Mapping[str, object],
        sink,
    ) -> None:
        """发出 started、精确可见增量及持久化完成事件。"""

        await sink.started(self.run_snapshot)
        await sink.text_delta("hello")
        await sink.completed(
            self.run_snapshot.model_copy(
                update={
                    "status": AgentRunStatus.COMPLETED,
                    "ended_at": datetime.now(UTC),
                }
            )
        )


class FailedTurnApplication(SuccessfulTurnApplication):
    """发布确定性、已持久化的启动后失败。"""

    async def run(
        self,
        _context: RequestContext,
        _params: Mapping[str, object],
        sink,
    ) -> None:
        """发出 started，再发出匹配的失败终止事件。"""

        await sink.started(self.run_snapshot)
        await sink.text_delta("partial")
        await sink.failed(
            self.run_snapshot.model_copy(
                update={
                    "status": AgentRunStatus.FAILED,
                    "error_code": "MODEL_RESPONSE_INVALID",
                    "ended_at": datetime.now(UTC),
                }
            ),
            "provider stream completed without any message chunks",
        )


def config_input(**overrides: object) -> dict[str, object]:
    """构建 Provider 配置变更输入。"""

    value: dict[str, object] = {
        "display_name": "Test Provider",
        "api_type": "RESPONSES",
        "base_url": "https://example.invalid/v1/",
        "model": "test-model",
        "enabled": True,
    }
    value.update(overrides)
    return value


def encrypted_secret(client: TestClient, secret: str) -> dict[str, object]:
    """使用当前 Runtime 公钥加密 API Key。"""

    public_key = client.get(
        "/v1/runtime/credential-encryption-key",
        headers=request_headers(),
    ).json()
    aes_key = bytes(range(32))
    iv = bytes(range(12))
    aad = f"harness-shell-credential-v1\0{public_key['key_id']}".encode()
    ciphertext = AESGCM(aes_key).encrypt(iv, secret.encode(), aad)
    rsa_public_key = serialization.load_der_public_key(
        base64.b64decode(public_key["public_key_spki_b64"], validate=True)
    )
    wrapped_key = rsa_public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "version": 1,
        "key_id": public_key["key_id"],
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "iv_b64": base64.b64encode(iv).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def credential_record(database_path: Path, credential_id: str) -> dict[str, str] | None:
    """通过测试线程拥有的 SQLite 连接读取凭据。"""

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            """
            SELECT payload
            FROM runtime_records
            WHERE record_type = 'credential' AND record_id = ?
            """,
            (credential_id,),
        ).fetchone()
    return None if row is None else json.loads(bytes(row[0]).decode("utf-8"))


def credential_count(database_path: Path) -> int:
    """统计凭据行，不借用 ASGI 线程连接。"""

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            "SELECT COUNT(*) FROM runtime_records WHERE record_type = 'credential'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_agent_api_config_crud_routes_are_typed(autonomous_client) -> None:
    created = autonomous_client.post(
        "/v1/agent/api-configs",
        headers=request_headers(),
        json=config_input(
            api_key_envelope=encrypted_secret(autonomous_client, "first-api-key")
        ),
    )
    assert created.status_code == 201
    created_config = created.json()["config"]
    config_id = created_config["api_config_id"]
    first_credential_id = created_config["api_key_credential_id"]

    listed = autonomous_client.get("/v1/agent/api-configs", headers=request_headers())
    unchanged_secret = autonomous_client.patch(
        f"/v1/agent/api-configs/{config_id}",
        headers=request_headers(),
        json=config_input(display_name="Updated Provider"),
    )
    replacement = autonomous_client.patch(
        f"/v1/agent/api-configs/{config_id}",
        headers=request_headers(),
        json=config_input(
            display_name="Rekeyed Provider",
            api_key_envelope=encrypted_secret(autonomous_client, "second-api-key"),
        ),
    )
    second_credential_id = replacement.json()["config"]["api_key_credential_id"]
    database_path = (
        autonomous_client.app.state.runtime_owner.require_resources().database.path
    )

    assert listed.status_code == 200
    assert unchanged_secret.status_code == 200
    assert unchanged_secret.json()["config"]["api_key_credential_id"] == first_credential_id
    assert replacement.status_code == 200
    assert second_credential_id != first_credential_id
    assert credential_record(database_path, first_credential_id) is None
    assert credential_record(database_path, second_credential_id) == {
        "credential_id": second_credential_id,
        "kind": "api_key",
        "secret": "second-api-key",
    }

    deleted = autonomous_client.delete(
        f"/v1/agent/api-configs/{config_id}", headers=request_headers()
    )

    assert deleted.json()["deleted"] is True
    assert credential_record(database_path, second_credential_id) is None


def test_api_config_create_rolls_back_credential_when_metadata_write_fails(
    autonomous_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = autonomous_client.app.state.runtime_owner.require_resources()

    def fail_create(self, _value: object) -> None:
        """handler 创建凭据后模拟元数据失败。"""

        raise RuntimeError("simulated metadata failure")

    monkeypatch.setattr(ApiConfigRepository, "create", fail_create)

    with pytest.raises(RuntimeError, match="simulated metadata failure"):
        autonomous_client.post(
            "/v1/agent/api-configs",
            headers=request_headers(),
            json=config_input(
                api_key_envelope=encrypted_secret(
                    autonomous_client, "must-roll-back"
                )
            ),
        )

    assert credential_count(resources.database.path) == 0


def test_agent_turn_accepts_only_identities_and_missing_config_is_typed(
    autonomous_client,
) -> None:
    payload = {
        "conversation_id": None,
        "ssh_session_id": str(uuid4()),
        "api_config_id": str(uuid4()),
        "user_message": "do not run",
        "user_message_id": str(uuid4()),
        "retry": True,
    }
    assert AgentTurnRequest.model_validate_json(json.dumps(payload))
    parsed = AgentTurnRequest.model_validate_json(json.dumps(payload)).to_input()
    assert str(parsed.user_message_id) == payload["user_message_id"]
    assert parsed.retry is True

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json=payload,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "MODEL_API_CONFIG_NOT_FOUND"

    forbidden = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json={**payload, "api_key_b64": "forbidden"},
    )
    assert forbidden.status_code == 422


def test_agent_turn_requires_sse_accept(autonomous_client) -> None:
    """未协商 SSE 时在应用工作开始前拒绝轮次。"""

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=request_headers(),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )

    assert response.status_code == 406
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["error_code"] == "AGENT_STREAM_ACCEPT_REQUIRED"


@pytest.mark.parametrize("publish_started", [False, True])
def test_disconnect_cancels_turn_before_or_after_response_start(
    tmp_path: Path, publish_started: bool,
) -> None:
    """真实 ASGI disconnect 必须停止启动等待及流式 worker，而非依赖应用关闭。"""

    class BlockingTurnApplication(SuccessfulTurnApplication):
        """在指定响应阶段阻塞，观察 HTTP 断连是否收回 worker。"""

        def __init__(self) -> None:
            """拥有测试同步事件，不访问 Provider 或 SSH。"""
            super().__init__()
            self.entered = asyncio.Event()  # 测试等待 worker 到达断连目标阶段。
            self.cancelled = asyncio.Event()  # 测试观察取消已传入 worker。

        async def run(
            self, _context: RequestContext, _params: Mapping[str, object],
            sink: AgentTurnEventSink,
        ) -> None:
            """只发布指定阶段事件，并等待请求 owner 取消。"""
            try:
                if publish_started:
                    await sink.started(self.run_snapshot)
                self.entered.set()
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    async def scenario() -> None:
        """用 ASGI 消息驱动实际路由和中间件，不通过测试客户端缓冲 SSE。"""
        app = create_app(settings=RuntimeSettings.from_data_dir((tmp_path / "cancel-runtime").resolve()))
        async with app.router.lifespan_context(app):
            application = BlockingTurnApplication()
            resources = app.state.runtime_owner.require_resources()
            resources.agent_turn_application = application
            incoming: asyncio.Queue[Message] = asyncio.Queue()
            incoming.put_nowait({"type": "http.request", "body": json.dumps({
                "conversation_id": None, "ssh_session_id": str(uuid4()),
                "api_config_id": str(uuid4()), "user_message": "inspect",
            }).encode(), "more_body": False})
            response_started = asyncio.Event()

            async def send(message: Message) -> None:
                """仅记录响应开始，使首帧后的断连时机可确定。"""
                if message["type"] == "http.response.start":
                    response_started.set()

            scope: Scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/v1/agent/turns", "raw_path": b"/v1/agent/turns",
                "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json"),
                            (b"accept", b"text/event-stream"),
                            (b"x-request-id", str(uuid4()).encode())],
                "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
            }
            request_task = asyncio.create_task(app(scope, incoming.get, send))
            try:
                await asyncio.wait_for(application.entered.wait(), timeout=1)
                if publish_started:
                    await asyncio.wait_for(response_started.wait(), timeout=1)
                incoming.put_nowait({"type": "http.disconnect"})
                await asyncio.wait_for(application.cancelled.wait(), timeout=1)
                await asyncio.wait_for(request_task, timeout=1)
            finally:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)

    asyncio.run(scenario())


def test_agent_turn_success_is_started_first_sse(autonomous_client) -> None:
    """HTTP 成功时仅暴露固定关联事件序列。"""

    request_id = str(uuid4())
    resources = autonomous_client.app.state.runtime_owner.require_resources()
    application = SuccessfulTurnApplication()
    resources.agent_turn_application = application

    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(request_id),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )
    events = parse_sse_events(response.text)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == request_id
    assert [event["type"] for event in events] == [
        "agent.turn.started",
        "agent.turn.text_delta",
        "agent.turn.completed",
    ]
    assert [event["sequence"] for event in events] == [0, 1, 2]
    assert {event["request_id"] for event in events} == {request_id}
    assert {event["conversation_id"] for event in events} == {
        str(application.run_snapshot.conversation_id)
    }
    assert {event["agent_run_id"] for event in events} == {
        str(application.run_snapshot.agent_run_id)
    }


def test_agent_turn_post_start_failure_remains_sse(autonomous_client) -> None:
    """started 后保持 HTTP 200，并发出安全终止失败。"""

    resources = autonomous_client.app.state.runtime_owner.require_resources()
    application = FailedTurnApplication()
    resources.agent_turn_application = application
    response = autonomous_client.post(
        "/v1/agent/turns",
        headers=agent_stream_headers(),
        json={
            "conversation_id": None,
            "ssh_session_id": str(uuid4()),
            "api_config_id": str(uuid4()),
            "user_message": "inspect",
        },
    )
    events = parse_sse_events(response.text)

    assert response.status_code == 200
    assert [event["type"] for event in events] == [
        "agent.turn.started",
        "agent.turn.text_delta",
        "agent.turn.failed",
    ]
    assert events[-1]["status"] == "FAILED"
    assert events[-1]["error_code"] == "MODEL_RESPONSE_INVALID"
    assert events[-1]["message"] == (
        "provider stream completed without any message chunks"
    )
    assert "partial" not in str(events[-1])


def test_approval_decision_http_identity_and_idempotency(autonomous_client: TestClient) -> None:
    """真实路由和 handler 只接受原审核身份，不接受替换命令。"""
    from tests.agent.test_approvals import approval_request, decision_for
    resources = autonomous_client.app.state.runtime_owner.require_resources()
    service = resources.agent_service
    service._session_is_available = lambda _session_id: True
    request = approval_request()
    # 注册和决定都在 TestClient 的同一事件循环执行。
    async def register() -> None:
        service._approvals.register(request)
    autonomous_client.portal.call(register)
    path = f"/v1/agent/approvals/{request.approval_id}/decision"
    body = decision_for(request).model_dump(mode="json")
    headers = request_headers()
    wrong = autonomous_client.post(path, headers=headers, json={**body, "ssh_session_id": str(uuid4())})
    assert wrong.status_code == 409
    extra = autonomous_client.post(path, headers=headers, json={**body, "command": "pwd"})
    assert extra.status_code == 422
    for _ in range(2):
        response = autonomous_client.post(path, headers=headers, json=body)
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
        assert response.json() == {"approval_id": str(request.approval_id), "status": "APPROVED"}
    conflict = autonomous_client.post(path, headers=headers, json={**body, "decision": "reject"})
    assert conflict.status_code == 409
    async def release() -> None:
        service._approvals.release_run(request.agent_run_id)
    autonomous_client.portal.call(release)
    missing = autonomous_client.post(path, headers=headers, json=body)
    assert missing.status_code == 404
    assert request.arguments.command not in missing.text


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_resumes_original_asgi_stream(tmp_path: Path, decision: str) -> None:
    """真实 SSE 路由保持打开，另一个 HTTP 请求决定后继续同一流。"""
    import httpx
    from tests.agent.test_approvals import approval_request, decision_for

    async def scenario() -> None:
        app = create_app(settings=RuntimeSettings.from_data_dir((tmp_path / "approval-runtime").resolve()))
        async with app.router.lifespan_context(app):
            resources = app.state.runtime_owner.require_resources()
            service = resources.agent_service
            service._session_is_available = lambda _session_id: True
            application = SuccessfulTurnApplication()
            run = application.run_snapshot
            pending = approval_request().model_copy(update={
                "conversation_id": run.conversation_id, "agent_run_id": run.agent_run_id,
                "ssh_session_id": run.ssh_session_id,
            })

            async def run_approval(_context, _params, sink) -> None:
                """只由原 worker 发布恢复后的事件；HTTP handler 不发布。"""
                try:
                    await sink.started(run)
                    service._approvals.register(pending)
                    await sink.approval_requested(pending)
                    resolution = await service._approvals.wait(pending.approval_id)
                    await sink.approval_resolved(resolution, pending.tool_call_id)
                    if resolution.status == "APPROVED":
                        service._approvals.consume(pending)
                        await sink.tool_started(pending.tool_call_id, pending.arguments)
                    await sink.completed(run.model_copy(update={"status": AgentRunStatus.COMPLETED, "ended_at": datetime.now(UTC)}))
                finally:
                    service._approvals.release_run(run.agent_run_id)

            application.run = run_approval
            resources.agent_turn_application = application
            incoming: asyncio.Queue[Message] = asyncio.Queue()
            incoming.put_nowait({"type": "http.request", "body": json.dumps({
                "conversation_id": None, "ssh_session_id": str(run.ssh_session_id),
                "api_config_id": str(run.api_config_id), "user_message": "change",
            }).encode(), "more_body": False})
            chunks: list[bytes] = []
            requested = asyncio.Event()
            async def send(message: Message) -> None:
                if message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
                    if b"agent.turn.approval_requested" in chunks[-1]:
                        requested.set()
            scope: Scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/v1/agent/turns", "raw_path": b"/v1/agent/turns", "query_string": b"", "root_path": "",
                "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"), (b"x-request-id", str(uuid4()).encode())],
                "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8765),
            }
            task = asyncio.create_task(app(scope, incoming.get, send))
            try:
                await asyncio.wait_for(requested.wait(), 2)
                assert not task.done()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                    response = await client.post(f"/v1/agent/approvals/{pending.approval_id}/decision",
                        headers=request_headers(), json=decision_for(pending, decision).model_dump(mode="json"))
                    assert response.status_code == 200
                await asyncio.wait_for(task, 2)
                events = parse_sse_events(b"".join(chunks).decode())
                expected = ["agent.turn.started", "agent.turn.approval_requested", "agent.turn.approval_resolved"]
                if decision == "approve":
                    expected.append("agent.turn.tool_started")
                assert [event["type"] for event in events] == [*expected, "agent.turn.completed"]
                assert [event["sequence"] for event in events] == list(range(len(events)))
                assert {event["agent_run_id"] for event in events} == {str(run.agent_run_id)}
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
