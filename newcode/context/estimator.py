from __future__ import annotations

import json
import math
from typing import Any, Sequence

from newcode.session import ChatMessage

from .types import UsageAnchor

MESSAGE_OVERHEAD = 12
TOOL_OVERHEAD = 24
ESTIMATE_MULTIPLIER = 1.20


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_value(value: Any) -> int:
    return math.ceil(len(_stable_json(value)) / 2)


def estimate_message(message: ChatMessage) -> int:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    tool_count = 0
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.raw_arguments}
            for call in message.tool_calls
        ]
        tool_count += len(message.tool_calls)
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
        tool_count += 1
    return estimate_value(payload) + MESSAGE_OVERHEAD + tool_count * TOOL_OVERHEAD


def estimate_messages(messages: Sequence[ChatMessage]) -> int:
    raw = sum(estimate_message(message) for message in messages)
    return math.ceil(raw * ESTIMATE_MULTIPLIER)


class TokenEstimator:
    def __init__(self) -> None:
        self._anchor: UsageAnchor | None = None

    @property
    def anchor(self) -> UsageAnchor | None:
        return self._anchor

    def record_usage(self, prompt_tokens: object, session_version: int) -> bool:
        if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
            return False
        self._anchor = UsageAnchor(prompt_tokens=prompt_tokens, session_version=session_version)
        return True

    def invalidate_for_version(self, session_version: int) -> None:
        if self._anchor is not None and self._anchor.session_version != session_version:
            self._anchor = None

    def estimate(self, messages: Sequence[ChatMessage], session_version: int) -> int:
        if self._anchor is None or self._anchor.session_version != session_version:
            return estimate_messages(messages)
        return self._anchor.prompt_tokens + estimate_messages(messages)
