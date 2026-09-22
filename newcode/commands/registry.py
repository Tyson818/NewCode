"""静态命令注册与可见候选查询。"""

from __future__ import annotations

from collections.abc import Iterable

from .types import CommandDefinition


class CommandRegistryError(Exception):
    """只对外暴露稳定、安全的命令注册错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CommandRegistry:
    """仅接受启动期静态定义，名称和别名全局唯一。"""

    def __init__(self, definitions: Iterable[CommandDefinition] = ()) -> None:
        self._definitions: dict[str, CommandDefinition] = {}
        self._lookup: dict[str, CommandDefinition] = {}
        self._sealed = False
        for definition in definitions:
            self.register(definition)
        self._sealed = True

    def register(self, definition: CommandDefinition) -> None:
        """注册一项定义；任意规范名或别名冲突都失败且不覆盖。"""

        if self._sealed:
            raise CommandRegistryError("command_registry_sealed")
        if not isinstance(definition, CommandDefinition):
            raise CommandRegistryError("command_definition_invalid")
        canonical = normalize_command_name(definition.name)
        names = (canonical, *(normalize_command_name(alias) for alias in definition.aliases))
        if len(set(names)) != len(names) or any(name in self._lookup for name in names):
            raise CommandRegistryError("command_alias_conflict")

        self._definitions[canonical] = definition
        for name in names:
            self._lookup[name] = definition

    def get(self, name: str) -> CommandDefinition | None:
        """按规范名或别名查找；无效名称视为未命中。"""

        try:
            return self._lookup.get(normalize_command_name(name))
        except CommandRegistryError:
            return None

    def visible_definitions(self) -> tuple[CommandDefinition, ...]:
        """返回按规范名排序的非隐藏定义。"""

        return tuple(
            sorted(
                (definition for definition in self._definitions.values() if not definition.hidden),
                key=lambda definition: normalize_command_name(definition.name),
            )
        )

    def completion_candidates(self, prefix: str) -> tuple[CommandDefinition, ...]:
        """按所有公开规范名/别名前缀匹配，并按规范名去重排序。"""

        normalized_prefix = normalize_command_prefix(prefix)
        matches: dict[str, CommandDefinition] = {}
        for name, definition in self._lookup.items():
            if definition.hidden or not name.startswith(normalized_prefix):
                continue
            matches[normalize_command_name(definition.name)] = definition
        return tuple(matches[key] for key in sorted(matches))


def normalize_command_name(value: str) -> str:
    """验证注册名称并产生不含 `/` 的大小写无关键。"""

    if not isinstance(value, str) or not value.startswith("/"):
        raise CommandRegistryError("command_definition_invalid")
    normalized = value[1:].casefold()
    if not normalized or any(character.isspace() for character in normalized):
        raise CommandRegistryError("command_definition_invalid")
    return normalized


def normalize_command_prefix(value: str) -> str:
    """验证补全前缀；只有 slash 命令名可补全。"""

    if not isinstance(value, str) or not value.startswith("/"):
        raise CommandRegistryError("command_completion_invalid")
    return value[1:].casefold()
