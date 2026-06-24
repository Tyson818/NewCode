from __future__ import annotations

from enum import Enum


class AgentMode(Enum):
    PLAN = "plan"
    DO = "do"


PLAN_TOOL_NAMES = frozenset({"read_file", "find_files", "search_code"})
DO_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
    }
)


def allowed_tool_names(mode: AgentMode) -> frozenset[str]:
    if mode is AgentMode.PLAN:
        return PLAN_TOOL_NAMES
    return DO_TOOL_NAMES


def is_tool_allowed(name: str, mode: AgentMode) -> bool:
    return name in allowed_tool_names(mode)
