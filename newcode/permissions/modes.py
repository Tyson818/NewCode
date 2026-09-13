from __future__ import annotations

import re

from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    PermissionRequest,
    RiskLevel,
)


SAFE_QUERY_COMMANDS = (
    re.compile(r"^\s*git\s+(?:status|log|diff|show|branch)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^\s*(?:dir|pwd|echo)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^\s*get-childitem(?:\s|$)", re.IGNORECASE),
)

HIGH_RISK_COMMANDS = (
    re.compile(r"\bsudo\b", re.IGNORECASE),
    re.compile(r"\brm\b(?=[^|;&\n\r]*-[^\s]*r)(?=[^|;&\n\r]*-[^\s]*f)", re.IGNORECASE),
    re.compile(r"\bdel\b(?=[^|;&\n\r]*\s/s\b)", re.IGNORECASE),
    re.compile(r"\brmdir\b(?=[^|;&\n\r]*\s/s\b)", re.IGNORECASE),
    re.compile(r"\bremove-item\b(?=[^|;&\n\r]*-(?:r|recurse)\b)", re.IGNORECASE),
    re.compile(r"\b(?:format|shutdown|mkfs)\b", re.IGNORECASE),
    re.compile(r"\breg(?:\.exe)?\s+delete\b", re.IGNORECASE),
)


def assess_request_risk(request: PermissionRequest) -> RiskLevel:
    if request.tool_name in {"find_files", "search_code", "read_file"}:
        return RiskLevel.LOW
    if request.tool_name in {"write_file", "replace_in_file"}:
        return RiskLevel.MEDIUM
    if request.tool_name == "run_command":
        return _assess_command_risk(request)
    return RiskLevel.MEDIUM


def decision_for_permission_mode(request: PermissionRequest) -> PermissionDecision:
    risk_level = assess_request_risk(request)
    return decision_for_risk(request.mode, risk_level, request)


def decision_for_risk(
    mode: PermissionMode,
    risk_level: RiskLevel,
    request: PermissionRequest,
) -> PermissionDecision:
    return PermissionDecision(
        decision=_decision_value_for_mode(mode, risk_level),
        tool_name=request.tool_name,
        reason=f"Permission mode {mode.value} default for {risk_level.value} risk.",
        risk_level=risk_level,
        matched_rule=None,
        layer=PermissionLayer.PERMISSION_MODE,
        original_args=request.original_args,
        normalized_args=request.normalized_args,
    )


def _assess_command_risk(request: PermissionRequest) -> RiskLevel:
    command = request.normalized_args.get("command")
    if not isinstance(command, str):
        return RiskLevel.MEDIUM
    if any(pattern.search(command) for pattern in HIGH_RISK_COMMANDS):
        return RiskLevel.HIGH
    if any(pattern.search(command) for pattern in SAFE_QUERY_COMMANDS):
        return RiskLevel.LOW
    return RiskLevel.MEDIUM


def _decision_value_for_mode(
    mode: PermissionMode,
    risk_level: RiskLevel,
) -> PermissionDecisionValue:
    if risk_level is RiskLevel.HIGH:
        return PermissionDecisionValue.DENY

    if mode is PermissionMode.STRICT:
        return PermissionDecisionValue.REQUIRE_CONFIRMATION

    if mode is PermissionMode.DEFAULT:
        if risk_level is RiskLevel.LOW:
            return PermissionDecisionValue.ALLOW
        return PermissionDecisionValue.REQUIRE_CONFIRMATION

    if mode in {PermissionMode.PERMISSIVE, PermissionMode.TRUSTED}:
        return PermissionDecisionValue.ALLOW

    return PermissionDecisionValue.REQUIRE_CONFIRMATION
