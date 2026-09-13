from __future__ import annotations

from dataclasses import dataclass, field

from newcode.permissions.rules import PermissionRuleSet
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMatch,
    PermissionRequest,
    PermissionRule,
    RiskLevel,
)


@dataclass
class SessionPermissionRules:
    rules: list[PermissionRule] = field(default_factory=list)

    def add_rule(self, rule: PermissionRule) -> None:
        if rule.source is not PermissionLayer.SESSION_RULES:
            raise ValueError("session rule 的 source 必须是 session_rules")
        self.rules.append(rule)

    def add_allow_for_request(
        self,
        request: PermissionRequest,
        *,
        reason: str = "本会话允许同类操作",
    ) -> PermissionRule:
        rule = PermissionRule(
            id=f"session_allow_{len(self.rules) + 1}",
            tool=request.tool_name,
            match=_match_for_request(request),
            action=PermissionDecisionValue.ALLOW,
            reason=reason,
            risk_level=RiskLevel.MEDIUM,
            source=PermissionLayer.SESSION_RULES,
        )
        self.add_rule(rule)
        return rule

    def as_rule_set(self) -> PermissionRuleSet:
        return PermissionRuleSet(
            source=PermissionLayer.SESSION_RULES,
            rules=tuple(self.rules),
        )


def _match_for_request(request: PermissionRequest) -> PermissionMatch:
    mcp_server = request.normalized_args.get("mcp_server")
    mcp_tool = request.normalized_args.get("mcp_tool")
    if isinstance(mcp_server, str) and isinstance(mcp_tool, str):
        return PermissionMatch(mcp_server=mcp_server, mcp_tool=mcp_tool)
    command = request.normalized_args.get("command")
    if isinstance(command, str):
        return PermissionMatch(command=command)

    relative_path = request.normalized_args.get("relative_path")
    if isinstance(relative_path, str):
        return PermissionMatch(path=relative_path)

    raise ValueError("当前请求无法生成 session allow rule")
