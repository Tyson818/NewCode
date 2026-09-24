from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import platform
import re
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
from newcode.hooks.actions import HookActionRunner
from newcode.hooks.engine import HookEngine
from newcode.hooks.types import HookContext, HookEvent
from newcode.permissions.manager import PermissionManager
from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
)
from newcode.prompt import PromptBuildContext, PromptBuilder, PromptEnvironment
from newcode.prompt.modules import DynamicPromptBackground
from newcode.providers.base import ChatProvider, ProviderError
from newcode.session import ChatMessage, ChatSession
from newcode.tools.executor import execute_tool_call, make_failure_result
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolCall, ToolContext, ToolResult
from newcode.skills.discovery import SkillDiscovery
from newcode.skills.policy import visible_tool_names
from newcode.skills.state import ActiveSkillState
from newcode.skills.tool import LoadSkillTool
from newcode.skills.types import SkillCatalog

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
        skill_state: ActiveSkillState | None = None,
        skill_catalog: SkillCatalog | None = None,
        skill_discovery: SkillDiscovery | None = None,
        hook_engine: HookEngine | None = None,
        hook_actions: HookActionRunner | None = None,
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
        self.hook_engine = hook_engine
        self.hook_actions = hook_actions
        self._turn_index = 0
        self.skill_state = skill_state or ActiveSkillState()
        self._skill_discovery = skill_discovery or SkillDiscovery()
        self._fixed_skill_catalog = skill_catalog is not None
        self._skill_catalog = skill_catalog or self._skill_discovery.discover(self.tool_context.workspace_root)
        if self.registry.get("load_skill") is None:
            self.registry.register(
                LoadSkillTool(
                    catalog=lambda: self._skill_catalog,
                    state=self.skill_state,
                    registry=self.registry,
                ),
                read_only=False,
                do_visible=True,
            )

    def run(
        self,
        user_input: str,
        *,
        mode: AgentMode = AgentMode.DO,
        cancel_flag: Any | None = None,
    ) -> Iterator[AgentEvent]:
        """在原有事件流外发射内部 Hook，不改变 AgentEvent 顺序。"""

        self._turn_index += 1
        if not _is_cancelled(cancel_flag):
            self._emit_hook(HookEvent.TURN_START, mode)
        ended = False
        try:
            for event in self._run_core(user_input, mode=mode, cancel_flag=cancel_flag):
                if isinstance(event, AgentFinalAnswer):
                    self._emit_hook(HookEvent.TURN_END, mode, {"stop.reason": "final_answer"})
                    ended = True
                elif isinstance(event, AgentStopped):
                    if event.reason is StopReason.USER_CANCELLED:
                        self._emit_hook(HookEvent.TURN_CANCELLED, mode, {"stop.reason": "user_cancelled"})
                    elif event.reason is StopReason.PROVIDER_ERROR:
                        self._emit_hook(HookEvent.TURN_EXCEPTION, mode, {"exception.kind": "provider_error"})
                    self._emit_hook(HookEvent.TURN_END, mode, {"stop.reason": event.reason.value})
                    ended = True
                yield event
        except GeneratorExit:
            if not ended:
                self._emit_hook(HookEvent.TURN_CANCELLED, mode, {"stop.reason": "generator_closed"})
                self._emit_hook(HookEvent.TURN_END, mode, {"stop.reason": "user_cancelled"})
            raise
        except Exception:
            if not ended:
                self._emit_hook(HookEvent.TURN_EXCEPTION, mode, {"exception.kind": "agent_exception"})
                self._emit_hook(HookEvent.TURN_END, mode, {"stop.reason": "exception"})
            raise

    def _run_core(
        self,
        user_input: str,
        *,
        mode: AgentMode,
        cancel_flag: Any | None,
    ) -> Iterator[AgentEvent]:
        if _is_cancelled(cancel_flag):
            yield AgentStopped(
                StopReason.USER_CANCELLED,
                "用户已取消，本轮不会发起模型请求。",
                iteration=0,
            )
            return

        previous_count = len(self.session.messages)
        self.session.add_user_message(user_input)
        if len(self.session.messages) > previous_count:
            self._emit_hook(HookEvent.USER_MESSAGE_RECEIVED, mode, {
                "message.id": f"{self._hook_session_id()}:{self._turn_index}:{len(self.session.messages)}",
                "message.summary": user_input,
            })
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
            self._refresh_skills()

            collector = StreamingTurnCollector()
            try:
                if self.context_manager is not None:
                    self.context_manager.prepare(self._generate_summary)
                self._emit_hook(HookEvent.BEFORE_MODEL_REQUEST, mode)
                injections = self.hook_actions.consume_prompt_injections() if self.hook_actions is not None else ()
                provider_events = self.provider.stream_chat(
                    self._provider_messages(mode, iteration, injections),
                    tools=self.registry.to_openai_tools(self._visible_tool_names(mode)),
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

            self._emit_hook(HookEvent.AFTER_MODEL_RESPONSE, mode)

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

            hook_records, allowed_tool_calls, allowed_indexes = self._precheck_hooks(
                allowed_tool_calls, allowed_indexes, mode,
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
                [*permission_records, *hook_records, *execution_records],
                key=lambda record: record.index,
            )
            executed_indexes = {
                allowed_indexes[record.index]
                for record in scheduled_records
                if self.registry.get(record.tool_call.name) is not None
            }

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
                if record.index in executed_indexes:
                    self._emit_hook(HookEvent.AFTER_TOOL, mode, {
                        **self._hook_tool_fields(tool_call),
                        "tool.result.ok": result.ok,
                        "tool.error_code": self._safe_tool_error_code(result),
                    })

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

    def _refresh_skills(self) -> None:
        if not self._fixed_skill_catalog:
            self._skill_catalog = self._skill_discovery.discover(self.tool_context.workspace_root)
        self.skill_state.refresh(self._skill_catalog)

    def _visible_tool_names(self, mode: AgentMode) -> frozenset[str]:
        return visible_tool_names(mode, self.registry, self.skill_state)

    def _provider_messages(
        self,
        mode: AgentMode,
        iteration: int,
        hook_injections: tuple[str, ...] = (),
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
        active_skills = self.skill_state.prompt_background()
        if not active_skills and not hook_injections:
            return self.prompt_builder.build_messages(self.session.messages, context)
        return self.prompt_builder.build_messages(
            self.session.messages,
            context,
            dynamic_background=DynamicPromptBackground(
                active_skills=active_skills,
                hook_injections="\n\n".join(hook_injections),
            ),
        )

    def _hook_session_id(self) -> str:
        return self.session.session_id or f"in-memory-{id(self.session)}"

    def _emit_hook(
        self, event: HookEvent, mode: AgentMode,
        extra: dict[str, Any] | None = None, *, hook_origin: bool = False,
    ) -> bool:
        if self.hook_engine is None:
            return False
        fields: dict[str, Any] = {
            "session.id": self._hook_session_id(),
            "turn.index": self._turn_index,
            "mode": mode.value,
        }
        fields.update(extra or {})
        try:
            context = HookContext(
                event, fields,
                sensitive_values=self.tool_context.sensitive_values,
            )
            return self.hook_engine.emit(event, context, hook_origin=hook_origin).denied
        except Exception:
            # Hook 内部失败不会改变主 Agent 流程，也不是合法 deny。
            return False

    def _hook_tool_fields(self, tool_call: ToolCall) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "tool.name": tool_call.name,
            "tool.call_id": tool_call.id,
            "tool.read_only": self.registry.is_read_only(tool_call.name),
        }
        try:
            tool = self.registry.get(tool_call.name)
            request = build_permission_request(tool_call, self.tool_context, self.permission_manager.mode, tool)
            fields["tool.normalized_args"] = request.normalized_args
        except Exception:
            pass
        return fields

    def _precheck_hooks(
        self, allowed_calls: list[ToolCall], allowed_indexes: list[int], mode: AgentMode,
    ) -> tuple[list[ToolExecutionRecord], list[ToolCall], list[int]]:
        if self.hook_engine is None:
            return [], allowed_calls, allowed_indexes
        denied: list[ToolExecutionRecord] = []
        remaining_calls: list[ToolCall] = []
        remaining_indexes: list[int] = []
        for tool_call, index in zip(allowed_calls, allowed_indexes, strict=True):
            if self.registry.get(tool_call.name) is None:
                remaining_calls.append(tool_call)
                remaining_indexes.append(index)
                continue
            if self._emit_hook(HookEvent.BEFORE_TOOL, mode, self._hook_tool_fields(tool_call)):
                denied.append(ToolExecutionRecord(
                    index=index, tool_call=tool_call,
                    result=make_failure_result(
                        tool_call.name, "hook_tool_denied", "Hook 拒绝该工具调用。",
                        context=self.tool_context,
                    ),
                ))
                continue
            remaining_calls.append(tool_call)
            remaining_indexes.append(index)
        return denied, remaining_calls, remaining_indexes

    @staticmethod
    def _safe_tool_error_code(result: ToolResult) -> str:
        if result.error is None:
            return ""
        code = result.error.code
        return code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,64}", code) else "tool_error"

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
            if tool_call.name not in self._visible_tool_names(mode):
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
