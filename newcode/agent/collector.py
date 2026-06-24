from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from newcode.agent.events import AgentEvent, AgentTextDelta, AgentUsage
from newcode.providers.base import ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.tools.types import ToolCall


@dataclass
class AgentTurnResult:
    assistant_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Any | None = None
    provider_error: Exception | None = None


class StreamingTurnCollector:
    def __init__(self) -> None:
        self._assistant_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._usage: Any | None = None
        self._provider_error: Exception | None = None

    def consume(self, provider_events: Iterable[ProviderEvent]) -> Iterator[AgentEvent]:
        try:
            for event in provider_events:
                if isinstance(event, TextDelta):
                    self._assistant_parts.append(event.text)
                    yield AgentTextDelta(event.text)
                    continue

                if isinstance(event, ToolCallEvent):
                    self._tool_calls.extend(event.tool_calls)
                    continue

                usage = _extract_usage(event)
                if usage is not None:
                    self._usage = usage
                    yield AgentUsage(usage)
        except ProviderError as exc:
            self._provider_error = exc

    @property
    def result(self) -> AgentTurnResult:
        return AgentTurnResult(
            assistant_content="".join(self._assistant_parts),
            tool_calls=list(self._tool_calls),
            usage=self._usage,
            provider_error=self._provider_error,
        )


def _extract_usage(event: object) -> Any | None:
    if hasattr(event, "usage"):
        return getattr(event, "usage")
    if isinstance(event, dict) and "usage" in event:
        return event["usage"]
    return None
