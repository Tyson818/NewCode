from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from newcode.session import ChatMessage
from newcode.tools.types import ToolCall


class ProviderError(Exception):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    tool_calls: list[ToolCall]


ProviderEvent = TextDelta | ToolCallEvent


class ChatProvider(Protocol):
    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, object]] | None = None,
        allow_tool_calls: bool = True,
    ) -> Iterator[ProviderEvent]:
        ...
