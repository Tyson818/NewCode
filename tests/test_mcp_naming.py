from __future__ import annotations

import pytest

from newcode.mcp.naming import (
    MCP_IDENTITY_DIGEST_LENGTH,
    MCP_NAME_ERROR_CODE,
    MCP_SCHEMA_ERROR_CODE,
    MAX_MCP_TOOL_NAME_LENGTH,
    MCPNameError,
    MCPToolSchemaError,
    build_mcp_tool_name,
    validate_input_schema,
    validate_mcp_tool_name,
)


def test_mcp_tool_name_is_stable_and_distinguishes_same_remote_name() -> None:
    github_name = build_mcp_tool_name("github", "search_issues")

    assert github_name == build_mcp_tool_name("github", "search_issues")
    assert github_name != build_mcp_tool_name("gitlab", "search_issues")
    assert github_name.startswith("mcp__github__search_issues__")


def test_slug_collisions_are_distinguished_by_identity_digest() -> None:
    first = build_mcp_tool_name("server", "same name")
    second = build_mcp_tool_name("server", "same-name")

    assert first.rsplit("__", 1)[0] == second.rsplit("__", 1)[0]
    assert first != second


def test_tool_name_slugs_are_ascii_and_digest_is_fixed_without_truncation() -> None:
    name = build_mcp_tool_name("S" * 100, "工具" * 100)
    digest = name.rsplit("__", 1)[1]

    assert len(name) <= MAX_MCP_TOOL_NAME_LENGTH
    assert name.isascii()
    assert len(digest) == MCP_IDENTITY_DIGEST_LENGTH
    assert all(character in "0123456789abcdef" for character in digest)


def test_tool_name_validation_enforces_character_set_and_64_character_boundary() -> None:
    validate_mcp_tool_name("a" * MAX_MCP_TOOL_NAME_LENGTH)

    with pytest.raises(MCPNameError) as invalid_characters:
        validate_mcp_tool_name("mcp.invalid")
    with pytest.raises(MCPNameError) as too_long:
        validate_mcp_tool_name("a" * (MAX_MCP_TOOL_NAME_LENGTH + 1))

    assert invalid_characters.value.code == MCP_NAME_ERROR_CODE
    assert too_long.value.code == MCP_NAME_ERROR_CODE


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object"},
        {"properties": {"query": {"type": "string"}}},
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ],
)
def test_input_schema_accepts_json_transferable_object_shapes(schema: dict[str, object]) -> None:
    validate_input_schema(schema)


@pytest.mark.parametrize(
    "schema",
    [
        [],
        {"type": "array"},
        {"type": None},
        {"properties": {"query": {"examples": {"not-json"}}}},
        {"required": {"query"}},
        {"additionalProperties": float("nan")},
        {1: "non-string JSON object key"},
    ],
)
def test_input_schema_rejects_non_object_or_non_json_values(schema: object) -> None:
    with pytest.raises(MCPToolSchemaError) as error:
        validate_input_schema(schema)

    assert error.value.code == MCP_SCHEMA_ERROR_CODE
