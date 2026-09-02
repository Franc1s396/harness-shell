"""Verify that packaged ASGI runtime dependencies stay exactly locked."""

from importlib.metadata import version
from pathlib import Path


def test_http_runtime_dependencies_are_locked_for_packaging() -> None:
    """Require every imported ASGI package to appear in the build lock."""

    lock = Path("backend/build-requirements.lock").read_text(encoding="utf-8")
    for distribution in ("fastapi", "uvicorn", "websockets"):
        assert f"{distribution}=={version(distribution)}" in lock.splitlines()
