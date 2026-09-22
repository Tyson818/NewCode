"""NewCode 静态命令注册、解析与 UI 边界。"""

from .dispatcher import CommandDispatcher
from .registry import CommandRegistry, CommandRegistryError
from .types import CommandCategory, CommandDefinition, CommandOutcome, CommandOutcomeKind, CommandParseKind
from .ui import CLIUIControl, FakeUIControl, UIControl

__all__ = [
    "CommandCategory",
    "CommandDefinition",
    "CommandDispatcher",
    "CommandOutcome",
    "CommandOutcomeKind",
    "CommandParseKind",
    "CommandRegistry",
    "CommandRegistryError",
    "CLIUIControl",
    "FakeUIControl",
    "UIControl",
]
