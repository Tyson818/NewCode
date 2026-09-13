from __future__ import annotations

import pytest

from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.sensitive import (
    check_sensitive_file_policy,
    is_sensitive_path,
)
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
)
from newcode.tools.types import ToolCall, ToolContext


def tool_call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments, raw_arguments="{}")


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "certs/server.pem",
        "certs/server.key",
        ".ssh/id_rsa",
        ".ssh/id_ed25519",
        "config/api_token.txt",
        "config/secret_config.json",
        "config/db_credentials.json",
    ],
)
def test_sensitive_path_patterns_are_recognized(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/app.py",
        "docs/environment.md",
        "notes/keynote.txt",
    ],
)
def test_normal_paths_are_not_sensitive(path):
    assert not is_sensitive_path(path)


@pytest.mark.parametrize("tool_name", ["read_file", "write_file", "replace_in_file"])
def test_sensitive_file_policy_requires_confirmation_for_file_tools(tmp_path, tool_name):
    args: dict[str, object] = {"path": ".env"}
    if tool_name == "write_file":
        args["content"] = "TOKEN=x"
    if tool_name == "replace_in_file":
        args.update({"old": "a", "new": "b"})

    request = build_permission_request(
        tool_call(tool_name, args),
        ToolContext(workspace_root=tmp_path),
        PermissionMode.DEFAULT,
    )

    decision = check_sensitive_file_policy(request)

    assert decision is not None
    assert decision.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION
    assert decision.layer is PermissionLayer.BUILT_IN_RULES
    assert decision.matched_rule == "built_in_sensitive_file"


def test_sensitive_file_policy_ignores_normal_files(tmp_path):
    request = build_permission_request(
        tool_call("read_file", {"path": "README.md"}),
        ToolContext(workspace_root=tmp_path),
        PermissionMode.DEFAULT,
    )

    assert check_sensitive_file_policy(request) is None


def test_sensitive_file_policy_ignores_non_file_tools(tmp_path):
    request = build_permission_request(
        tool_call("run_command", {"command": "echo .env"}),
        ToolContext(workspace_root=tmp_path),
        PermissionMode.DEFAULT,
    )

    assert check_sensitive_file_policy(request) is None
