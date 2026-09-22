from __future__ import annotations

from newcode.commands.dispatcher import CommandDispatcher, UNKNOWN_COMMAND_MESSAGE
from newcode.commands.registry import CommandRegistry
from newcode.commands.types import CommandCategory, CommandDefinition, CommandParseKind
from newcode.commands.ui import FakeUIControl


def command(name: str, *aliases: str, hidden: bool = False) -> CommandDefinition:
    return CommandDefinition(name, aliases, "说明", name, CommandCategory.LOCAL, hidden=hidden, handler=object())


def dispatcher() -> CommandDispatcher:
    return CommandDispatcher(CommandRegistry([command("/help", "/h"), command("/hello"), command("/hidden", hidden=True)]))


def test_empty_and_plain_text_return_early_without_ui_or_handler_execution():
    ui = FakeUIControl()
    subject = dispatcher()

    assert subject.dispatch_input("  \t", ui).kind is CommandParseKind.EMPTY
    assert subject.dispatch_input("请分析代码", ui).kind is CommandParseKind.TEXT
    assert ui.errors == []


def test_registered_slash_command_is_case_insensitive_and_not_executed():
    ui = FakeUIControl()
    result = dispatcher().dispatch_input(" /HeLp\talpha beta ", ui)

    assert result.kind is CommandParseKind.COMMAND
    assert result.command is not None
    assert result.command.definition.name == "/help"
    assert result.command.arguments == ("alpha", "beta")
    assert result.command.argument_text == "alpha beta"
    assert ui.errors == []


def test_unknown_command_only_emits_help_guidance_and_never_becomes_text():
    ui = FakeUIControl()
    result = dispatcher().dispatch_input("/missing secret-value", ui)

    assert result.kind is CommandParseKind.UNKNOWN
    assert result.unknown_name == "/missing"
    assert ui.errors == [("command_unknown", UNKNOWN_COMMAND_MESSAGE)]
    assert "secret-value" not in ui.errors[0][1]


def test_tab_completion_replaces_unique_visible_match_without_ui_side_effect():
    ui = FakeUIControl()
    result = dispatcher().complete("/HELLo", ui)

    assert result.replacement == "/hello"
    assert [item.name for item in result.candidates] == ["/hello"]
    assert ui.completion_menus == []


def test_tab_completion_shows_sorted_menu_for_multiple_matches_and_hides_hidden():
    ui = FakeUIControl()
    result = dispatcher().complete("/h", ui)

    assert result.replacement is None
    assert [item.name for item in result.candidates] == ["/hello", "/help"]
    assert [[item.name for item in entries] for entries in ui.completion_menus] == [["/hello", "/help"]]


def test_tab_completion_with_arguments_or_no_match_has_no_side_effect():
    ui = FakeUIControl()

    assert dispatcher().complete("/help topic", ui).replacement is None
    assert dispatcher().complete("/nothing", ui).candidates == ()
    assert dispatcher().complete("plain text", ui).candidates == ()
    assert ui.completion_menus == []


def test_completion_never_executes_handler():
    calls = []

    def forbidden(*_args):
        calls.append("called")
        raise AssertionError("补全不得执行 handler")

    registry = CommandRegistry(
        [CommandDefinition("/help", description="说明", usage="/help", handler=forbidden)]
    )
    ui = FakeUIControl()

    assert CommandDispatcher(registry).complete("/hel", ui).replacement == "/help"
    assert calls == []
