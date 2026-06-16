from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from mewcode.session import ChatMessage


class ProviderError(Exception):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class ChatProvider(Protocol):
    def stream_chat(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        ...
