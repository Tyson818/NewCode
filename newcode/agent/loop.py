from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import platform
from typing import Any

from newcode.agent.collector import StreamingTurnCollector
from newcode.agent.config import AgentLoopConfig, StopReason
from newcode.agent.events import (
    AgentEvent,
    AgentFinalAnswer,
    AgentIterationStarted,
    AgentStopped,
    AgentToolCallStarted,
    AgentToolError,
    AgentToolResult,
)
from newcode.agent.mode import AgentMode, allowed_tool_names, is_tool_allowed
from newcode.agent.scheduler import ToolExecutionRecord, ToolScheduler
from newcode.context.manager import ContextManager
from newcode.memory.service import MemoryService
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
)
from newcode.prompt import PromptBuildContext, PromptBuilder, PromptEnvironment
from newcode.providers.base import ChatProvider, ProviderError
from newcode.session import ChatMessage, ChatSession
from newcode.tools.executor import execute_tool_call, make_failure_result
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolCall, ToolContext, ToolResult

class AgentLoop:
    def __init__(
        self,
        *,
        provider: ChatProvider,
        session: ChatSession,
        registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        config: AgentLoopConfig | None = None,
        prompt_builder: PromptBuilder | None = None,
        permission_manager: PermissionManager | None = None,
        context_manager: ContextManager | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.provider = provider
        self.session = session
        self.registry = registry or create_default_registry()
        self.tool_context = tool_context or ToolContext(workspace_root=Path.cwd())
        self.config = config or AgentLoopConfig()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.scheduler = ToolScheduler(self.registry)
        self.permission_manager = permission_manager or PermissionManager()
        self.context_manager = context_manager
        self.memory_service = memory_service

    def run(
        self,
        user_input: str,
        *,
        mode: AgentMode = AgentMode.DO,
        cancel_flag: Any | None = None,
    ) -> Iterator[AgentEvent]:
        if _is_cancelled(cancel_flag):
            yield AgentStopped(
                StopReason.USER_CANCELLED,
                "用户已取消，本轮不会发起模型请求。",
                iteration=0,
            )
            return

        self.session.add_user_message(user_input)
        consecutive_unknown_tools = 0
        tool_error_count = 0

        for iteration in range(1, self.config.max_iterations + 1):
            if _is_cancelled(cancel_flag):
                yield AgentStopped(
                    StopReason.USER_CANCELLED,
                    "用户已取消，本轮不会继续发起模型请求或工具执行。",
                    iteration=iteration,
                )
                return

            yield AgentIterationStarted(
                iteration=iteration,
                max_iterations=self.config.max_iterations,
            )

            collector = StreamingTurnCollector()
            try:
                if self.context_manager is not None:
                    self.context_manager.prepare(self._generate_summary)
                provider_events = self.provider.stream_chat(
                    self._provider_messages(mode, iteration),
                    tools=self.registry.to_openai_tools(allowed_tool_names(mode, self.registry)),
                    allow_tool_calls=True,
                )
            except ProviderError as exc:
                yield AgentStopped(
                    StopReason.PROVIDER_ERROR,
                    str(exc),
                    iteration=iteration,
                )
                return

            yield from collector.consume(provider_events)
            turn_result = collector.result

            if turn_result.provider_error is not None:
                yield AgentStopped(
                    StopReason.PROVIDER_ERROR,
                    str(turn_result.provider_error),
                    iteration=iteration,
                )
                return

            if self.context_manager is not None:
                self.context_manager.record_usage(turn_result.usage)

            if not turn_result.tool_calls:
                if turn_result.assistant_content.strip():
                    self.session.add_assistant_message(turn_result.assistant_content)
                yield AgentFinalAnswer(turn_result.assistant_content)
                if self.memory_service is not None:
                    try:
                        self.memory_service.submit(self.session.messages)
                    except Exception:
                        pass
                return

            self.session.add_assistant_tool_calls(turn_result.tool_calls)

            for tool_call in turn_result.tool_calls:
                yield AgentToolCallStarted(tool_call, iteration=iteration)

            disallowed_tool_call = self._find_disallowed_tool_call(
                turn_result.tool_calls,
                mode,
            )
            if disallowed_tool_call is not None:
                result = self._make_disallowed_tool_result(
                    disallowed_tool_call,
                    mode,
                )
                self.session.add_tool_result(disallowed_tool_call.id, result)
                yield AgentToolError(
                    tool_call=disallowed_tool_call,
                    message=result.error.message if result.error else "工具不可用",
                    code=result.error.code if result.error else "disallowed_tool",
                    iteration=iteration,
                    details=result.error.details if result.error else {},
                )
                yield AgentStopped(
                    StopReason.DISALLOWED_TOOL_CALL,
                    "当前模式不允许执行该工具。",
                    iteration=iteration,
                )
                return

            if _is_cancelled(cancel_flag):
                yield AgentStopped(
                    StopReason.USER_CANCELLED,
                    "用户已取消，本轮不会继续执行工具。",
                    iteration=iteration,
                )
                return

            permission_records, allowed_tool_calls, allowed_indexes = self._precheck_permissions(
                turn_result.tool_calls,
            )

            scheduled_records = self.scheduler.execute(
                allowed_tool_calls,
                self._execute_tool_call,
                self._make_unknown_tool_result,
            )
            execution_records = [
                ToolExecutionRecord(
                    index=allowed_indexes[record.index],
                    tool_call=record.tool_call,
                    result=record.result,
                )
                for record in scheduled_records
            ]
            execution_records = sorted(
                [*permission_records, *execution_records],
                key=lambda record: record.index,
            )

            for record in execution_records:
                if _is_cancelled(cancel_flag):
                    yield AgentStopped(
                        StopReason.USER_CANCELLED,
                        "用户已取消，本轮不会继续执行工具。",
                        iteration=iteration,
                    )
                    return

                tool_call = record.tool_call
                result = record.result
                self.session.add_tool_result(tool_call.id, result)

                if result.ok:
                    consecutive_unknown_tools = 0
                    yield AgentToolResult(tool_call, result, iteration=iteration)
                    continue

                yield AgentToolError(
                    tool_call=tool_call,
                    message=result.error.message if result.error else "工具执行失败",
                    code=result.error.code if result.error else "tool_error",
                    iteration=iteration,
                    details=result.error.details if result.error else {},
                )

                if result.error and result.error.code == "unknown_tool":
                    consecutive_unknown_tools += 1
                    if (
                        consecutive_unknown_tools
                        >= self.config.unknown_tool_threshold
                    ):
                        yield AgentStopped(
                            StopReason.UNKNOWN_TOOL_LIMIT,
                            "连续请求未知工具达到上限。",
                            iteration=iteration,
                        )
                        return
                    continue

                consecutive_unknown_tools = 0
                tool_error_count += 1
                if tool_error_count >= self.config.tool_error_threshold:
                    yield AgentStopped(
                        StopReason.TOOL_ERROR_LIMIT,
                        "工具执行错误次数达到上限。",
                        iteration=iteration,
                    )
                    return

        yield AgentStopped(
            StopReason.MAX_ITERATIONS,
            "已达到最大迭代次数。",
            iteration=self.config.max_iterations,
        )

    def _execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        return execute_tool_call(tool_call, self.registry, self.tool_context)

    def _provider_messages(
        self,
        mode: AgentMode,
        iteration: int,
    ) -> list[ChatMessage]:
        context = PromptBuildContext(
            mode=mode,
            iteration=iteration,
            max_iterations=self.config.max_iterations,
            environment=PromptEnvironment(
                workspace_root=str(self.tool_context.workspace_root),
                platform=platform.system() or platform.platform(),
            ),
            permission_mode=self.permission_manager.mode.value,
        )
        return self.prompt_builder.build_messages(self.session.messages, context)

    def _generate_summary(self, prompt: str) -> str:
        messages = [
            ChatMessage(role="system", content="你是上下文摘要器。"),
            ChatMessage(role="user", content=prompt),
        ]
        parts: list[str] = []
        for event in self.provider.stream_chat(messages, tools=[], allow_tool_calls=False):
            if hasattr(event, "text"):
                parts.append(event.text)
        return "".join(parts)

    def _make_unknown_tool_result(self, tool_call: ToolCall) -> ToolResult:
        return make_failure_result(
            tool_call.name,
            "unknown_tool",
            "模型请求了未注册的工具。",
            {"tool_name": tool_call.name},
            context=self.tool_context,
        )

    def _find_disallowed_tool_call(
        self,
        tool_calls: list[ToolCall],
        mode: AgentMode,
    ) -> ToolCall | None:
        for tool_call in tool_calls:
            if self.registry.get(tool_call.name) is None:
                continue
            if not is_tool_allowed(tool_call.name, mode, self.registry):
                return tool_call
        return None

    def _make_disallowed_tool_result(
        self,
        tool_call: ToolCall,
        mode: AgentMode,
    ) -> ToolResult:
        return make_failure_result(
            tool_call.name,
            "disallowed_tool",
            "当前模式不允许执行该工具。",
            {"tool_name": tool_call.name, "mode": mode.value},
            context=self.tool_context,
        )

    def _precheck_permissions(
        self,
        tool_calls: list[ToolCall],
    ) -> tuple[list[ToolExecutionRecord], list[ToolCall], list[int]]:
        denied_records: list[ToolExecutionRecord] = []
        allowed_tool_calls: list[ToolCall] = []
        allowed_indexes: list[int] = []

        for index, tool_call in enumerate(tool_calls):
            if self.registry.get(tool_call.name) is None:
                allowed_tool_calls.append(tool_call)
                allowed_indexes.append(index)
                continue

            decision = self.permission_manager.check(tool_call, self.tool_context, self.registry.get(tool_call.name))
            if decision.decision is PermissionDecisionValue.ALLOW:
                allowed_tool_calls.append(tool_call)
                allowed_indexes.append(index)
                continue

            denied_records.append(
                ToolExecutionRecord(
                    index=index,
                    tool_call=tool_call,
                    result=self._make_permission_denied_result(tool_call, decision),
                )
            )

        return denied_records, allowed_tool_calls, allowed_indexes

    def _make_permission_denied_result(
        self,
        tool_call: ToolCall,
        decision: PermissionDecision,
    ) -> ToolResult:
        return make_failure_result(
            tool_call.name,
            "permission_denied",
            decision.reason,
            {
                "permission_layer": decision.layer.value,
                "risk_level": decision.risk_level.value,
                "matched_rule": decision.matched_rule,
                "decision_reason": decision.reason,
            },
            context=self.tool_context,
        )


def _is_cancelled(cancel_flag: Any | None) -> bool:
    if cancel_flag is None:
        return False
    if isinstance(cancel_flag, bool):
        return cancel_flag
    is_set = getattr(cancel_flag, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if hasattr(cancel_flag, "cancelled"):
        return bool(getattr(cancel_flag, "cancelled"))
    if hasattr(cancel_flag, "value"):
        return bool(getattr(cancel_flag, "value"))
    return False
