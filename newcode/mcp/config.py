from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from .types import MCPServerConfig, MCPServerError

DEFAULT_USER_MCP_CONFIG_PATH = Path.home() / ".newcode" / "mcp.yaml"
PROJECT_MCP_CONFIG_RELATIVE_PATH = Path(".newcode") / "mcp.yaml"
_SERVER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_VARIABLE_REFERENCE = re.compile(r"\$\{([^{}]+)\}")


@dataclass(frozen=True)
class MCPConfigLoadResult:
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    errors: dict[str, MCPServerError] = field(default_factory=dict)


@dataclass(frozen=True)
class _ConfigError(Exception):
    message: str


def load_mcp_config(
    workspace_root: Path,
    *,
    user_config_path: Path | None = None,
    project_config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> MCPConfigLoadResult:
    root = Path(workspace_root)
    user_path = user_config_path or DEFAULT_USER_MCP_CONFIG_PATH
    project_path = project_config_path or root / PROJECT_MCP_CONFIG_RELATIVE_PATH
    environment = os.environ if environ is None else environ
    errors: dict[str, MCPServerError] = {}
    user_servers = _read_servers(user_path, "user", errors)
    project_servers = _read_servers(project_path, "project", errors)
    servers: dict[str, MCPServerConfig] = {}

    for name, raw in {**user_servers, **project_servers}.items():
        try:
            servers[name] = _parse_server(name, raw, environment)
        except _ConfigError as error:
            errors[name] = MCPServerError("mcp_config_error", error.message)
    return MCPConfigLoadResult(servers, errors)


def _read_servers(path: Path, source: str, errors: dict[str, MCPServerError]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        errors[f"<{source}-config>"] = MCPServerError("mcp_config_error", "Invalid MCP YAML.")
        return {}
    except OSError:
        errors[f"<{source}-config>"] = MCPServerError("mcp_config_error", "MCP configuration could not be read.")
        return {}
    if document is None:
        return {}
    if not isinstance(document, dict) or set(document) - {"mcp_servers"}:
        errors[f"<{source}-config>"] = MCPServerError("mcp_config_error", "Invalid MCP configuration document.")
        return {}
    servers = document.get("mcp_servers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        errors[f"<{source}-config>"] = MCPServerError("mcp_config_error", "mcp_servers must be a mapping.")
        return {}
    return servers


def _parse_server(
    name: str,
    raw: Any,
    environ: Mapping[str, str],
) -> MCPServerConfig:
    if not _SERVER_NAME.fullmatch(name):
        raise _ConfigError("Invalid MCP server name.")
    if not isinstance(raw, dict):
        raise _ConfigError("MCP server configuration must be a mapping.")
    transport = raw.get("transport")
    if transport == "stdio":
        _only(raw, {"transport", "command", "args", "env"})
        config_env, sensitive_values = _expand_string_map(_string_map(raw, "env"), environ)
        final_env = dict(environ)
        final_env.update(config_env)
        return MCPServerConfig(
            name=name,
            transport="stdio",
            command=_required(raw, "command"),
            args=_string_list(raw, "args"),
            env=final_env,
            _sensitive_values=sensitive_values,
        )
    if transport == "streamable_http":
        _only(raw, {"transport", "url", "headers"})
        url = _required(raw, "url")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _ConfigError("Streamable HTTP url must use http or https.")
        headers, sensitive_values = _expand_string_map(_string_map(raw, "headers"), environ)
        return MCPServerConfig(
            name=name,
            transport="streamable_http",
            url=url,
            headers=headers,
            _sensitive_values=sensitive_values,
        )
    raise _ConfigError("MCP transport must be stdio or streamable_http.")


def _only(raw: dict[str, Any], allowed: set[str]) -> None:
    if set(raw) - allowed:
        raise _ConfigError("MCP server configuration has unsupported fields.")


def _required(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise _ConfigError(f"MCP server {key} must be a non-empty string.")
    return value


def _string_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _ConfigError(f"MCP server {key} must be a list of strings.")
    return tuple(value)


def _string_map(raw: dict[str, Any], key: str) -> dict[str, str]:
    value = raw.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise _ConfigError(f"MCP server {key} must be a string mapping.")
    return dict(value)


def _expand_string_map(
    values: dict[str, str],
    environ: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    expanded_values: dict[str, str] = {}
    sensitive_values: list[str] = []
    for key, value in values.items():
        expanded, resolved_values = _expand_variables(value, environ)
        expanded_values[key] = expanded
        sensitive_values.extend(resolved_values)
        if resolved_values:
            sensitive_values.append(expanded)
    return expanded_values, tuple(
        sorted(dict.fromkeys(sensitive_values), key=len, reverse=True)
    )


def _expand_variables(value: str, environ: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    resolved_values: list[str] = []

    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        variable_value = environ.get(variable_name)
        if not variable_name or not variable_value:
            raise _ConfigError("MCP environment variable is missing or empty.")
        resolved_values.append(variable_value)
        return variable_value

    expanded = _VARIABLE_REFERENCE.sub(replace, value)
    if "${" in expanded:
        raise _ConfigError("MCP environment variable placeholder is invalid.")
    return expanded, tuple(resolved_values)
