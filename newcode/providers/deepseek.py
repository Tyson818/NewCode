from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from html import unescape
from typing import Any

from newcode.config import AppConfig
from newcode.providers.base import ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.session import ChatMessage
from newcode.tools.types import ToolCall


DSML_TOOL_CALLS_START = "<｜｜DSML｜｜tool_calls>"
DSML_TOOL_CALLS_END = "</｜｜DSML｜｜tool_calls>"
DISALLOWED_TOOL_CALL_TEXT = "模型尝试再次调用工具，但本阶段最终回复不允许继续调用工具。"
DSML_INVOKE_PATTERN = re.compile(
    r'<｜｜DSML｜｜invoke\s+name="([^"]+)">(.*?)</｜｜DSML｜｜invoke>',
    re.DOTALL,
)
DSML_PARAMETER_PATTERN = re.compile(
    r'<｜｜DSML｜｜parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?>(.*?)</｜｜DSML｜｜parameter>',
    re.DOTALL,
)


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
        dsml_buffer = ""
        dsml_mode = False
        pending_text = ""
        try:
            stream = self.client.chat.completions.create(**request)
            for chunk in stream:
                text = _extract_delta_text(chunk)
                if text:
                    pending_text += text
                    events, pending_text, dsml_buffer, dsml_mode = _drain_content_text(
                        pending_text,
                        dsml_buffer,
                        dsml_mode,
                        allow_tool_calls=allow_tool_calls,
                    )
                    yield from events
                for item in _extract_tool_call_deltas(chunk):
                    index = item["index"]
                    buffer = buffers.setdefault(index, _ToolCallBuffer())
                    if item.get("id"):
                        buffer.id += item["id"]
                    if item.get("name"):
                        buffer.name_parts.append(item["name"])
                    if item.get("arguments"):
                        buffer.arguments_parts.append(item["arguments"])

            if pending_text and not dsml_mode:
                yield TextDelta(pending_text)
                pending_text = ""
            if buffers:
                tool_calls = [
                    buffer.to_tool_call(fallback_id=f"tool_call_{index}")
                    for index, buffer in sorted(buffers.items())
                ]
                yield ToolCallEvent(tool_calls)
            if dsml_mode and dsml_buffer:
                if allow_tool_calls:
                    yield ToolCallEvent(
                        [
                            ToolCall(
                                id="dsml_tool_call_parse_error",
                                name="__tool_call_parse_error__",
                                arguments={"raw_arguments": dsml_buffer},
                                raw_arguments=dsml_buffer,
                            )
                        ]
                    )
                else:
                    yield TextDelta(DISALLOWED_TOOL_CALL_TEXT)
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


def _try_finish_dsml(
    dsml_buffer: str,
    *,
    allow_tool_calls: bool,
) -> tuple[ProviderEvent | None, str, bool, str]:
    end_index = dsml_buffer.find(DSML_TOOL_CALLS_END)
    if end_index < 0:
        return None, dsml_buffer, True, ""

    end_position = end_index + len(DSML_TOOL_CALLS_END)
    dsml_text = dsml_buffer[:end_position]
    trailing_text = dsml_buffer[end_position:]
    if not allow_tool_calls:
        return TextDelta(DISALLOWED_TOOL_CALL_TEXT), "", False, trailing_text
    return ToolCallEvent(_parse_dsml_tool_calls(dsml_text)), "", False, trailing_text


def _drain_content_text(
    pending_text: str,
    dsml_buffer: str,
    dsml_mode: bool,
    *,
    allow_tool_calls: bool,
) -> tuple[list[ProviderEvent], str, str, bool]:
    events: list[ProviderEvent] = []

    while pending_text:
        if dsml_mode:
            dsml_buffer += pending_text
            pending_text = ""
            dsml_event, dsml_buffer, dsml_mode, trailing_text = _try_finish_dsml(
                dsml_buffer,
                allow_tool_calls=allow_tool_calls,
            )
            if dsml_event:
                events.append(dsml_event)
            if trailing_text:
                pending_text = trailing_text
            continue

        start_index = pending_text.find(DSML_TOOL_CALLS_START)
        if start_index >= 0:
            before = pending_text[:start_index]
            if before:
                events.append(TextDelta(before))
            dsml_buffer = pending_text[start_index:]
            pending_text = ""
            dsml_mode = True
            dsml_event, dsml_buffer, dsml_mode, trailing_text = _try_finish_dsml(
                dsml_buffer,
                allow_tool_calls=allow_tool_calls,
            )
            if dsml_event:
                events.append(dsml_event)
            if trailing_text:
                pending_text = trailing_text
            continue

        keep_length = _dsml_start_prefix_suffix_length(pending_text)
        emit_length = len(pending_text) - keep_length
        if emit_length > 0:
            events.append(TextDelta(pending_text[:emit_length]))
            pending_text = pending_text[emit_length:]
        break

    return events, pending_text, dsml_buffer, dsml_mode


def _dsml_start_prefix_suffix_length(text: str) -> int:
    max_length = min(len(text), len(DSML_TOOL_CALLS_START) - 1)
    for length in range(max_length, 0, -1):
        if DSML_TOOL_CALLS_START.startswith(text[-length:]):
            return length
    return 0


def _parse_dsml_tool_calls(dsml_text: str) -> list[ToolCall]:
    tool_calls: list[ToolCall] = []
    for index, match in enumerate(DSML_INVOKE_PATTERN.finditer(dsml_text)):
        name = unescape(match.group(1))
        body = match.group(2)
        arguments = _parse_dsml_parameters(body)
        raw_arguments = json.dumps(arguments, ensure_ascii=False)
        tool_calls.append(
            ToolCall(
                id=f"dsml_tool_call_{index}",
                name=name,
                arguments=arguments,
                raw_arguments=raw_arguments,
            )
        )

    if tool_calls:
        return tool_calls

    return [
        ToolCall(
            id="dsml_tool_call_parse_error",
            name="__tool_call_parse_error__",
            arguments={"raw_arguments": dsml_text},
            raw_arguments=dsml_text,
        )
    ]


def _parse_dsml_parameters(body: str) -> dict[str, object]:
    arguments: dict[str, object] = {}
    for match in DSML_PARAMETER_PATTERN.finditer(body):
        name = unescape(match.group(1))
        is_string = match.group(2) != "false"
        value_text = unescape(match.group(3))
        arguments[name] = value_text if is_string else _parse_non_string_parameter(value_text)
    return arguments


def _parse_non_string_parameter(value_text: str) -> object:
    text = value_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


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
