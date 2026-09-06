"""将严格 Agent 轮次事件编码为 UTF-8 SSE 帧。"""

from __future__ import annotations

import json

from harness_shell_sidecar.agent.streaming import AgentTurnStreamEvent


def encode_sse_event(event: AgentTurnStreamEvent) -> bytes:
    """使用固定 LF 分帧和一行紧凑 JSON 数据编码事件。"""

    payload = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.type}\nid: {event.sequence}\ndata: {payload}\n\n".encode(
        "utf-8"
    )


__all__ = ["encode_sse_event"]
