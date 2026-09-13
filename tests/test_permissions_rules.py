from __future__ import annotations

import pytest

from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.rules import (
    PermissionRuleConfigError,
    decision_from_rule,
    match_first_rule,
    parse_permission_rules_document,
    rule_matches,
)
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    RiskLevel,
)
from newcode.tools.types import ToolCall, ToolContext


def tool_call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments, raw_arguments="{}")


def request_for(name: str, arguments: dict[str, object], tmp_path):
    return build_permission_request(
        tool_call(name, arguments),
        ToolContext(workspace_root=tmp_path),
        PermissionMode.DEFAULT,
    )


def test_parse_yaml_like_rules_supports_allow_and_deny():
    rule_set = parse_permission_rules_document(
        {
            "rules": [
                {
                    "id": "allow_git_status",
                    "tool": "run_command",
                    "match": {"command_glob": "git status*"},
                    "action": "allow",
                    "reason": "Allow safe git status commands",
                    "risk_level": "low",
                },
                {
                    "id": "deny_env_write",
                    "tool": "write_file",
                    "match": {"path_glob": ".env*"},
                    "action": "deny",
                    "reason": "Protect secret files",
                    "risk_level": "high",
                },
            ]
        },
        source=PermissionLayer.PROJECT_RULES,
    )

    assert [rule.id for rule in rule_set.rules] == [
        "allow_git_status",
        "deny_env_write",
    ]
    assert rule_set.rules[0].action is PermissionDecisionValue.ALLOW
    assert rule_set.rules[1].action is PermissionDecisionValue.DENY
    assert rule_set.rules[0].risk_level is RiskLevel.LOW
    assert rule_set.rules[1].risk_level is RiskLevel.HIGH


def test_require_confirmation_action_is_invalid():
    with pytest.raises(PermissionRuleConfigError):
        parse_permission_rules_document(
            {
                "rules": [
                    {
                        "id": "ask",
                        "tool": "write_file",
                        "match": {"path_glob": "*.txt"},
                        "action": "require_confirmation",
                        "reason": "not supported",
                        "risk_level": "medium",
                    }
                ]
            },
            source=PermissionLayer.PROJECT_RULES,
        )


def test_exact_and_glob_matching_for_commands(tmp_path):
    rule_set = parse_permission_rules_document(
        {
            "rules": [
                {
                    "id": "allow_exact",
                    "tool": "run_command",
                    "match": {"command": "git status"},
                    "action": "allow",
                    "reason": "exact",
                    "risk_level": "low",
                },
                {
                    "id": "allow_glob",
                    "tool": "run_command",
                    "match": {"command_glob": "git log*"},
                    "action": "allow",
                    "reason": "glob",
                    "risk_level": "low",
                },
            ]
        },
        source=PermissionLayer.PROJECT_RULES,
    )

    assert rule_matches(
        rule_set.rules[0],
        request_for("run_command", {"command": "git status"}, tmp_path),
    )
    assert rule_matches(
        rule_set.rules[1],
        request_for("run_command", {"command": "git log --oneline"}, tmp_path),
    )
    assert not rule_matches(
        rule_set.rules[0],
        request_for("run_command", {"command": "git status --short"}, tmp_path),
    )


def test_exact_and_glob_matching_for_paths(tmp_path):
    rule_set = parse_permission_rules_document(
        {
            "rules": [
                {
                    "id": "deny_exact",
                    "tool": "write_file",
                    "match": {"path": ".env"},
                    "action": "deny",
                    "reason": "exact",
                    "risk_level": "high",
                },
                {
                    "id": "deny_glob",
                    "tool": "write_file",
                    "match": {"path_glob": "secrets/*.key"},
                    "action": "deny",
                    "reason": "glob",
                    "risk_level": "high",
                },
            ]
        },
        source=PermissionLayer.PROJECT_RULES,
    )

    assert rule_matches(
        rule_set.rules[0],
        request_for("write_file", {"path": ".env"}, tmp_path),
    )
    assert rule_matches(
        rule_set.rules[1],
        request_for("write_file", {"path": "secrets/prod.key"}, tmp_path),
    )
    assert not rule_matches(
        rule_set.rules[1],
        request_for("write_file", {"path": "notes/prod.key"}, tmp_path),
    )


def test_match_first_rule_uses_yaml_order(tmp_path):
    rule_set = parse_permission_rules_document(
        {
            "rules": [
                {
                    "id": "first",
                    "tool": "run_command",
                    "match": {"command_glob": "git *"},
                    "action": "deny",
                    "reason": "first wins",
                    "risk_level": "high",
                },
                {
                    "id": "second",
                    "tool": "run_command",
                    "match": {"command": "git status"},
                    "action": "allow",
                    "reason": "second loses",
                    "risk_level": "low",
                },
            ]
        },
        source=PermissionLayer.PROJECT_RULES,
    )

    rule = match_first_rule(
        rule_set,
        request_for("run_command", {"command": "git status"}, tmp_path),
    )

    assert rule is not None
    assert rule.id == "first"


def test_decision_from_rule_preserves_rule_metadata(tmp_path):
    rule_set = parse_permission_rules_document(
        {
            "rules": [
                {
                    "id": "deny_env",
                    "tool": "write_file",
                    "match": {"path": ".env"},
                    "action": "deny",
                    "reason": "Protect env",
                    "risk_level": "high",
                }
            ]
        },
        source=PermissionLayer.PROJECT_RULES,
    )
    request = request_for("write_file", {"path": ".env"}, tmp_path)

    decision = decision_from_rule(rule_set.rules[0], request)

    assert decision.decision is PermissionDecisionValue.DENY
    assert decision.matched_rule == "deny_env"
    assert decision.layer is PermissionLayer.PROJECT_RULES
    assert decision.reason == "Protect env"


def test_rules_require_non_empty_match():
    with pytest.raises(PermissionRuleConfigError):
        parse_permission_rules_document(
            {
                "rules": [
                    {
                        "id": "empty_match",
                        "tool": "run_command",
                        "match": {},
                        "action": "allow",
                        "reason": "invalid",
                        "risk_level": "low",
                    }
                ]
            },
            source=PermissionLayer.PROJECT_RULES,
        )
