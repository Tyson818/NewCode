from __future__ import annotations

import pytest

from newcode.subagents.types import (
    AgentPermissionMode,
    AgentValidationError,
    parse_agent_frontmatter,
)


def _frontmatter(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "code-reviewer",
        "description": "Reviews code safely.",
        "tools": {"allow": ["read_file", "mcp__demo__inspect"]},
        "max_iterations": 4,
        "permission_mode": "inherit",
    }
    value.update(overrides)
    return value


def test_definition_frontmatter_is_immutable_and_preserves_allow_deny_semantics():
    name, description, allow, deny, model, iterations, mode = parse_agent_frontmatter(
        _frontmatter(tools={"allow": ["read_file"], "deny": ["read_file"]}, model="model-a")
    )

    assert (name, description) == ("code-reviewer", "Reviews code safely.")
    assert allow == ("read_file",)
    assert deny == ("read_file",)  # 调用策略层时 deny 可优先于 allow。
    assert model == "model-a"
    assert iterations == 4
    assert mode is AgentPermissionMode.INHERIT


def test_tools_allow_must_be_present_but_empty_list_is_valid():
    parsed = parse_agent_frontmatter(_frontmatter(tools={"allow": []}))
    assert parsed[2] == ()
    assert parsed[3] == ()

    with pytest.raises(AgentValidationError, match="subagent_definition_invalid"):
        parse_agent_frontmatter(_frontmatter(tools={"deny": ["write_file"]}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "BadName"},
        {"name": "../escape"},
        {"description": " two spaces "},
        {"description": "two\nlines"},
        {"tools": {"allow": "read_file"}},
        {"tools": {"allow": ["read_file", "read_file"]}},
        {"tools": {"allow": [], "unexpected": []}},
        {"max_iterations": True},
        {"max_iterations": 0},
        {"max_iterations": 9},
        {"permission_mode": "magic"},
        {"unknown": "field"},
    ],
)
def test_invalid_definition_fields_have_stable_safe_error(overrides: dict[str, object]):
    with pytest.raises(AgentValidationError, match="subagent_definition_invalid") as error:
        parse_agent_frontmatter(_frontmatter(**overrides))
    assert str(error.value) == "subagent_definition_invalid"
