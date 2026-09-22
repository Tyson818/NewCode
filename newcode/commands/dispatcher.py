"""无副作用的 slash 命令解析与补全分派。"""

from __future__ import annotations

from .registry import CommandRegistry, CommandRegistryError
from .types import (
    CommandOutcome,
    CommandOutcomeKind,
    CommandParseKind,
    CommandParseResult,
    CompletionResult,
    ParsedCommand,
)
from .ui import UIControl


UNKNOWN_COMMAND_MESSAGE = "未知命令。输入 /help 查看可用命令。"


class CommandDispatcher:
    """解析、引导、补全及受控 handler 调用。"""

    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    def parse(self, user_input: str) -> CommandParseResult:
        """把输入区分为空、普通文本、已注册命令或未知 slash 命令。"""

        if not isinstance(user_input, str):
            raise TypeError("用户输入必须是字符串")
        stripped = user_input.strip()
        if not stripped:
            return CommandParseResult(CommandParseKind.EMPTY, user_input)
        if not stripped.startswith("/"):
            return CommandParseResult(CommandParseKind.TEXT, user_input)

        body = stripped[1:]
        pieces = body.split(maxsplit=1)
        name = pieces[0] if pieces else ""
        argument_text = pieces[1].strip() if len(pieces) == 2 else ""
        definition = self.registry.get(f"/{name}")
        if definition is None:
            return CommandParseResult(
                CommandParseKind.UNKNOWN,
                user_input,
                unknown_name=f"/{name}",
            )
        return CommandParseResult(
            CommandParseKind.COMMAND,
            user_input,
            command=ParsedCommand(
                definition=definition,
                invoked_name=f"/{name}",
                arguments=tuple(argument_text.split()) if argument_text else (),
                argument_text=argument_text,
            ),
        )

    def dispatch_input(self, user_input: str, ui: UIControl) -> CommandParseResult:
        """提供未知命令引导；匹配命令只返回解析结果。"""

        result = self.parse(user_input)
        if result.kind is CommandParseKind.UNKNOWN:
            ui.error("command_unknown", UNKNOWN_COMMAND_MESSAGE)
        return result

    def execute(self, parsed: ParsedCommand, runtime: object, ui: UIControl) -> CommandOutcome:
        """执行已匹配的 handler；未知命令永远不会到达此处。"""

        handler = parsed.definition.handler
        if handler is None:
            ui.error("command_handler_unavailable", "命令当前不可用。")
            return CommandOutcome(kind=CommandOutcomeKind.HANDLED)
        outcome = handler(runtime, parsed, ui)
        if not isinstance(outcome, CommandOutcome):
            ui.error("command_handler_failed", "命令未完成。")
            return CommandOutcome(kind=CommandOutcomeKind.HANDLED)
        return outcome

    def complete(self, user_input: str, ui: UIControl) -> CompletionResult:
        """完成命令名；多匹配仅展示菜单，绝不改变其他状态。"""

        if not isinstance(user_input, str) or not user_input.startswith("/"):
            return CompletionResult()
        if any(character.isspace() for character in user_input):
            return CompletionResult()
        command_part = user_input
        try:
            candidates = self.registry.completion_candidates(command_part)
        except CommandRegistryError:
            return CompletionResult()
        if len(candidates) == 1:
            return CompletionResult(replacement=candidates[0].name, candidates=candidates)
        if len(candidates) > 1:
            ui.show_completion_menu(candidates)
            return CompletionResult(candidates=candidates)
        return CompletionResult()
