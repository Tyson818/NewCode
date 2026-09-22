"""渲染无关的命令 UI control interface。"""

from __future__ import annotations

from dataclasses import dataclass, field
import sys
from typing import TextIO
from typing import Protocol, runtime_checkable

from .types import CommandDefinition


@runtime_checkable
class UIControl(Protocol):
    """命令 handler 唯一可使用的交互呈现边界。"""

    def info(self, text: str) -> None: ...

    def error(self, code: str, text: str) -> None: ...

    def show_help(self, entries: tuple[CommandDefinition, ...]) -> None: ...

    def show_completion_menu(self, entries: tuple[CommandDefinition, ...]) -> None: ...

    def set_mode(self, mode: str) -> None: ...


@dataclass
class FakeUIControl:
    """测试使用的收集型 UI，不绑定终端或渲染框架。"""

    infos: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    help_entries: list[tuple[CommandDefinition, ...]] = field(default_factory=list)
    completion_menus: list[tuple[CommandDefinition, ...]] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)

    def info(self, text: str) -> None:
        self.infos.append(text)

    def error(self, code: str, text: str) -> None:
        self.errors.append((code, text))

    def show_help(self, entries: tuple[CommandDefinition, ...]) -> None:
        self.help_entries.append(entries)

    def show_completion_menu(self, entries: tuple[CommandDefinition, ...]) -> None:
        self.completion_menus.append(entries)

    def set_mode(self, mode: str) -> None:
        self.modes.append(mode)


class CLIUIControl:
    """把 UIControl 映射到现有 CLI 输出流的薄适配器。"""

    def __init__(self, output: TextIO = sys.stdout, error_output: TextIO = sys.stderr) -> None:
        self.output = output
        self.error_output = error_output

    def info(self, text: str) -> None:
        print(text, file=self.output)

    def error(self, code: str, text: str) -> None:
        del code
        print(text, file=self.output)

    def show_help(self, entries: tuple[CommandDefinition, ...]) -> None:
        for entry in entries:
            print(f"{entry.usage} — {entry.description}", file=self.output)

    def show_completion_menu(self, entries: tuple[CommandDefinition, ...]) -> None:
        print("可用命令：" + "、".join(entry.name for entry in entries), file=self.output)

    def set_mode(self, mode: str) -> None:
        label = "Plan Mode" if mode == "plan" else "Do Mode"
        print(f"已切换到 {label}。", file=self.output)
