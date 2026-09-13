from __future__ import annotations

from pathlib import Path

import pytest

from newcode.mcp import (
    MCPServerConfig,
    MCPServerError,
    MCPServerState,
    MCPServerStatus,
    MCPToolDescriptor,
)


def test_mcp_server_config_hides_sensitive_values_from_repr() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer test-secret"},
        _sensitive_values=("test-secret",),
    )

    assert config.name == "remote"
    assert config.transport == "streamable_http"
    assert config.url == "https://example.test/mcp"
    assert "test-secret" not in repr(config)
    assert "Authorization" not in repr(config)
    assert config.redact("request failed: test-secret") == "request failed: [REDACTED]"


def test_mcp_tool_descriptor_fields_are_readable() -> None:
    descriptor = MCPToolDescriptor(
        server_name="local",
        remote_name="search",
        description="Search local data.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    assert descriptor.server_name == "local"
    assert descriptor.remote_name == "search"
    assert descriptor.input_schema["type"] == "object"


def test_unavailable_status_redacts_error_text_and_keeps_server_scope() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="stdio",
        command="python",
        env={"TOKEN": "test-secret"},
        _sensitive_values=("test-secret",),
    )

    status = MCPServerStatus.unavailable(
        config,
        code="mcp_config_error",
        message="Missing credential test-secret.",
    )

    assert status.server_name == "remote"
    assert status.state is MCPServerState.UNAVAILABLE
    assert status.error == MCPServerError(
        code="mcp_config_error",
        message="Missing credential [REDACTED].",
    )
    assert "test-secret" not in repr(status)
    assert "test-secret" not in str(status.error)


def test_ready_status_is_safe_and_contains_tool_count() -> None:
    status = MCPServerStatus.ready("local", tool_count=2)

    assert status.server_name == "local"
    assert status.state is MCPServerState.READY
    assert status.tool_count == 2
    assert status.error is None

from newcode.mcp.config import load_mcp_config
from newcode.mcp.naming import MCP_SCHEMA_ERROR_CODE, MCPToolSchemaError, validate_input_schema


