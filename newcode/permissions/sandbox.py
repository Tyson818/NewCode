from __future__ import annotations

from pathlib import Path

from newcode.permissions.normalizer import FILE_PATH_TOOLS
from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionRequest,
    RiskLevel,
)


def check_workspace_sandbox(request: PermissionRequest) -> PermissionDecision | None:
    if request.tool_name not in FILE_PATH_TOOLS:
        return None

    resolved_path_value = request.normalized_args.get("resolved_path")
    if not isinstance(resolved_path_value, str):
        return None

    workspace_root = Path(request.workspace_root).resolve()
    resolved_path = Path(resolved_path_value).resolve(strict=False)

    try:
        resolved_path.relative_to(workspace_root)
    except ValueError:
        return PermissionDecision(
            decision=PermissionDecisionValue.DENY,
            tool_name=request.tool_name,
            reason="路径超出当前工作区，已由 workspace sandbox 拒绝。",
            risk_level=RiskLevel.HIGH,
            matched_rule="workspace_sandbox",
            layer=PermissionLayer.WORKSPACE_SANDBOX,
            original_args=request.original_args,
            normalized_args=request.normalized_args,
        )

    return None
