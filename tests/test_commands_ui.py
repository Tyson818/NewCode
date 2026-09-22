from __future__ import annotations

from newcode.commands.types import CommandCategory, CommandDefinition
from newcode.commands.ui import FakeUIControl, UIControl


def test_fake_ui_implements_rendering_independent_protocol():
    ui = FakeUIControl()
    definition = CommandDefinition("/help", description="帮助", usage="/help", category=CommandCategory.LOCAL)

    assert isinstance(ui, UIControl)
    ui.info("安全信息")
    ui.error("command_unknown", "未知命令")
    ui.show_help((definition,))
    ui.show_completion_menu((definition,))
    ui.set_mode("plan")

    assert ui.infos == ["安全信息"]
    assert ui.errors == [("command_unknown", "未知命令")]
    assert ui.help_entries == [(definition,)]
    assert ui.completion_menus == [(definition,)]
    assert ui.modes == ["plan"]


def test_fake_ui_has_no_terminal_output_dependency(capsys):
    ui = FakeUIControl()

    ui.info("不会直接打印")
    ui.error("safe", "不会直接打印")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
