"""验证打包 ASGI 运行时依赖保持精确锁定。"""

from importlib.metadata import version
from pathlib import Path


def test_http_runtime_dependencies_are_locked_for_packaging() -> None:
    """要求每个导入的 ASGI 包都出现在构建锁文件中。"""

    lock = Path("backend/build-requirements.lock").read_text(encoding="utf-8")
    for distribution in ("fastapi", "uvicorn", "websockets"):
        assert f"{distribution}=={version(distribution)}" in lock.splitlines()
