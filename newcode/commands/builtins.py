"""Chapter 10 的固定内置命令及其声明式 handler。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .registry import CommandRegistry
from .types import (
    CommandCategory,
    CommandDefinition,
    CommandOutcome,
    CommandOutcomeKind,
    ParsedCommand,
)
from .ui import UIControl


REVIEW_AI_INPUT = "审查当前工作区未提交变更，说明风险、证据和建议，不擅自修改。"


@dataclass
class CommandRuntime:
    """内置 handler 可用的窄本地状态与安全查询回调。"""

    registry: CommandRegistry
    manual_compact: Callable[[], str]
    list_sessions: Callable[[], tuple[Any, ...]]
    list_memory: Callable[[str], tuple[Any, ...]]
    permission_summary: Callable[[], str]
    status_summary: Callable[[], str]


def create_builtin_registry() -> CommandRegistry:
    """创建唯一的固定十项命令表；不读取配置或运行时输入。"""

    return CommandRegistry(
        (
            _definition("/help", ("/h",), "显示可用命令或指定命令的用法", "/help [命令]", "命令", _help),
            _definition("/compact", (), "手动压缩当前上下文", "/compact", "", _compact),
            _definition("/clear", (), "安全保存后开始新会话", "/clear", "", _clear),
            _definition("/plan", (), "切换到 Plan Mode", "/plan", "", _plan),
            _definition("/do", (), "切换到 Do Mode", "/do", "", _do),
            _definition("/session", ("/sessions", "/resume"), "列出或恢复当前工作区会话", "/session [list|resume <session-id>]", "list | resume <session-id>", _session),
            _definition("/memory", (), "查看已筛选的本地记忆元数据", "/memory [user|project|all]", "user | project | all", _memory),
            _definition("/permission", (), "查看当前权限安全摘要", "/permission", "", _permission),
            _definition("/status", (), "查看当前会话与服务安全状态", "/status", "", _status),
            _definition("/review", (), "以固定请求审查当前工作区变更", "/review", "", _review),
        )
    )


def _definition(
    name: str,
    aliases: tuple[str, ...],
    description: str,
    usage: str,
    argument_hint: str,
    handler: Callable[[CommandRuntime, ParsedCommand, UIControl], CommandOutcome],
) -> CommandDefinition:
    category = CommandCategory.UI_STATE if name in ("/plan", "/do") else CommandCategory.LOCAL
    if name == "/review":
        category = CommandCategory.AI_PRESET
    return CommandDefinition(name, aliases, description, usage, category, argument_hint, False, handler)


def _handled() -> CommandOutcome:
    return CommandOutcome(CommandOutcomeKind.HANDLED)


def _invalid(ui: UIControl, usage: str) -> CommandOutcome:
    ui.error("command_invalid_arguments", f"用法：{usage}")
    return _handled()


def _help(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if len(parsed.arguments) > 1:
        return _invalid(ui, "/help [命令]")
    if not parsed.arguments:
        ui.show_help(runtime.registry.visible_definitions())
        return _handled()
    name = parsed.arguments[0]
    definition = runtime.registry.get(name if name.startswith("/") else f"/{name}")
    if definition is None or definition.hidden:
        ui.error("command_unknown", "未知命令。输入 /help 查看可用命令。")
        return _handled()
    ui.show_help((definition,))
    return _handled()


def _compact(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/compact")
    status = runtime.manual_compact()
    messages = {
        "compacted": "上下文已压缩。",
        "no_history": "没有可压缩的历史。",
        "failed": "上下文压缩未完成。",
    }
    ui.info(messages.get(status, "上下文压缩未完成。"))
    return _handled()


def _clear(_runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/clear")
    return CommandOutcome(CommandOutcomeKind.CLEAR_SESSION)


def _plan(_runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/plan")
    return CommandOutcome(CommandOutcomeKind.MODE_CHANGE, mode="plan")


def _do(_runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/do")
    return CommandOutcome(CommandOutcomeKind.MODE_CHANGE, mode="do")


def _session(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    invoked = parsed.invoked_name.casefold()
    arguments = parsed.arguments
    if invoked == "/sessions":
        arguments = ("list", *arguments)
    elif invoked == "/resume":
        arguments = ("resume", *arguments)
    if not arguments:
        arguments = ("list",)
    if arguments == ("list",):
        summaries = runtime.list_sessions()
        if not summaries:
            ui.info("没有可恢复的会话。")
            return _handled()
        for summary in summaries:
            ui.info(
                f"{summary.session_id} | {summary.title} | "
                f"{summary.updated_at.isoformat()} | {summary.message_count} 条消息"
            )
        return _handled()
    if len(arguments) == 2 and arguments[0].casefold() == "resume":
        return CommandOutcome(CommandOutcomeKind.RESUME_SESSION, session_id=arguments[1])
    return _invalid(ui, "/session [list|resume <session-id>]")


def _memory(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if len(parsed.arguments) > 1:
        return _invalid(ui, "/memory [user|project|all]")
    scope = parsed.arguments[0].casefold() if parsed.arguments else "all"
    if scope not in ("user", "project", "all"):
        return _invalid(ui, "/memory [user|project|all]")
    notes = runtime.list_memory(scope)
    if not notes:
        ui.info("没有可显示的记忆。")
        return _handled()
    for note in notes:
        ui.info(f"{note.id} | {note.scope.value} | {note.category.value} | {note.updated_at.isoformat()}")
    return _handled()


def _permission(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/permission")
    ui.info(runtime.permission_summary())
    return _handled()


def _status(runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/status")
    ui.info(runtime.status_summary())
    return _handled()


def _review(_runtime: CommandRuntime, parsed: ParsedCommand, ui: UIControl) -> CommandOutcome:
    if parsed.arguments:
        return _invalid(ui, "/review")
    return CommandOutcome(CommandOutcomeKind.AI_INPUT, ai_input=REVIEW_AI_INPUT)
