from __future__ import annotations

from typing import Any

from .registry import ToolRegistry
from .types import ToolCall, ToolContext, ToolFailure, ToolResult


def execute_tool_call(
    tool_call: ToolCall,
    registry: ToolRegistry,
    context: ToolContext,
) -> ToolResult:
    tool = registry.get(tool_call.name)
    if tool is None:
        return _mask_result(
            ToolResult.failure(
                tool_call.name,
                "unknown_tool",
                "模型请求了未注册的工具",
                {"tool_name": tool_call.name},
            ),
            context,
        )

    try:
        result = tool.run(tool_call.arguments, context)
    except ToolFailure as exc:
        result = ToolResult.failure(
            tool.spec.name,
            exc.code,
            exc.message,
            exc.details,
        )
    except Exception as exc:
        result = ToolResult.failure(
            tool.spec.name,
            "execution_error",
            "工具执行时发生异常",
            {"error_type": type(exc).__name__, "message": str(exc)},
        )

    return _mask_result(result, context)


def make_failure_result(
    tool_name: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    context: ToolContext | None = None,
) -> ToolResult:
    result = ToolResult.failure(tool_name, code, message, details or {})
    return _mask_result(result, context) if context else result


def _mask_result(result: ToolResult, context: ToolContext) -> ToolResult:
    data = _mask_value(result.data, context.sensitive_values)
    error = result.error
    if error is not None:
        from .types import ToolError

        error = ToolError(
            code=error.code,
            message=_mask_text(error.message, context.sensitive_values),
            details=_mask_value(error.details, context.sensitive_values),
        )
    return ToolResult(
        ok=result.ok,
        tool_name=result.tool_name,
        data=data,
        error=error,
        metadata=_mask_value(result.metadata, context.sensitive_values),
    )


def _mask_value(value: Any, sensitive_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _mask_text(value, sensitive_values)
    if isinstance(value, list):
        return [_mask_value(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return [_mask_value(item, sensitive_values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _mask_value(item, sensitive_values)
            for key, item in value.items()
        }
    return value


def _mask_text(text: str, sensitive_values: tuple[str, ...]) -> str:
    masked = text
    for secret in sensitive_values:
        if secret:
            masked = masked.replace(secret, "[REDACTED]")
    return masked
