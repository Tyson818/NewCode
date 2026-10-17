from __future__ import annotations

from collections.abc import Iterable

from .command_tool import RunCommandTool
from .file_tools import ReadFileTool, ReplaceInFileTool, WriteFileTool
from .search_tools import FindFilesTool, SearchCodeTool
from .types import Tool


READ_ONLY_TOOLS = frozenset({"read_file", "find_files", "search_code"})
SIDE_EFFECT_TOOLS = frozenset({"write_file", "replace_in_file", "run_command"})
ROLE_TOOL_ROLES = {
    "team_status": ("lead", "member"), "team_manage": ("lead",),
    "team_tasks": ("lead", "member"), "team_message": ("lead", "member"),
    "team_integrate": ("lead",),
}


class ToolRegistry:
    def __init__(self) -> None:
        self.execution_role = "ordinary"
        self._tools: dict[str, Tool] = {}
        self._read_only: dict[str, bool] = {}
        self._do_visible: dict[str, bool] = {}

    def register(self, tool: Tool, *, read_only: bool | None = None, do_visible: bool | None = None) -> None:
        # 子对话创建新 registry view 时不会继承父角色；受控角色工具不可泄入普通执行者。
        roles = ROLE_TOOL_ROLES.get(tool.spec.name, getattr(tool, "execution_roles", None))
        if roles is not None and self.execution_role not in roles:
            return
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        self._tools[name] = tool
        self._read_only[name] = name in READ_ONLY_TOOLS if read_only is None else read_only
        self._do_visible[name] = name in READ_ONLY_TOOLS | SIDE_EFFECT_TOOLS if do_visible is None else do_visible

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_read_only(self, name: str) -> bool:
        return self._read_only.get(name, False)

    def has_side_effects(self, name: str) -> bool:
        return name in self._tools and not self.is_read_only(name)

    def do_visible_names(self) -> frozenset[str]:
        return frozenset(name for name, visible in self._do_visible.items() if visible)

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
