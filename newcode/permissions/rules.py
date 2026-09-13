from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMatch,
    PermissionRequest,
    PermissionRule,
    RiskLevel,
)


class PermissionRuleConfigError(Exception):
    pass


@dataclass(frozen=True)
class PermissionRuleSet:
    source: PermissionLayer
    rules: tuple[PermissionRule, ...] = ()


def empty_rule_set(source: PermissionLayer) -> PermissionRuleSet:
    return PermissionRuleSet(source=source, rules=())


def parse_permission_rules_document(
    raw: Any,
    *,
    source: PermissionLayer,
) -> PermissionRuleSet:
    if raw is None:
        return empty_rule_set(source)
    if not isinstance(raw, dict):
        raise PermissionRuleConfigError("权限规则文件顶层必须是 YAML 对象")

    raw_rules = raw.get("rules", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise PermissionRuleConfigError("权限规则 rules 必须是列表")

    rules = tuple(
        _parse_rule(item, index=index, source=source)
        for index, item in enumerate(raw_rules)
    )
    return PermissionRuleSet(source=source, rules=rules)


def match_first_rule(
    rule_set: PermissionRuleSet,
    request: PermissionRequest,
) -> PermissionRule | None:
    for rule in rule_set.rules:
        if rule_matches(rule, request):
            return rule
    return None


def decision_from_rule(
    rule: PermissionRule,
    request: PermissionRequest,
) -> PermissionDecision:
    return PermissionDecision(
        decision=rule.action,
        tool_name=request.tool_name,
        reason=rule.reason,
        risk_level=rule.risk_level,
        matched_rule=rule.id,
        layer=rule.source,
        original_args=request.original_args,
        normalized_args=request.normalized_args,
    )


def rule_matches(rule: PermissionRule, request: PermissionRequest) -> bool:
    if rule.tool != request.tool_name:
        return False

    match = rule.match
    checks = [
        _matches_exact(match.command, _read_text(request, "command")),
        _matches_glob(match.command_glob, _read_text(request, "command")),
        _matches_exact(match.path, _read_text(request, "relative_path")),
        _matches_glob(match.path_glob, _read_text(request, "relative_path")),
        _matches_exact(match.mcp_server, _read_text(request, "mcp_server")),
        _matches_glob(match.mcp_server_glob, _read_text(request, "mcp_server")),
        _matches_exact(match.mcp_tool, _read_text(request, "mcp_tool")),
        _matches_glob(match.mcp_tool_glob, _read_text(request, "mcp_tool")),
    ]
    active_checks = [
        check
        for pattern, check in [
            (match.command, checks[0]),
            (match.command_glob, checks[1]),
            (match.path, checks[2]),
            (match.path_glob, checks[3]),
            (match.mcp_server, checks[4]),
            (match.mcp_server_glob, checks[5]),
            (match.mcp_tool, checks[6]),
            (match.mcp_tool_glob, checks[7]),
        ]
        if pattern is not None
    ]
    return bool(active_checks) and all(active_checks)


def _parse_rule(
    raw: Any,
    *,
    index: int,
    source: PermissionLayer,
) -> PermissionRule:
    if not isinstance(raw, dict):
        raise PermissionRuleConfigError(f"第 {index + 1} 条权限规则必须是 YAML 对象")

    rule_id = _required_string(raw, "id", index)
    tool = _required_string(raw, "tool", index)
    reason = _required_string(raw, "reason", index)
    action = _parse_action(_required_string(raw, "action", index), index)
    risk_level = _parse_risk_level(_required_string(raw, "risk_level", index), index)
    match = _parse_match(raw.get("match"), index)

    return PermissionRule(
        id=rule_id,
        tool=tool,
        match=match,
        action=action,
        reason=reason,
        risk_level=risk_level,
        source=source,
    )


def _parse_match(raw: Any, index: int) -> PermissionMatch:
    if not isinstance(raw, dict):
        raise PermissionRuleConfigError(f"第 {index + 1} 条权限规则 match 必须是 YAML 对象")

    allowed_keys = {"command", "command_glob", "path", "path_glob", "mcp_server", "mcp_server_glob", "mcp_tool", "mcp_tool_glob"}
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        raise PermissionRuleConfigError(
            f"第 {index + 1} 条权限规则 match 包含未知字段: {sorted(unknown_keys)}"
        )

    match = PermissionMatch(
        command=_optional_string(raw, "command", index),
        command_glob=_optional_string(raw, "command_glob", index),
        path=_optional_string(raw, "path", index),
        path_glob=_optional_string(raw, "path_glob", index),
        mcp_server=_optional_string(raw, "mcp_server", index),
        mcp_server_glob=_optional_string(raw, "mcp_server_glob", index),
        mcp_tool=_optional_string(raw, "mcp_tool", index),
        mcp_tool_glob=_optional_string(raw, "mcp_tool_glob", index),
    )
    if all(
        value is None
        for value in (match.command, match.command_glob, match.path, match.path_glob, match.mcp_server, match.mcp_server_glob, match.mcp_tool, match.mcp_tool_glob)
    ):
        raise PermissionRuleConfigError(f"第 {index + 1} 条权限规则 match 至少需要一个条件")
    return match


def _parse_action(value: str, index: int) -> PermissionDecisionValue:
    if value == PermissionDecisionValue.ALLOW.value:
        return PermissionDecisionValue.ALLOW
    if value == PermissionDecisionValue.DENY.value:
        return PermissionDecisionValue.DENY
    raise PermissionRuleConfigError(
        f"第 {index + 1} 条权限规则 action 只支持 allow / deny"
    )


def _parse_risk_level(value: str, index: int) -> RiskLevel:
    try:
        return RiskLevel(value)
    except ValueError as exc:
        raise PermissionRuleConfigError(
            f"第 {index + 1} 条权限规则 risk_level 无效"
        ) from exc


def _required_string(raw: dict[str, Any], name: str, index: int) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise PermissionRuleConfigError(f"第 {index + 1} 条权限规则 {name} 必须是非空字符串")
    return value


def _optional_string(raw: dict[str, Any], name: str, index: int) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PermissionRuleConfigError(f"第 {index + 1} 条权限规则 match.{name} 必须是非空字符串")
    return value


def _read_text(request: PermissionRequest, name: str) -> str | None:
    value = request.normalized_args.get(name)
    return value if isinstance(value, str) else None


def _matches_exact(pattern: str | None, value: str | None) -> bool:
    return pattern is not None and value == pattern


def _matches_glob(pattern: str | None, value: str | None) -> bool:
    return pattern is not None and value is not None and fnmatchcase(value, pattern)
