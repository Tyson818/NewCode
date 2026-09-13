from __future__ import annotations

from pathlib import Path

import pytest

from newcode.permissions import (
    ConfirmationResult,
    ConfirmationScope,
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionManager,
    PermissionMatch,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
    RiskLevel,
)
from newcode.tools.types import ToolCall, ToolContext


def test_permission_mode_values():
    assert [mode.value for mode in PermissionMode] == [
        "strict",
        "default",
        "permissive",
        "trusted",
    ]


def test_risk_level_values():
    assert [level.value for level in RiskLevel] == ["low", "medium", "high"]


def test_permission_decision_values():
    assert [decision.value for decision in PermissionDecisionValue] == [
        "allow",
        "deny",
        "require_confirmation",
    ]


def test_permission_layer_values():
    assert [layer.value for layer in PermissionLayer] == [
        "hard_denylist",
        "workspace_sandbox",
        "session_rules",
        "local_project_rules",
        "project_rules",
        "user_global_rules",
        "built_in_rules",
        "permission_mode",
        "hitl_confirmation",
    ]


def test_confirmation_scope_does_not_include_permanent():
    assert [scope.value for scope in ConfirmationScope] == ["once", "session"]
    assert "permanent" not in {scope.value for scope in ConfirmationScope}


def test_permission_request_is_readable(tmp_path):
    request = PermissionRequest(
        tool_name="read_file",
        original_args={"path": "README.md"},
        normalized_args={"path": "README.md"},
        workspace_root=tmp_path,
        mode=PermissionMode.DEFAULT,
    )

    assert request.tool_name == "read_file"
    assert request.original_args == {"path": "README.md"}
    assert request.normalized_args == {"path": "README.md"}
    assert request.workspace_root == tmp_path
    assert request.mode is PermissionMode.DEFAULT


def test_permission_decision_is_readable():
    decision = PermissionDecision(
        decision=PermissionDecisionValue.DENY,
        tool_name="run_command",
        reason="blocked",
        risk_level=RiskLevel.HIGH,
        matched_rule="rule_1",
        layer=PermissionLayer.HARD_DENYLIST,
        original_args={"command": "shutdown"},
        normalized_args={"command": "shutdown"},
    )

    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.tool_name == "run_command"
    assert decision.reason == "blocked"
    assert decision.risk_level is RiskLevel.HIGH
    assert decision.matched_rule == "rule_1"
    assert decision.layer is PermissionLayer.HARD_DENYLIST
    assert decision.original_args == {"command": "shutdown"}
    assert decision.normalized_args == {"command": "shutdown"}


def test_confirmation_result_is_readable():
    result = ConfirmationResult(
        allowed=True,
        scope=ConfirmationScope.ONCE,
        reason="approved for this call",
    )

    assert result.allowed is True
    assert result.scope is ConfirmationScope.ONCE
    assert result.reason == "approved for this call"


def test_permission_match_is_readable():
    match = PermissionMatch(
        command="git status",
        command_glob="git status*",
        path=".env",
        path_glob=".env*",
    )

    assert match.command == "git status"
    assert match.command_glob == "git status*"
    assert match.path == ".env"
    assert match.path_glob == ".env*"


def test_permission_rule_accepts_only_allow_or_deny_actions():
    allow_rule = PermissionRule(
        id="allow_git_status",
        tool="run_command",
        match=PermissionMatch(command_glob="git status*"),
        action=PermissionDecisionValue.ALLOW,
        reason="Allow git status",
        risk_level=RiskLevel.LOW,
        source=PermissionLayer.PROJECT_RULES,
    )
    deny_rule = PermissionRule(
        id="deny_env_write",
        tool="write_file",
        match=PermissionMatch(path_glob=".env*"),
        action=PermissionDecisionValue.DENY,
        reason="Protect env files",
        risk_level=RiskLevel.HIGH,
        source=PermissionLayer.PROJECT_RULES,
    )

    assert allow_rule.action is PermissionDecisionValue.ALLOW
    assert deny_rule.action is PermissionDecisionValue.DENY

    with pytest.raises(ValueError):
        PermissionRule(
            id="ask_before_write",
            tool="write_file",
            match=PermissionMatch(path_glob="*.txt"),
            action=PermissionDecisionValue.REQUIRE_CONFIRMATION,
            reason="Not supported for YAML rules in this phase",
            risk_level=RiskLevel.MEDIUM,
            source=PermissionLayer.PROJECT_RULES,
        )


def test_permission_manager_defaults_to_default_mode(tmp_path):
    manager = PermissionManager()
    tool_call = ToolCall(
        id="call_1",
        name="write_file",
        arguments={"path": "README.md", "content": "hello"},
        raw_arguments="{}",
    )
    context = ToolContext(workspace_root=tmp_path)

    decision = manager.check(tool_call, context)

    assert manager.mode is PermissionMode.DEFAULT
    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.tool_name == "write_file"
    assert decision.risk_level is RiskLevel.MEDIUM
    assert decision.matched_rule is None
    assert decision.layer is PermissionLayer.HITL_CONFIRMATION
    assert decision.original_args == {"path": "README.md", "content": "hello"}
    assert decision.normalized_args["workspace_root"] == str(context.workspace_root)
    assert decision.normalized_args["path"] == "README.md"
    assert decision.normalized_args["relative_path"] == "README.md"


def test_permission_manager_confirm_if_needed_denies_by_default(tmp_path):
    manager = PermissionManager(mode=PermissionMode.STRICT)
    request = PermissionRequest(
        tool_name="run_command",
        original_args={"command": "echo hi"},
        normalized_args={},
        workspace_root=Path(tmp_path),
        mode=PermissionMode.STRICT,
    )
    decision = PermissionDecision(
        decision=PermissionDecisionValue.REQUIRE_CONFIRMATION,
        tool_name="run_command",
        reason="skeleton",
        risk_level=RiskLevel.MEDIUM,
        matched_rule=None,
        layer=PermissionLayer.PERMISSION_MODE,
    )

    assert manager.mode is PermissionMode.STRICT
    confirmed = manager.confirm_if_needed(request, decision)

    assert confirmed.decision is PermissionDecisionValue.DENY
    assert confirmed.layer is PermissionLayer.HITL_CONFIRMATION
    assert "No permission confirmer" in confirmed.reason
