from __future__ import annotations

import pytest

from newcode.skills.types import SkillMode, SkillValidationError, parse_frontmatter


def _frontmatter(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "safe-skill",
        "description": "A concise safe skill.",
        "tools": ["read_file"],
        "mode": "shared",
    }
    value.update(overrides)
    return value


def test_valid_shared_frontmatter_is_immutable_and_normalized():
    parsed = parse_frontmatter(_frontmatter(parameters=["branch_name"]))

    assert parsed.name == "safe-skill"
    assert parsed.mode is SkillMode.SHARED
    assert parsed.history_messages is None
    assert [item.name for item in parsed.parameters] == ["branch_name"]


@pytest.mark.parametrize("history", [0, 20])
def test_isolated_history_accepts_closed_boundary(history: int):
    parsed = parse_frontmatter(_frontmatter(mode="isolated", history_messages=history))

    assert parsed.history_messages == history


@pytest.mark.parametrize("history", [-1, 21, True, None])
def test_isolated_history_rejects_invalid_values(history: object):
    with pytest.raises(SkillValidationError, match="skill_frontmatter_invalid"):
        parse_frontmatter(_frontmatter(mode="isolated", history_messages=history))


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"name": "BadName"}, "skill_name_invalid"),
        ({"description": "two\nlines"}, "skill_frontmatter_invalid"),
        ({"tools": []}, "skill_frontmatter_invalid"),
        ({"tools": ["read_file", "read_file"]}, "skill_frontmatter_invalid"),
        ({"mode": "unknown"}, "skill_frontmatter_invalid"),
        ({"history_messages": 1}, "skill_frontmatter_invalid"),
        ({"parameters": ["bad-name"]}, "skill_frontmatter_invalid"),
        ({"unexpected": "field"}, "skill_frontmatter_invalid"),
    ],
)
def test_frontmatter_rejects_schema_drift(overrides: dict[str, object], code: str):
    with pytest.raises(SkillValidationError, match=code):
        parse_frontmatter(_frontmatter(**overrides))
