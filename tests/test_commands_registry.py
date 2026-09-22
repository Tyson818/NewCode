from __future__ import annotations

import pytest

from newcode.commands.registry import CommandRegistry, CommandRegistryError, normalize_command_name
from newcode.commands.types import CommandCategory, CommandDefinition


def command(name: str, *aliases: str, hidden: bool = False) -> CommandDefinition:
    return CommandDefinition(
        name=name,
        aliases=aliases,
        description="说明",
        usage=f"{name} [参数]",
        category=CommandCategory.LOCAL,
        argument_hint="参数",
        hidden=hidden,
        handler=object(),
    )


def test_metadata_keeps_all_static_fields():
    definition = command("/help", "/h")

    assert definition.name == "/help"
    assert definition.aliases == ("/h",)
    assert definition.description == "说明"
    assert definition.usage == "/help [参数]"
    assert definition.argument_hint == "参数"
    assert definition.handler is not None


def test_name_and_aliases_are_casefolded_after_slash_removal():
    registry = CommandRegistry([command("/Help", "/H")])

    assert normalize_command_name("/HeLP") == "help"
    assert registry.get("/HELP") is registry.get("/h")


@pytest.mark.parametrize(
    "definitions",
    [
        (command("/help"), command("/HELP")),
        (command("/help", "/h"), command("/history", "/H")),
        (command("/help", "/HELP"),),
    ],
)
def test_any_canonical_or_alias_conflict_fails_without_overwrite(definitions):
    with pytest.raises(CommandRegistryError) as error:
        CommandRegistry(definitions)

    assert error.value.code == "command_alias_conflict"


def test_visible_entries_and_completion_candidates_exclude_hidden_and_sort():
    registry = CommandRegistry(
        [command("/zeta"), command("/alpha", "/a"), command("/secret", hidden=True)]
    )

    assert [item.name for item in registry.visible_definitions()] == ["/alpha", "/zeta"]
    assert [item.name for item in registry.completion_candidates("/")] == ["/alpha", "/zeta"]
    assert registry.completion_candidates("/s") == ()


def test_registry_is_sealed_after_static_startup_registration():
    registry = CommandRegistry([command("/help")])

    with pytest.raises(CommandRegistryError) as error:
        registry.register(command("/custom"))

    assert error.value.code == "command_registry_sealed"
