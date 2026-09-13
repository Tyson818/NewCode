from __future__ import annotations

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from newcode.tools.registry import ToolRegistry


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


def allowed_tool_names(mode: AgentMode, registry: ToolRegistry | None = None) -> frozenset[str]:
    if mode is AgentMode.PLAN:
        return PLAN_TOOL_NAMES
    if registry is not None:
        return registry.do_visible_names()
    return DO_TOOL_NAMES


def is_tool_allowed(name: str, mode: AgentMode, registry: ToolRegistry | None = None) -> bool:
    return name in allowed_tool_names(mode, registry)
