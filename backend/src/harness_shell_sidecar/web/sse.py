"""Encode strict Agent turn events as UTF-8 Server-Sent Events frames."""

from __future__ import annotations

import json

from harness_shell_sidecar.agent.streaming import AgentTurnStreamEvent


def encode_sse_event(event: AgentTurnStreamEvent) -> bytes:
    """Encode one event with fixed LF framing and one compact JSON data line."""

    payload = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.type}\nid: {event.sequence}\ndata: {payload}\n\n".encode(
        "utf-8"
    )


__all__ = ["encode_sse_event"]
