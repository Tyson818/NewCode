from __future__ import annotations

import os
from pathlib import Path

import pytest

from newcode.permissions.manager import PermissionManager
from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    PermissionRequest,
)
from newcode.tools.types import ToolCall, ToolContext


def tool_call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id="call_1",
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )


def test_normalizer_extracts_run_command_command(tmp_path):
    request = build_permission_request(
        tool_call("run_command", {"command": "git status"}),
        ToolContext(workspace_root=tmp_path),
        PermissionMode.DEFAULT,
    )

    assert request.normalized_args["command"] == "git status"


def test_normalizer_extracts_file_path_fields(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolContext(workspace_root=workspace)

    request = build_permission_request(
        tool_call("read_file", {"path": "src/app.py"}),
        context,
        PermissionMode.DEFAULT,
    )

    assert request.normalized_args["path"] == "src/app.py"
    assert request.normalized_args["resolved_path"] == str(workspace / "src" / "app.py")
    assert request.normalized_args["relative_path"] == "src/app.py"


def test_normalizer_extracts_find_files_pattern_and_search_query(tmp_path):
    context = ToolContext(workspace_root=tmp_path)

    find_request = build_permission_request(
        tool_call("find_files", {"pattern": "*.py"}),
        context,
        PermissionMode.DEFAULT,
    )
    search_request = build_permission_request(
        tool_call("search_code", {"query": "PermissionManager", "pattern": "*.py"}),
        context,
        PermissionMode.DEFAULT,
    )

    assert find_request.normalized_args["pattern"] == "*.py"
    assert "path" not in find_request.normalized_args
    assert "root" not in find_request.normalized_args
    assert search_request.normalized_args["query"] == "PermissionManager"
    assert "path" not in search_request.normalized_args
    assert "root" not in search_request.normalized_args


def test_workspace_inside_path_reaches_permission_mode_decision(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decision = PermissionManager(mode=PermissionMode.PERMISSIVE).check(
        tool_call("write_file", {"path": "notes/todo.txt", "content": "hello"}),
        ToolContext(workspace_root=workspace),
    )

    assert decision.decision is PermissionDecisionValue.ALLOW
    assert decision.layer is PermissionLayer.PERMISSION_MODE
    assert decision.normalized_args["relative_path"] == "notes/todo.txt"


def test_workspace_sandbox_rejects_parent_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decision = PermissionManager(mode=PermissionMode.TRUSTED).check(
        tool_call("read_file", {"path": "../outside.txt"}),
        ToolContext(workspace_root=workspace),
    )

    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.layer is PermissionLayer.WORKSPACE_SANDBOX


def test_workspace_sandbox_rejects_absolute_path_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    decision = PermissionManager(mode=PermissionMode.TRUSTED).check(
        tool_call("read_file", {"path": str(outside)}),
        ToolContext(workspace_root=workspace),
    )

    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.layer is PermissionLayer.WORKSPACE_SANDBOX


def test_workspace_sandbox_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    link = workspace / "outside_link.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境不支持创建符号链接: {exc}")

    decision = PermissionManager(mode=PermissionMode.TRUSTED).check(
        tool_call("read_file", {"path": "outside_link.txt"}),
        ToolContext(workspace_root=workspace),
    )

    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.layer is PermissionLayer.WORKSPACE_SANDBOX
    assert Path(decision.normalized_args["resolved_path"]) == outside.resolve()


def test_workspace_sandbox_rejects_symlink_like_resolved_escape(tmp_path):
    from newcode.permissions.sandbox import check_workspace_sandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("secret", encoding="utf-8")
    request = PermissionRequest(
        tool_name="read_file",
        original_args={"path": "outside_link.txt"},
        normalized_args={
            "path": "outside_link.txt",
            "resolved_path": str(outside.resolve()),
        },
        workspace_root=workspace,
        mode=PermissionMode.TRUSTED,
    )

    decision = check_workspace_sandbox(request)

    assert decision is not None
    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.layer is PermissionLayer.WORKSPACE_SANDBOX


def test_workspace_sandbox_is_not_affected_by_permission_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for mode in PermissionMode:
        decision = PermissionManager(mode=mode).check(
            tool_call("replace_in_file", {"path": "../outside.txt"}),
            ToolContext(workspace_root=workspace),
        )

        assert decision.decision is PermissionDecisionValue.DENY
        assert decision.layer is PermissionLayer.WORKSPACE_SANDBOX
