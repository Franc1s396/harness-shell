"""验证 Python 诊断目录的 HTTP 边界。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from harness_shell_sidecar.runtime.settings import RuntimeSettings
from harness_shell_sidecar.web import create_app


def request_headers() -> dict[str, str]:
    """创建合法请求关联头。"""

    return {"X-Request-ID": str(uuid4())}


def test_log_directory_reports_only_availability(tmp_path: Path) -> None:
    """不向 React 暴露 Python 拥有的日志绝对路径。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/v1/diagnostics/log-directory",
            headers=request_headers(),
        )

    assert response.status_code == 200
    assert set(response.json()) == {"request_id", "available"}
    assert response.json()["available"] is True
    assert str(settings.log_dir) not in response.text


def test_open_log_directory_uses_fixed_runtime_path(tmp_path: Path) -> None:
    """只有配置派生目录能够传到操作系统打开入口。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)
    opened: list[Path] = []
    with TestClient(
        create_app(settings=settings, log_directory_opener=opened.append)
    ) as client:
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 204
    assert opened == [settings.log_dir]


def test_open_log_directory_rejects_missing_fixed_path(tmp_path: Path) -> None:
    """固定目录缺失属于显式诊断失败。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        # 自主启动会创建固定目录；启动后删除空目录，
        # 用来模拟真实的外部删除失败。
        settings.log_dir.rmdir()
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "LOG_DIRECTORY_UNAVAILABLE"


def test_open_log_directory_reports_os_start_failure(tmp_path: Path) -> None:
    """保持 Explorer 启动错误稳定，不暴露绝对路径。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    settings.log_dir.mkdir(parents=True)

    def fail_open(_path: Path) -> None:
        """模拟有界操作系统进程启动失败。"""

        raise OSError("test marker must not cross the HTTP boundary")

    with TestClient(
        create_app(settings=settings, log_directory_opener=fail_open)
    ) as client:
        response = client.post(
            "/v1/diagnostics/log-directory/open",
            headers=request_headers(),
        )

    assert response.status_code == 500
    assert response.json()["error_code"] == "LOG_DIRECTORY_OPEN_FAILED"
    assert "test marker" not in response.text


def test_cors_allows_only_the_two_fixed_react_origins(tmp_path: Path) -> None:
    """拒绝任意 origin，同时暴露直接客户端所需请求头。"""

    settings = RuntimeSettings.from_data_dir((tmp_path / "runtime").resolve())
    with TestClient(create_app(settings=settings)) as client:
        allowed = client.options(
            "/v1/diagnostics/log-directory",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Request-ID",
            },
        )
        rejected = client.options(
            "/v1/diagnostics/log-directory",
            headers={
                "Origin": "http://192.168.1.4:8765",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Request-ID",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