def _write_yaml(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_mcp_config_missing_or_empty_has_no_servers(tmp_path: Path) -> None:
    user = tmp_path / "user.yaml"
    project = tmp_path / "project.yaml"
    assert load_mcp_config(tmp_path, user_config_path=user, project_config_path=project).servers == {}
    _write_yaml(user, "")
    _write_yaml(project, "mcp_servers: {}")
    assert load_mcp_config(tmp_path, user_config_path=user, project_config_path=project).servers == {}


def test_project_entry_completely_overrides_user_entry(tmp_path: Path) -> None:
    user, project = tmp_path / "user.yaml", tmp_path / "project.yaml"
    _write_yaml(user, "mcp_servers:\n  same: {transport: stdio, command: python, args: [old]}\n  user: {transport: stdio, command: uvx}\n")
    _write_yaml(project, "mcp_servers:\n  same: {transport: streamable_http, url: https://example.test/mcp}\n  project: {transport: stdio, command: python}\n")
    result = load_mcp_config(tmp_path, user_config_path=user, project_config_path=project)
    assert set(result.servers) == {"same", "user", "project"}
    assert result.servers["same"].transport == "streamable_http"
    assert result.servers["same"].command is None
    assert result.servers["same"].args == ()


def test_stdio_and_http_fields_are_validated_without_expansion(tmp_path: Path) -> None:
    project = tmp_path / "project.yaml"
    _write_yaml(project, "mcp_servers:\n  local: {transport: stdio, command: python, args: [server.py], env: {TOKEN: raw-value}}\n  remote: {transport: streamable_http, url: https://example.test/mcp, headers: {Authorization: raw-header}}\n")
    result = load_mcp_config(tmp_path, user_config_path=tmp_path / "none", project_config_path=project)
    assert result.errors == {}
    assert result.servers["local"].args == ("server.py",)
    assert result.servers["local"].env["TOKEN"] == "raw-value"
    assert result.servers["remote"].headers == {"Authorization": "raw-header"}


def test_invalid_server_does_not_block_valid_server(tmp_path: Path) -> None:
    project = tmp_path / "project.yaml"
    _write_yaml(project, "mcp_servers:\n  valid: {transport: stdio, command: python}\n  bad name: {transport: stdio, command: python}\n  bad_transport: {transport: legacy_sse}\n  bad_http: {transport: streamable_http, url: ftp://secret.example/mcp}\n")
    result = load_mcp_config(tmp_path, user_config_path=tmp_path / "none", project_config_path=project)
    assert set(result.servers) == {"valid"}
    assert {"bad name", "bad_transport", "bad_http"} <= set(result.errors)
    assert all("secret.example" not in item.message for item in result.errors.values())


def test_invalid_user_document_does_not_block_project(tmp_path: Path) -> None:
    user, project = tmp_path / "user.yaml", tmp_path / "project.yaml"
    _write_yaml(user, "mcp_servers: [private-token")
    _write_yaml(project, "mcp_servers:\n  valid: {transport: stdio, command: python}\n")
    result = load_mcp_config(tmp_path, user_config_path=user, project_config_path=project)
    assert set(result.servers) == {"valid"}
    assert "private-token" not in result.errors["<user-config>"].message


def test_expands_multiple_variables_and_uses_process_environment_for_stdio(tmp_path: Path) -> None:
    project = tmp_path / "project.yaml"
    _write_yaml(
        project,
        "mcp_servers:\n"
        "  local: {transport: stdio, command: python, env: {TOKEN: '${TOKEN}', LABEL: 'id-${ACCOUNT}-${REGION}'}}\n"
        "  remote: {transport: streamable_http, url: https://example.test/mcp, headers: {Authorization: 'Bearer ${TOKEN}', X-Account: '${ACCOUNT}'}}\n",
    )

    result = load_mcp_config(
        tmp_path,
        user_config_path=tmp_path / "none",
        project_config_path=project,
        environ={"PATH": "inherited-path", "TOKEN": "top-secret", "ACCOUNT": "alice", "REGION": "cn"},
    )

    assert result.errors == {}
    local = result.servers["local"]
    remote = result.servers["remote"]
    assert local.env == {
        "PATH": "inherited-path",
        "TOKEN": "top-secret",
        "ACCOUNT": "alice",
        "REGION": "cn",
        "LABEL": "id-alice-cn",
    }
    assert remote.headers == {"Authorization": "Bearer top-secret", "X-Account": "alice"}
    assert local.redact("top-secret id-alice-cn") == "[REDACTED] [REDACTED]"
    assert remote.redact("Bearer top-secret") == "[REDACTED]"
    assert "top-secret" not in repr(local)
    assert "top-secret" not in repr(remote)


def test_missing_or_empty_variable_marks_only_affected_server_invalid(tmp_path: Path) -> None:
    project = tmp_path / "project.yaml"
    _write_yaml(
        project,
        "mcp_servers:\n"
        "  missing: {transport: stdio, command: python, env: {TOKEN: '${MISSING}'}}\n"
        "  empty: {transport: streamable_http, url: https://example.test/mcp, headers: {Authorization: 'Bearer ${EMPTY}'}}\n"
        "  valid: {transport: stdio, command: python, env: {LABEL: '${PRESENT}'}}\n",
    )

    result = load_mcp_config(
        tmp_path,
        user_config_path=tmp_path / "none",
        project_config_path=project,
        environ={"EMPTY": "", "PRESENT": "available"},
    )

    assert set(result.servers) == {"valid"}
    assert {"missing", "empty"} <= set(result.errors)
    assert all(error.code == "mcp_config_error" for error in result.errors.values())
    assert all("${" not in error.message for error in result.errors.values())
    assert "available" not in repr(result)


def test_schema_validation_has_stable_safe_error_code() -> None:
    with pytest.raises(MCPToolSchemaError) as error:
        validate_input_schema({"properties": {"query": {"default": object()}}})

    assert error.value.code == MCP_SCHEMA_ERROR_CODE
    assert "object at" not in str(error.value)
