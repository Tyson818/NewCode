"""Skill 激活后的纯工具可见性计算。"""

from __future__ import annotations

from newcode.agent.mode import AgentMode, allowed_tool_names
from newcode.tools.registry import ToolRegistry

from .state import ActiveSkillState


LOAD_SKILL_TOOL_NAME = "load_skill"


def visible_tool_names(
    mode: AgentMode,
    registry: ToolRegistry,
    state: ActiveSkillState,
) -> frozenset[str]:
    """普通工具取 mode 与所有 activation whitelist 的交集。"""

    names = set(allowed_tool_names(mode, registry))
    for activation in state.activations:
        names.intersection_update(activation.loaded.metadata.frontmatter.tools)
    if registry.get(LOAD_SKILL_TOOL_NAME) is not None:
        names.add(LOAD_SKILL_TOOL_NAME)
    return frozenset(names)
