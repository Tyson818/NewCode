from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


MCP_NAME_ERROR_CODE = "mcp_name_collision"
MCP_SCHEMA_ERROR_CODE = "mcp_tool_schema_invalid"
MAX_MCP_TOOL_NAME_LENGTH = 64
MCP_IDENTITY_DIGEST_LENGTH = 12
MCP_SLUG_LENGTH = 21
_VALID_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSAFE_SLUG_CHARACTERS = re.compile(r"[^a-z0-9_-]+")


class MCPNameError(ValueError):
    code = MCP_NAME_ERROR_CODE


class MCPToolSchemaError(ValueError):
    code = MCP_SCHEMA_ERROR_CODE


def build_mcp_tool_name(server_key: str, remote_tool_name: str) -> str:
    _validate_identity(server_key)
    _validate_identity(remote_tool_name)
    identity = f"{server_key}\0{remote_tool_name}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:MCP_IDENTITY_DIGEST_LENGTH]
    name = "mcp__{}__{}__{}".format(
        _slugify(server_key, fallback="server"),
        _slugify(remote_tool_name, fallback="tool"),
        digest,
    )
    validate_mcp_tool_name(name)
    return name


def validate_mcp_tool_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > MAX_MCP_TOOL_NAME_LENGTH
        or not _VALID_TOOL_NAME.fullmatch(name)
    ):
        raise MCPNameError("Invalid MCP tool name.")


def validate_input_schema(schema: Any) -> None:
    if not isinstance(schema, dict) or not _is_json_transferable(schema):
        raise MCPToolSchemaError("Invalid MCP tool input schema.")
    if "type" in schema and schema["type"] != "object":
        raise MCPToolSchemaError("Invalid MCP tool input schema.")
    for key in ("properties", "required", "additionalProperties"):
        if key in schema and not _is_json_transferable(schema[key]):
            raise MCPToolSchemaError("Invalid MCP tool input schema.")


def _validate_identity(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise MCPNameError("Invalid MCP tool identity.")


def _slugify(value: str, *, fallback: str) -> str:
    slug = _UNSAFE_SLUG_CHARACTERS.sub("-", value.lower()).strip("-_")
    return (slug[:MCP_SLUG_LENGTH].rstrip("-_")) or fallback


def _is_json_transferable(value: Any) -> bool:
    if not _has_only_string_mapping_keys(value):
        return False
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _has_only_string_mapping_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _has_only_string_mapping_keys(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_has_only_string_mapping_keys(item) for item in value)
    return not isinstance(value, float) or math.isfinite(value)
