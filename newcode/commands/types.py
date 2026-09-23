"""命令注册中心使用的纯数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class CommandCategory(str, Enum):
    """命令允许执行的交互类别。"""

    LOCAL = "local"
    UI_STATE = "ui_state"
    AI_PRESET = "ai_preset"


class CommandParseKind(str, Enum):
    """输入在执行任何 handler 前的分类。"""

    EMPTY = "empty"
    TEXT = "text"
    COMMAND = "command"
    UNKNOWN = "unknown"


class CommandOutcomeKind(str, Enum):
    """handler 允许返回的声明式结果。"""

    HANDLED = "handled"
    AI_INPUT = "ai_input"
    MODE_CHANGE = "mode_change"
    CLEAR_SESSION = "clear_session"
    RESUME_SESSION = "resume_session"
    SKILL_REQUEST = "skill_request"


@dataclass(frozen=True)
class CommandDefinition:
    """程序内静态声明的命令元数据；handler 在本 Phase 不会被调用。"""

    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    usage: str = ""
    category: CommandCategory = CommandCategory.LOCAL
    argument_hint: str = ""
    hidden: bool = False
    handler: Callable[..., "CommandOutcome"] | None = None


@dataclass(frozen=True)
class ParsedCommand:
    """已匹配命令的无副作用解析结果。"""

    definition: CommandDefinition
    invoked_name: str
    arguments: tuple[str, ...]
    argument_text: str


@dataclass(frozen=True)
class CommandParseResult:
    """分派前解析结果；未知命令不会携带可执行对象。"""

    kind: CommandParseKind
    raw_input: str
    command: ParsedCommand | None = None
    unknown_name: str | None = None


@dataclass(frozen=True)
class CompletionResult:
    """Tab 补全结果；唯一匹配仅提供替换文本。"""

    replacement: str | None = None
    candidates: tuple[CommandDefinition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CommandOutcome:
    """handler 结果；不携带 Provider、工具调用或文件句柄。"""

    kind: CommandOutcomeKind
    ai_input: str | None = None
    mode: str | None = None
    session_id: str | None = None
    skill_name: str | None = None
    skill_parameters: tuple[tuple[str, str], ...] = ()
