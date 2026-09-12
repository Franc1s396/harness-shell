"""使用兼容的 Provider usage 或显式本地编码估算上下文。"""
from __future__ import annotations
import hashlib
import json
from math import floor
from collections.abc import Sequence
import tiktoken
from langchain_core.messages import AIMessage
from .context import ContextService, SYSTEM_MESSAGE
from .context_models import AgentContextPolicy, ContextError, ContextMessage, ContextSummary, TokenEstimate
from .contracts import ModelApiConfig
from .model_gateway import model_input_payload


class ContextBudget:
    """持有不可变本地编码器并计算输入准入预算。"""
    def __init__(self, encoding: tiktoken.Encoding, policy: AgentContextPolicy) -> None:
        """借用已初始化的离线编码器和冻结策略。"""
        self._encoding = encoding  # 运行时拥有的不可变编码器。
        self._policy = policy  # 不含秘密的估算身份信息。

    @staticmethod
    def assert_fits(config: ModelApiConfig, tokens: int) -> None:
        """无法预留配置要求的输出预算时终止本轮。"""
        if tokens > config.context_window_size - config.max_output_tokens:
            raise ContextError("CONTEXT_BUDGET_EXCEEDED", "the context exceeds the configured input token budget")

    @staticmethod
    def should_compact(config: ModelApiConfig, tokens: int) -> bool:
        """达到配置的压缩阈值时即触发，包含相等情形。"""
        return tokens >= floor(config.context_window_size * config.context_compaction_threshold_ratio)

    def estimate_payload(self, payload: dict[str, object]) -> int:
        """计算确定性协议输入文本的 token 数，不猜测字符比例。"""
        model_input = {key: value for key, value in payload.items() if key != "parallel_tool_calls"}
        encoded = json.dumps(model_input, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return len(self._encoding.encode(encoded, disallowed_special=()))

    def request_identity(self, config: ModelApiConfig) -> str:
        """为静态输入和配置生成指纹，不包含凭据或历史。"""
        values = config.model_dump(mode="json", include={"api_config_id", "api_type", "base_url", "model"})
        values.update(request_mapping_version=2, tokenizer=self._policy.tokenizer_encoding,
                      static_input=model_input_payload(config, [SYSTEM_MESSAGE], include_tools=True))
        return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")).hexdigest()

    def estimate(self, config: ModelApiConfig, records: Sequence[ContextMessage],
                 summaries: Sequence[ContextSummary]) -> TokenEstimate:
        """使用最新兼容 usage，并仅累加其回复之后的消息。"""
        # 1. 确定本轮配置指纹和摘要版本，只在尚未被摘要覆盖的消息中寻找 usage 锚点。
        identity = self.request_identity(config)
        revision = summaries[-1].revision if summaries else 0
        covered = summaries[-1].covered_through_sequence if summaries else 0
        effective = [record for record in records if record.sequence > covered]
        # 2. 从新到旧查找兼容且统计有效的 AI 回复；配置或摘要版本不同的锚点不可复用。
        for index in range(len(effective) - 1, -1, -1):
            message = effective[index].message
            if not isinstance(message, AIMessage):
                continue
            anchor = message.additional_kwargs.get("harness_context_anchor")
            usage = message.usage_metadata
            if (not isinstance(anchor, dict) or type(anchor.get("schema_version")) is not int
                    or anchor.get("schema_version") != 1 or type(anchor.get("context_revision")) is not int
                    or anchor.get("context_revision") != revision or anchor.get("request_identity") != identity
                    or not usage):
                continue
            incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
            if type(incoming) is not int or type(outgoing) is not int or incoming <= 0 or outgoing < 0:
                continue
            # 3. 锚点的输入和输出已覆盖当时上下文，仅追加其后新增消息的估算，避免重复计数。
            later = [record.message for record in effective[index + 1:]]
            increment = self.estimate_payload(model_input_payload(config, later, include_tools=False)) if later else 0
            return TokenEstimate(incoming + outgoing + increment, "PROVIDER_USAGE")
        # 4. 没有可用 usage 时，本地估算完整投影：System、摘要、有效历史和工具定义。
        payload = model_input_payload(config, ContextService.project(records, summaries), include_tools=True)
        return TokenEstimate(self.estimate_payload(payload), "TOKENIZER_ESTIMATE")
