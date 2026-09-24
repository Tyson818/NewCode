"""子 Agent 的工具可见性上限与隔离 registry view。"""

from __future__ import annotations

from collections.abc import Iterable

from newcode.agent.mode import AgentMode, PLAN_TOOL_NAMES
from newcode.permissions.types import PermissionMode
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import Tool

from .types import AgentDefinition, ParentPolicySnapshot, TaskExecution


def child_tool_names(
    registry: ToolRegistry,
    *,
    launch: ParentPolicySnapshot,
    latest: ParentPolicySnapshot,
    allow: Iterable[str],
    deny: Iterable[str] = (),
    mode: AgentMode = AgentMode.DO,
    execution: TaskExecution = TaskExecution.FOREGROUND,
) -> frozenset[str]:
    """求交只能缩小启动权限；agent/load_skill 永远排除。"""
    role_allow = frozenset(allow)
    role_deny = frozenset(deny)
    candidates = (
        frozenset(registry.names())
        & launch.visible_tools
        & latest.visible_tools
        & role_allow
        - role_deny
        - {"agent", "load_skill"}
    )
    mode_names = PLAN_TOOL_NAMES if mode is AgentMode.PLAN else registry.do_visible_names()
    candidates &= mode_names
    if execution is TaskExecution.BACKGROUND:
        candidates = frozenset(name for name in candidates if registry.is_read_only(name))
    return frozenset(candidates)


def definition_tool_names(
    definition: AgentDefinition,
    registry: ToolRegistry,
    *,
    mode: AgentMode = AgentMode.DO,
    execution: TaskExecution = TaskExecution.FOREGROUND,
) -> frozenset[str]:
    """发现阶段按最终 registry 校验引用；runner 再应用实时快照。"""
    allow = frozenset(definition.tools_allow)
    deny = frozenset(definition.tools_deny)
    names = frozenset(registry.names()) & allow - deny - {"agent", "load_skill"}
    mode_names = PLAN_TOOL_NAMES if mode is AgentMode.PLAN else registry.do_visible_names()
    names &= mode_names
    if execution is TaskExecution.BACKGROUND:
        names = frozenset(name for name in names if registry.is_read_only(name))
    return names


def most_restrictive_permission_mode(*modes: PermissionMode) -> PermissionMode:
    rank = {
        PermissionMode.STRICT: 0,
        PermissionMode.DEFAULT: 1,
        PermissionMode.PERMISSIVE: 2,
        PermissionMode.TRUSTED: 2,
    }
    return min(modes, key=lambda mode: rank[mode]) if modes else PermissionMode.DEFAULT


def isolated_registry_view(
    source: ToolRegistry,
    names: Iterable[str],
    wrappers: dict[str, Tool],
) -> ToolRegistry:
    """创建独立的注册表元数据；工具执行仍由 AgentLoop executor 调用。"""
    view = _ChildToolRegistry()
    for name in source.names():
        if name not in names:
            continue
        tool = wrappers.get(name, source.get(name))
        if tool is not None:
            view.register(
                tool,
                read_only=source.is_read_only(name),
                do_visible=name in source.do_visible_names(),
            )
    return view


class _ChildToolRegistry(ToolRegistry):
    """阻止 AgentLoop 自动注册 load_skill，同时不把它放进 tools/catalog。"""

    def get(self, name: str):
        if name == "load_skill":
            return _NO_SKILL_TOOL
        return super().get(name)


class _SkillRegistrationSentinel:
    pass


_NO_SKILL_TOOL = _SkillRegistrationSentinel()
