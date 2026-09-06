"""独立于传输的运行时生命周期模型。"""

from __future__ import annotations

import re
from enum import StrEnum

# HTTP 请求、响应和 Agent 结果预检共用同一编码 JSON 边界，
# 统一数值可避免传输层之间产生偏差。
MAX_JSON_BODY_BYTES = 1_048_576
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class RuntimeInitializationFailure(RuntimeError):
    """暴露经过校验的非秘密初始化失败。"""

    def __init__(self, error_code: str, public_message: str) -> None:
        """仅保留稳定错误码和显式公开消息。"""

        if _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("runtime error code must use uppercase identifiers")
        super().__init__(public_message)
        self.error_code = error_code  # 面向 HTTP 的稳定标识。
        self.public_message = public_message  # 安全有界的失败文本。
        self.safe_message = public_message  # 内部诊断别名。


class RuntimePhase(StrEnum):
    """描述从启动到清理的唯一运行时管理者。"""

    #: 正在原子打开并验证运行时资源。
    INITIALIZING = "INITIALIZING"
    #: 全部资源有效，允许派发应用操作。
    READY = "READY"
    #: 拒绝新请求，同时取消 dispatcher 活动工作。
    DRAINING = "DRAINING"
    #: 领域管理者正在收敛并关闭远程资源。
    CONVERGING = "CONVERGING"
    #: 正在关闭可观测性资源、密钥和本地持久化资源。
    CLOSING = "CLOSING"
    #: 完整运行时资源图已成功释放。
    STOPPED = "STOPPED"
    #: 初始化或收敛失败，运行时不可复用。
    FAILED = "FAILED"
