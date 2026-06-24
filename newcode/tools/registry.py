from __future__ import annotations

from collections.abc import Iterable

from .command_tool import RunCommandTool
from .file_tools import ReadFileTool, ReplaceInFileTool, WriteFileTool
from .search_tools import FindFilesTool, SearchCodeTool
from .types import Tool


READ_ONLY_TOOLS = frozenset({"read_file", "find_files", "search_code"})
SIDE_EFFECT_TOOLS = frozenset({"write_file", "replace_in_file", "run_command"})


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_read_only(self, name: str) -> bool:
        return name in READ_ONLY_TOOLS

    def has_side_effects(self, name: str) -> bool:
        return name in SIDE_EFFECT_TOOLS

    def to_openai_tools(
        self,
        tool_names: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        allowed_names = set(tool_names) if tool_names is not None else None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters,
                },
            }
            for tool in self._tools.values()
            if allowed_names is None or tool.spec.name in allowed_names
        ]


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ReplaceInFileTool())
    registry.register(RunCommandTool())
    registry.register(FindFilesTool())
    registry.register(SearchCodeTool())
    return registry
