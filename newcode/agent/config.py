from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class AgentLoopConfig:
    max_iterations: int = 8
    unknown_tool_threshold: int = 2
    tool_error_threshold: int = 3


class StopReason(Enum):
    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    USER_CANCELLED = "user_cancelled"
    UNKNOWN_TOOL_LIMIT = "unknown_tool_limit"
    DISALLOWED_TOOL_CALL = "disallowed_tool_call"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR_LIMIT = "tool_error_limit"
