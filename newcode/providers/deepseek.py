from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from newcode.config import AppConfig
from newcode.providers.base import ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.session import ChatMessage
from newcode.tools.types import ToolCall


@dataclass
class _ToolCallBuffer:
    id: str = ""
    name_parts: list[str] = field(default_factory=list)
    arguments_parts: list[str] = field(default_factory=list)

    def to_tool_call(self, fallback_id: str) -> ToolCall:
        raw_arguments = "".join(self.arguments_parts) or "{}"
        name = "".join(self.name_parts)
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return ToolCall(
                id=self.id or fallback_id,
                name="__tool_call_parse_error__",
                arguments={"raw_arguments": raw_arguments},
                raw_arguments=raw_arguments,
            )
        if not isinstance(parsed, dict):
            return ToolCall(
                id=self.id or fallback_id,
                name="__tool_call_parse_error__",
                arguments={"raw_arguments": raw_arguments},
                raw_arguments=raw_arguments,
            )
        return ToolCall(
            id=self.id or fallback_id,
            name=name,
            arguments=parsed,
            raw_arguments=raw_arguments,
        )


class DeepSeekProvider:
    def __init__(self, config: AppConfig, api_key: str, client: Any | None = None) -> None:
        self.config = config
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=config.base_url)
        self.client = client

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, object]] | None = None,
        allow_tool_calls: bool = True,
    ) -> Iterator[ProviderEvent]:
        request: dict[str, object] = {
            "model": self.config.model,
            "messages": [message_to_provider_dict(message) for message in messages],
            "stream": True,
        }
        if allow_tool_calls and tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        buffers: dict[int, _ToolCallBuffer] = {}
        try:
            stream = self.client.chat.completions.create(**request)
            for chunk in stream:
                text = _extract_delta_text(chunk)
                if text:
                    yield TextDelta(text)
                for item in _extract_tool_call_deltas(chunk):
                    index = item["index"]
                    buffer = buffers.setdefault(index, _ToolCallBuffer())
                    if item.get("id"):
                        buffer.id += item["id"]
                    if item.get("name"):
                        buffer.name_parts.append(item["name"])
                    if item.get("arguments"):
                        buffer.arguments_parts.append(item["arguments"])

            if buffers:
                tool_calls = [
                    buffer.to_tool_call(fallback_id=f"tool_call_{index}")
                    for index, buffer in sorted(buffers.items())
                ]
                yield ToolCallEvent(tool_calls)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("模型服务请求失败，请稍后重试。", exc) from exc


def message_to_provider_dict(message: ChatMessage) -> dict[str, object]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content or "",
        }

    payload: dict[str, object] = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.raw_arguments,
                },
            }
            for tool_call in message.tool_calls
        ]
    return payload


def _extract_delta_text(chunk: Any) -> str:
    delta = _first_choice_delta(chunk)
    content = _get_attr_or_key(delta, "content")
    return content if isinstance(content, str) else ""


def _extract_tool_call_deltas(chunk: Any) -> list[dict[str, Any]]:
    delta = _first_choice_delta(chunk)
    tool_calls = _get_attr_or_key(delta, "tool_calls")
    if not tool_calls:
        return []

    result: list[dict[str, Any]] = []
    for fallback_index, tool_call in enumerate(tool_calls):
        function = _get_attr_or_key(tool_call, "function")
        index = _get_attr_or_key(tool_call, "index")
        result.append(
            {
                "index": index if isinstance(index, int) else fallback_index,
                "id": _get_attr_or_key(tool_call, "id") or "",
                "name": _get_attr_or_key(function, "name") or "",
                "arguments": _get_attr_or_key(function, "arguments") or "",
            }
        )
    return result


def _first_choice_delta(chunk: Any) -> Any:
    choices = _get_attr_or_key(chunk, "choices")
    if not choices:
        return None
    return _get_attr_or_key(choices[0], "delta")


def _get_attr_or_key(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
