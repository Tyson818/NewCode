from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TextIO
from typing import Protocol

from newcode.permissions.types import (
    ConfirmationResult,
    ConfirmationScope,
    PermissionDecision,
    PermissionLayer,
    PermissionRequest,
    RiskLevel,
)


class PermissionConfirmer(Protocol):
    def confirm(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ConfirmationResult:
        ...


class DenyByDefaultConfirmer:
    def confirm(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ConfirmationResult:
        return ConfirmationResult(
            allowed=False,
            scope=ConfirmationScope.ONCE,
            reason="No permission confirmer is configured; denied by default.",
        )


class CliPermissionConfirmer:
    def __init__(
        self,
        *,
        input_func: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
        interactive: bool = True,
    ) -> None:
        self.input_func = input_func
        self.output = output
        self.interactive = interactive

    def confirm(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> ConfirmationResult:
        if not self.interactive:
            return ConfirmationResult(
                allowed=False,
                scope=ConfirmationScope.ONCE,
                reason="Permission confirmation is not interactive; denied by default.",
            )

        self._print_summary(request, decision)
        try:
            answer = self.input_func("是否允许？[n] 拒绝 / [o] 仅本次允许 / [s] 本会话允许: ")
        except (EOFError, KeyboardInterrupt):
            return ConfirmationResult(
                allowed=False,
                scope=ConfirmationScope.ONCE,
                reason="Permission confirmation was not completed; denied by default.",
            )

        normalized = answer.strip().lower()
        if normalized in {"o", "once", "y", "yes"}:
            return ConfirmationResult(
                allowed=True,
                scope=ConfirmationScope.ONCE,
                reason="Allowed once by CLI confirmation.",
            )
        if normalized in {"s", "session"}:
            return ConfirmationResult(
                allowed=True,
                scope=ConfirmationScope.SESSION,
                reason="Allowed for this session by CLI confirmation.",
            )
        return ConfirmationResult(
            allowed=False,
            scope=ConfirmationScope.ONCE,
            reason="Denied by CLI confirmation.",
        )

    def _print_summary(
        self,
        request: PermissionRequest,
        decision: PermissionDecision,
    ) -> None:
        print("", file=self.output)
        print("[权限] 工具请求需要确认", file=self.output)
        print(f"  工具: {request.tool_name}", file=self.output)
        print(f"  原因: {_display_reason(request, decision)}", file=self.output)
        print(f"  风险: {decision.risk_level.value}", file=self.output)
        print(f"  层级: {decision.layer.value}", file=self.output)
        if decision.matched_rule:
            print(f"  匹配规则: {decision.matched_rule}", file=self.output)
        summary = _summarize_args(request)
        if summary:
            print(f"  参数: {summary}", file=self.output)


def _summarize_args(request: PermissionRequest) -> str:
    if isinstance(request.normalized_args.get("mcp_server"), str):
        return f"mcp_server={request.normalized_args['mcp_server']}, mcp_tool={request.normalized_args.get('mcp_tool', '')}"
    keys = ("command", "relative_path", "path", "pattern", "query")
    parts = [
        f"{key}={value}"
        for key in keys
        if isinstance((value := request.normalized_args.get(key)), str)
    ]
    return ", ".join(parts)


def _display_reason(
    request: PermissionRequest,
    decision: PermissionDecision,
) -> str:
    if decision.layer is PermissionLayer.PERMISSION_MODE:
        return _permission_mode_reason(request, decision)
    if decision.matched_rule == "built_in_sensitive_file":
        return "访问敏感文件需要确认"
    if decision.matched_rule == "built_in_external_mcp":
        return "外部 MCP 工具需要确认"
    return decision.reason


def _permission_mode_reason(
    request: PermissionRequest,
    decision: PermissionDecision,
) -> str:
    mode = request.mode.value
    if mode == "strict":
        return "strict 权限模式下，该操作需要确认"
    if mode == "default":
        if decision.risk_level is RiskLevel.LOW:
            return "default 权限模式下，低风险操作可直接执行"
        if decision.risk_level is RiskLevel.MEDIUM:
            return "default 权限模式下，中风险操作需要确认"
        return "default 权限模式下，高风险操作会被拒绝"
    if mode in {"permissive", "trusted"}:
        label = "permissive/trusted" if mode == "trusted" else "permissive"
        if decision.risk_level in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            return f"{label} 权限模式下允许低中风险操作"
        return f"{label} 权限模式下，高风险操作会被拒绝"
    return decision.reason
