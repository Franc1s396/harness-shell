"""对一个已配置的 Agent API 执行显式、非流式冒烟探测。"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from langchain_core.messages import HumanMessage
from pydantic import SecretStr, ValidationError

from harness_shell_sidecar.agent.contracts import ApiType, ModelApiConfig
from harness_shell_sidecar.agent.model_gateway import ModelGateway


REQUIRED_ENVIRONMENT = (
    "HARNESS_AGENT_PROBE_BASE_URL",
    "HARNESS_AGENT_PROBE_API_KEY",
    "HARNESS_AGENT_PROBE_MODEL",
    "HARNESS_AGENT_PROBE_API_TYPE",
)


def _configuration_from_environment() -> tuple[ModelApiConfig, SecretStr]:
    """构造严格的临时配置，不持久化秘密。"""

    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        raise SystemExit("missing required Agent provider probe environment")

    now = datetime.now(timezone.utc)
    try:
        config = ModelApiConfig(
            api_config_id=uuid4(),
            display_name="Agent provider probe",
            api_type=ApiType(os.environ["HARNESS_AGENT_PROBE_API_TYPE"]),
            base_url=os.environ["HARNESS_AGENT_PROBE_BASE_URL"],
            model=os.environ["HARNESS_AGENT_PROBE_MODEL"],
            api_key_secret_ref=uuid4(),
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    except (ValueError, ValidationError) as error:
        raise SystemExit("invalid Agent provider probe configuration") from error
    return config, SecretStr(os.environ["HARNESS_AGENT_PROBE_API_KEY"])


async def _probe(config: ModelApiConfig, api_key: SecretStr) -> tuple[str, int]:
    """仅调用配置指定的 API 路径，并返回有界的状态元数据。"""

    started = time.monotonic()
    status = "PASS"
    try:
        await ModelGateway().invoke(
            config,
            api_key,
            [
                HumanMessage(
                    content=(
                        "Reply with a brief acknowledgement. Do not call any tool."
                    )
                )
            ],
            asyncio.Event(),
        )
    except Exception:
        # Provider 错误可能包含 URL、请求头、请求体或响应正文。
        # 因此探测结果只暴露下列固定状态维度。
        status = "FAIL"
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    return status, latency_ms


def main() -> None:
    """仅在显式启用后运行，且不输出 Provider 敏感内容。"""

    if os.environ.get("HARNESS_RUN_AGENT_PROVIDER_PROBE") != "1":
        raise SystemExit(
            "set HARNESS_RUN_AGENT_PROVIDER_PROBE=1 to contact the configured provider"
        )

    config, api_key = _configuration_from_environment()
    status, latency_ms = asyncio.run(_probe(config, api_key))
    print(
        f"api_type={config.api_type.value} model={config.model} "
        f"status={status} latency_ms={latency_ms}"
    )
    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
