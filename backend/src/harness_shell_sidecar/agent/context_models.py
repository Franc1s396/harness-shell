"""共享上下文策略与显式失败契约。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID
from langchain_core.messages import AnyMessage


class ContextError(RuntimeError):
    """只向调用方暴露经过审查的上下文失败原因。"""
    def __init__(self, error_code: str, safe_message: str) -> None:
        """绑定稳定错误码，不保留原始历史。"""
        super().__init__(safe_message)
        self.error_code = error_code  # 对外公开的上下文失败类别。
        self.safe_message = safe_message  # 经过审查的用户可见原因。


@dataclass(frozen=True, slots=True)
class AgentContextPolicy:
    """保存仅由后端使用的显式上下文配置。"""
    tool_stdout_max_chars: int = 6000  # 每个输出流保留的 Unicode 码点数。
    tool_stderr_max_chars: int = 6000  # stderr 独立的首部保留上限。
    tokenizer_encoding: str = "o200k_base"  # 随包提供的估算编码标识。

    def __post_init__(self) -> None:
        """在处理远程输出前拒绝非法限制值。"""
        for limit in (self.tool_stdout_max_chars, self.tool_stderr_max_chars):
            if type(limit) is not int or limit <= 0:
                raise ValueError("output limits must be positive integers")




@dataclass(frozen=True, slots=True)
class ContextMessage:
    """将数据库序号与权威消息一同保存。"""
    sequence: int  # 会话内持久化的顺序号。
    agent_run_id: UUID  # 实际追加此记录的 Run。
    message: AnyMessage  # 权威持久化消息，不是模型投影。


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """表示一条已提交的滚动摘要。"""
    conversation_id: UUID  # 所属会话。
    revision: int  # 成功替换后单调递增的计数。
    covered_through_sequence: int  # 最后覆盖的完整历史边界。
    summary_text: str  # 明文历史数据，不具备 System 权威。
    source_run_id: UUID  # 生成此摘要的 Run。
    created_at: datetime  # 首次创建时间。
    updated_at: datetime  # 最近一次成功替换时间。


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """区分基于 Provider 锚点与纯本地计算的估算。"""
    tokens: int  # 用于准入判断的估算输入大小。
    source: Literal["PROVIDER_USAGE", "TOKENIZER_ESTIMATE"]  # 计数来源。
