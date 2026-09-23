"""经既有执行链调用的系统级 ``load_skill`` 工具。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolContext, ToolFailure, ToolResult, ToolSpec

from .loader import SkillLoader
from .policy import LOAD_SKILL_TOOL_NAME
from .state import ActiveSkillState
from .types import SkillCatalog, SkillValidationError


class LoadSkillTool:
    """加载会改变 activation state，因此由注册方标记为非只读。"""

    def __init__(
        self,
        *,
        catalog: Callable[[], SkillCatalog],
        state: ActiveSkillState,
        registry: ToolRegistry,
        loader: SkillLoader | None = None,
        available_models: Iterable[str] = (),
    ) -> None:
        self._catalog = catalog
        self._state = state
        self._registry = registry
        self._loader = loader or SkillLoader()
        self._available_models = tuple(available_models)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LOAD_SKILL_TOOL_NAME,
            description="按名称加载一个已发现的本地受控 Skill。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "parameters": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolResult:
        del context
        name = arguments.get("name")
        parameters = arguments.get("parameters", {})
        if not isinstance(name, str) or not isinstance(parameters, Mapping):
            raise ToolFailure("skill_parameter_invalid", "Skill 参数无效。")
        try:
            loaded = self._loader.load(
                name,
                parameters,
                self._catalog(),
                available_tools=self._registry.names(),
                available_models=self._available_models,
            )
        except SkillValidationError as exc:
            raise ToolFailure(exc.code, "Skill 加载失败。") from exc
        activation = self._state.activate(loaded)
        return ToolResult.success(
            self.spec.name,
            {"name": loaded.metadata.frontmatter.name, "activation": activation.sequence},
        )
