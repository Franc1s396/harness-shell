"""仅限内部 loopback 的 typed HTTP 应用边界。"""

from .app import create_app

__all__ = ["create_app"]
