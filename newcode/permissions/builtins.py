from __future__ import annotations

from newcode.permissions.sensitive import check_sensitive_file_policy
from newcode.permissions.types import PermissionDecision, PermissionDecisionValue, PermissionLayer, PermissionRequest, RiskLevel


def check_built_in_policies(
    request: PermissionRequest,
) -> PermissionDecision | None:
    sensitive = check_sensitive_file_policy(request)
    if sensitive is not None:
        return sensitive
    if isinstance(request.normalized_args.get("mcp_server"), str):
        return PermissionDecision(PermissionDecisionValue.REQUIRE_CONFIRMATION, request.tool_name, "External MCP tools require confirmation.", RiskLevel.HIGH, "built_in_external_mcp", PermissionLayer.BUILT_IN_RULES, request.original_args, request.normalized_args)
    return None
