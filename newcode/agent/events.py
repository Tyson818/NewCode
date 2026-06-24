from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newcode.tools.types import ToolCall, ToolResult


@dataclass(frozen=True)
class AgentTextDelta:
    text: str


@dataclass(frozen=True)
class AgentToolCallStarted:
    tool_call: ToolCall
    iteration: int | None = None


@dataclass(frozen=True)
class AgentToolResult:
    tool_call: ToolCall
    result: ToolResult
    iteration: int | None = None


@dataclass(frozen=True)
class AgentToolError:
    tool_call: ToolCall | None
    message: str
    code: str = "tool_error"
    iteration: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentIterationStarted:
    iteration: int
    max_iterations: int | None = None


@dataclass(frozen=True)
class AgentFinalAnswer:
    content: str


@dataclass(frozen=True)
class AgentStopped:
    reason: Any
    message: str
    iteration: int | None = None


@dataclass(frozen=True)
class AgentUsage:
    usage: Any


AgentEvent = (
    AgentTextDelta
    | AgentToolCallStarted
    | AgentToolResult
    | AgentToolError
    | AgentIterationStarted
    | AgentFinalAnswer
    | AgentStopped
    | AgentUsage
)
