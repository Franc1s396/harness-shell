from __future__ import annotations

from importlib import import_module
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from harness_ssh_sidecar.web import create_app


def runtime_settings(data_dir: Path):
    """创建目标配置，缺失实现时仍明确测试失败。"""

    try:
        settings_type = import_module(
            "harness_ssh_sidecar.runtime.settings"
        ).RuntimeSettings
    except (ModuleNotFoundError, AttributeError) as exc:
        raise AssertionError("autonomous Runtime settings are not implemented") from exc
    return settings_type.from_data_dir(data_dir.resolve())


def test_lifespan_initializes_plaintext_resources_before_requests(
    tmp_path: Path,
) -> None:
    request_id = uuid4()
    app = create_app(settings=runtime_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/v1/health/ready",
            headers={"X-Request-ID": str(request_id)},
        )

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == str(request_id)
        assert response.json() == {
            "request_id": str(request_id),
            "ready": True,
            "state": "READY",
        }


def test_restart_purges_unbound_images_only(tmp_path: Path) -> None:
    """使用真实 Runtime 启停，绑定图在重启后仍可读取。"""
    from harness_ssh_sidecar.storage import RuntimeDatabase
    from harness_ssh_sidecar.agent.attachments import AttachmentRepository
    from harness_ssh_sidecar.agent.conversations import ConversationRepository
    from harness_ssh_sidecar.storage import PlaintextRecordStore
    from ..agent.test_image_validation import picture
    from harness_ssh_sidecar.agent.image_validation import validate_image
    settings = runtime_settings(tmp_path)
    with TestClient(create_app(settings=settings)):
        pass
    database = RuntimeDatabase.open(tmp_path / 'runtime.sqlite3')
    try:
        with database.write_session() as session:
            conversation = ConversationRepository(session, PlaintextRecordStore(session)).create_conversation()
            repo = AttachmentRepository(session)
            draft = uuid4()
            bound = repo.create(draft, validate_image('bound', picture('PNG')))
            unbound = repo.create(draft, validate_image('unbound', picture('PNG')))
            repo.bind(draft, conversation, uuid4(), (bound.attachment_id,))
    finally:
        database.close()
    with TestClient(create_app(settings=settings)) as client:
        for item, status in [(bound, 200), (unbound, 404)]:
            assert client.get(f'/v1/agent/attachments/{item.attachment_id}/content', headers={'X-Request-ID': str(uuid4())}).status_code == status
