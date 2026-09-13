from __future__ import annotations

from typing import Any


__all__ = [
    "ConfirmationResult",
    "ConfirmationScope",
    "CliPermissionConfirmer",
    "DenyByDefaultConfirmer",
    "PermissionConfirmer",
    "PermissionDecision",
    "PermissionDecisionValue",
    "PermissionLayer",
    "PermissionManager",
    "PermissionMatch",
    "PermissionMode",
    "PermissionRequest",
    "PermissionRule",
    "PermissionRuleConfigError",
    "PermissionRuleLoadError",
    "PermissionRuleSet",
    "PermissionRulesLoadResult",
    "RiskLevel",
    "SessionPermissionRules",
    "assess_request_risk",
    "build_permission_request",
    "check_built_in_policies",
    "check_hard_denylist",
    "check_sensitive_file_policy",
    "check_workspace_sandbox",
    "decision_from_rule",
    "decision_for_permission_mode",
    "decision_for_risk",
    "find_hard_denylist_match",
    "is_sensitive_path",
    "load_permission_rule_file",
    "load_permission_rules",
    "match_first_rule",
    "parse_permission_rules_document",
]


def __getattr__(name: str) -> Any:
    if name == "PermissionManager":
        from newcode.permissions.manager import PermissionManager

        return PermissionManager

    if name in {"CliPermissionConfirmer", "DenyByDefaultConfirmer", "PermissionConfirmer"}:
        from newcode.permissions import confirmer

        return getattr(confirmer, name)

    if name in {
        "ConfirmationResult",
        "ConfirmationScope",
        "PermissionDecision",
        "PermissionDecisionValue",
        "PermissionLayer",
        "PermissionMatch",
        "PermissionMode",
        "PermissionRequest",
        "PermissionRule",
        "RiskLevel",
    }:
        from newcode.permissions import types

        return getattr(types, name)

    if name in {"check_hard_denylist", "find_hard_denylist_match"}:
        from newcode.permissions import denylist

        return getattr(denylist, name)

    if name == "build_permission_request":
        from newcode.permissions.normalizer import build_permission_request

        return build_permission_request

    if name == "check_workspace_sandbox":
        from newcode.permissions.sandbox import check_workspace_sandbox

        return check_workspace_sandbox

    if name in {"check_sensitive_file_policy", "is_sensitive_path"}:
        from newcode.permissions import sensitive

        return getattr(sensitive, name)

    if name == "check_built_in_policies":
        from newcode.permissions.builtins import check_built_in_policies

        return check_built_in_policies

    if name in {
        "assess_request_risk",
        "decision_for_permission_mode",
        "decision_for_risk",
    }:
        from newcode.permissions import modes

        return getattr(modes, name)

    if name in {
        "PermissionRuleConfigError",
        "PermissionRuleSet",
        "decision_from_rule",
        "match_first_rule",
        "parse_permission_rules_document",
    }:
        from newcode.permissions import rules

        return getattr(rules, name)

    if name in {
        "PermissionRuleLoadError",
        "PermissionRulesLoadResult",
        "load_permission_rule_file",
        "load_permission_rules",
    }:
        from newcode.permissions import loader

        return getattr(loader, name)

    if name == "SessionPermissionRules":
        from newcode.permissions.session import SessionPermissionRules

        return SessionPermissionRules

    raise AttributeError(f"module 'newcode.permissions' has no attribute {name!r}")
