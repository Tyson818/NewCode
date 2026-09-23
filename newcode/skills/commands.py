"""不改动 Chapter 10 静态注册表的会话级 Skill 命令 overlay。"""

from __future__ import annotations

from newcode.commands.dispatcher import CommandDispatcher
from newcode.commands.registry import CommandRegistry
from newcode.commands.types import (
    CommandCategory,
    CommandDefinition,
    CommandOutcome,
    CommandOutcomeKind,
    CommandParseKind,
    CommandParseResult,
    ParsedCommand,
)
from newcode.commands.ui import UIControl

from .state import ActiveSkillState


class SkillCommandOverlay:
    """由 activation 快照投影出的只读命令视图。"""

    def __init__(self, static_registry: CommandRegistry, state: ActiveSkillState) -> None:
        self._static_registry = static_registry
        self._state = state

    def definitions(self) -> tuple[CommandDefinition, ...]:
        definitions: list[CommandDefinition] = []
        for activation in self._state.activations:
            name = activation.loaded.metadata.frontmatter.name
            command_name = f"/{name}"
            if self._static_registry.get(command_name) is not None:
                continue
            definitions.append(
                CommandDefinition(
                    name=command_name,
                    description=activation.loaded.metadata.frontmatter.description,
                    usage=f"{command_name} key=value",
                    category=CommandCategory.AI_PRESET,
                    argument_hint="key=value",
                    handler=self._handle,
                )
            )
        return tuple(definitions)

    def parse(self, user_input: str) -> ParsedCommand | None:
        stripped = user_input.strip()
        if not stripped.startswith("/"):
            return None
        name, _, argument_text = stripped[1:].partition(" ")
        normalized = name.casefold()
        for definition in self.definitions():
            if definition.name[1:] == normalized:
                text = argument_text.strip()
                return ParsedCommand(definition, f"/{name}", tuple(text.split()) if text else (), text)
        return None

    def _handle(self, runtime: object, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
        del runtime
        activation = next(
            (item for item in self._state.activations if item.loaded.metadata.frontmatter.name == parsed.definition.name[1:]),
            None,
        )
        if activation is None:
            ui.error("skill_not_found", "Skill 当前不可用。")
            return CommandOutcome(CommandOutcomeKind.HANDLED)
        arguments: dict[str, str] = {}
        for item in parsed.arguments:
            if item.count("=") != 1:
                ui.error("skill_parameter_invalid", "Skill 参数无效。")
                return CommandOutcome(CommandOutcomeKind.HANDLED)
            key, value = item.split("=", 1)
            if not key or not value or key in arguments:
                ui.error("skill_parameter_invalid", "Skill 参数无效。")
                return CommandOutcome(CommandOutcomeKind.HANDLED)
            arguments[key] = value
        declared = {parameter.name for parameter in activation.loaded.metadata.frontmatter.parameters}
        if set(arguments) != declared:
            ui.error("skill_parameter_missing", "Skill 参数不完整。")
            return CommandOutcome(CommandOutcomeKind.HANDLED)
        return CommandOutcome(
            CommandOutcomeKind.SKILL_REQUEST,
            skill_name=activation.loaded.metadata.frontmatter.name,
            skill_parameters=tuple(sorted(arguments.items())),
        )


class SkillCommandDispatcher:
    """静态 dispatcher 优先；overlay 仅产生待执行请求，不运行 Skill。"""

    def __init__(self, static: CommandDispatcher, overlay: SkillCommandOverlay) -> None:
        self._static = static
        self._overlay = overlay

    def parse(self, user_input: str) -> CommandParseResult:
        parsed = self._static.parse(user_input)
        if parsed.kind is not CommandParseKind.UNKNOWN:
            return parsed
        command = self._overlay.parse(user_input)
        if command is None:
            return parsed
        return CommandParseResult(CommandParseKind.COMMAND, user_input, command=command)

    def dispatch_input(self, user_input: str, ui: UIControl) -> CommandParseResult:
        parsed = self.parse(user_input)
        if parsed.kind is CommandParseKind.UNKNOWN:
            ui.error("command_unknown", "未知命令。输入 /help 查看可用命令。")
        return parsed

    def execute(self, parsed: ParsedCommand, runtime: object, ui: UIControl) -> CommandOutcome:
        return self._static.execute(parsed, runtime, ui)
