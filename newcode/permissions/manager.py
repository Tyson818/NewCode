from __future__ import annotations

from typing import Any

from newcode.permissions.builtins import check_built_in_policies
from newcode.permissions.confirmer import (
    DenyByDefaultConfirmer,
    PermissionConfirmer,
)
from newcode.permissions.denylist import check_hard_denylist
from newcode.permissions.loader import PermissionRuleLoadError
from newcode.permissions.modes import decision_for_permission_mode
from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.rules import (
    PermissionRuleSet,
    decision_from_rule,
    empty_rule_set,
    match_first_rule,
)
from newcode.permissions.sandbox import check_workspace_sandbox
from newcode.permissions.session import SessionPermissionRules
from newcode.permissions.types import (
    ConfirmationScope,
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    RiskLevel,
)


class PermissionManager:
    def __init__(
        self,
        *,
        mode: PermissionMode = PermissionMode.DEFAULT,
        session_rules: PermissionRuleSet | None = None,
        local_project_rules: PermissionRuleSet | None = None,
        project_rules: PermissionRuleSet | None = None,
        user_global_rules: PermissionRuleSet | None = None,
        rule_load_errors: tuple[PermissionRuleLoadError, ...] = (),
        confirmer: PermissionConfirmer | None = None,
    ) -> None:
        self.mode = mode
        self._session_rules = _coerce_session_rules(session_rules)
        self.local_project_rules = local_project_rules or empty_rule_set(
            PermissionLayer.LOCAL_PROJECT_RULES
        )
        self.project_rules = project_rules or empty_rule_set(PermissionLayer.PROJECT_RULES)
        self.user_global_rules = user_global_rules or empty_rule_set(
            PermissionLayer.USER_GLOBAL_RULES
        )
        self.rule_load_errors = tuple(rule_load_errors)
        self.confirmer = confirmer or DenyByDefaultConfirmer()

    @property
    def session_rules(self) -> PermissionRuleSet:
        return self._session_rules.as_rule_set()

    def check(self, tool_call: Any, context: Any, tool: Any | None = None) -> PermissionDecision:
        request = build_permission_request(tool_call, context, self.mode, tool)

        hard_deny_decision = check_hard_denylist(request)
        if hard_deny_decision is not None:
            return hard_deny_decision

        sandbox_decision = check_workspace_sandbox(request)
        if sandbox_decision is not None:
            return sandbox_decision

        for rule_set in (
            self._session_rules.as_rule_set(),
            self.local_project_rules,
            self.project_rules,
            self.user_global_rules,
        ):
            load_error_decision = self._decision_for_load_error(rule_set.source, request)
            if load_error_decision is not None:
                return load_error_decision

            matched_rule = match_first_rule(rule_set, request)
            if matched_rule is not None:
                return decision_from_rule(matched_rule, request)

        built_in_decision = check_built_in_policies(request)
        if built_in_decision is not None:
            return self.confirm_if_needed(request, built_in_decision)

        mode_decision = decision_for_permission_mode(request)
        return self.confirm_if_needed(request, mode_decision)

    def confirm_if_needed(
        self,
        request: Any,
        decision: PermissionDecision,
    ) -> PermissionDecision:
        if decision.decision is not PermissionDecisionValue.REQUIRE_CONFIRMATION:
            return decision

        result = self.confirmer.confirm(request, decision)
        if result.allowed:
            if result.scope is ConfirmationScope.SESSION:
                self._session_rules.add_allow_for_request(
                    request,
                    reason=result.reason or "Allowed by session confirmation.",
                )
            return PermissionDecision(
                decision=PermissionDecisionValue.ALLOW,
                tool_name=decision.tool_name,
                reason=result.reason or "Allowed by confirmation.",
                risk_level=decision.risk_level,
                matched_rule=decision.matched_rule,
                layer=PermissionLayer.HITL_CONFIRMATION,
                original_args=decision.original_args,
                normalized_args=decision.normalized_args,
            )

        return PermissionDecision(
            decision=PermissionDecisionValue.DENY,
            tool_name=decision.tool_name,
            reason=result.reason or "Denied by confirmation.",
            risk_level=decision.risk_level,
            matched_rule=decision.matched_rule,
            layer=PermissionLayer.HITL_CONFIRMATION,
            original_args=decision.original_args,
            normalized_args=decision.normalized_args,
        )

    def _decision_for_load_error(
        self,
        source: PermissionLayer,
        request: Any,
    ) -> PermissionDecision | None:
        messages = [
            error.message
            for error in self.rule_load_errors
            if error.source is source
        ]
        if not messages:
            return None

        return PermissionDecision(
            decision=PermissionDecisionValue.DENY,
            tool_name=request.tool_name,
            reason="Invalid permission rule config; conservatively denied.",
            risk_level=RiskLevel.HIGH,
            matched_rule=f"{source.value}_load_error",
            layer=source,
            original_args=request.original_args,
            normalized_args={
                **request.normalized_args,
                "rule_load_errors": messages,
            },
        )


def _coerce_session_rules(
    session_rules: PermissionRuleSet | SessionPermissionRules | None,
) -> SessionPermissionRules:
    if session_rules is None:
        return SessionPermissionRules()
    if isinstance(session_rules, SessionPermissionRules):
        return session_rules
    return SessionPermissionRules(rules=list(session_rules.rules))
