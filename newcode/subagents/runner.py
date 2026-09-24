"""在现有 AgentLoop 安全执行链中运行隔离的 Definition/Fork child。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Protocol

from newcode.agent import AgentFinalAnswer, AgentLoop, AgentLoopConfig, AgentStopped
from newcode.agent.mode import AgentMode
from newcode.context.manager import ContextManager
from newcode.context.redaction import redact_text
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import PermissionRuleSet, match_first_rule
from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.types import PermissionDecisionValue, PermissionMode, PermissionLayer
from newcode.providers.base import ChatProvider, ProviderError, ProviderEvent
from newcode.session import ChatMessage, ChatSession
from newcode.skills.state import ActiveSkillState
from newcode.skills.types import SkillCatalog
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import Tool, ToolCall, ToolContext, ToolFailure, ToolResult

from .manager import SubAgentManager
from .policy import child_tool_names, isolated_registry_view, most_restrictive_permission_mode
from .types import (
    AgentDefinition,
    AgentPermissionMode,
    MAX_TASK_SUMMARY_CHARS,
    ParentPolicySnapshot,
    TaskBudget,
    TaskExecution,
    TaskState,
    WorkerResult,
    WorkerTaskContext,
)


MAX_FORK_MESSAGES = 20
MAX_FORK_CHARS = 12_000
MAX_READ_CACHE_ITEMS = 128
MAX_READ_CACHE_BYTES = 2 * 1024 * 1024


class ProviderFactory(Protocol):
    @property
    def available_models(self) -> Iterable[str]: ...

    def create(self, model: str, *, timeout_seconds: float) -> ChatProvider: ...


@dataclass(frozen=True)
class DefinitionTask:
    definition: AgentDefinition
    mode: AgentMode = AgentMode.DO


@dataclass(frozen=True)
class ForkTask:
    messages: tuple[ChatMessage, ...]
    mode: AgentMode = AgentMode.DO
    model: str | None = None
    allowlist: tuple[str, ...] | None = None
    denylist: tuple[str, ...] = ()


def capture_fork_snapshot(
    messages: Sequence[ChatMessage], sensitive_values: tuple[str, ...] = (),
) -> tuple[ChatMessage, ...]:
    """复制最近最多 20 条纯文本 user/assistant 消息，总长不超过 12K。"""
    candidates: list[ChatMessage] = []
    for message in messages:
        if message.role not in {"user", "assistant"} or message.tool_calls or not isinstance(message.content, str):
            continue
        text = redact_text(message.content, sensitive_values)
        if text.strip():
            candidates.append(ChatMessage(role=message.role, content=text))
    selected: list[ChatMessage] = []
    remaining = MAX_FORK_CHARS
    for message in reversed(candidates[-MAX_FORK_MESSAGES:]):
        content = message.content or ""
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[-remaining:]
        selected.append(ChatMessage(role=message.role, content=content))
        remaining -= len(content)
    return tuple(reversed(selected))


class ChildReadCache:
    """按文件 stat 签名校验的 child-local、有界 read_file cache。"""

    def __init__(self, max_items: int = MAX_READ_CACHE_ITEMS, max_bytes: int = MAX_READ_CACHE_BYTES) -> None:
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[tuple[int, int, int, int], ToolResult, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def get(self, path: Path) -> ToolResult | None:
        try:
            signature = _stat_signature(path)
        except OSError:
            return None
        key = str(path)
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry[0] != signature:
                self._bytes -= entry[2]
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry[1]

    def put(self, path: Path, result: ToolResult) -> None:
        try:
            signature = _stat_signature(path)
            size = len(json.dumps(result.to_dict(), ensure_ascii=False, default=str).encode("utf-8"))
        except (OSError, TypeError, ValueError):
            return
        if size > self.max_bytes:
            return
        key = str(path)
        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self._bytes -= old[2]
            self._items[key] = (signature, result, size)
            self._bytes += size
            while len(self._items) > self.max_items or self._bytes > self.max_bytes:
                _, removed = self._items.popitem(last=False)
                self._bytes -= removed[2]

    def invalidate(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def byte_size(self) -> int:
        with self._lock:
            return self._bytes


class SubAgentRunner:
    def __init__(
        self,
        *,
        manager: SubAgentManager,
        provider_factory: ProviderFactory,
        default_model: str,
        registry: ToolRegistry,
        tool_context: ToolContext,
        parent_permission_manager: PermissionManager,
    ) -> None:
        self.manager = manager
        self.provider_factory = provider_factory
        self.default_model = default_model
        self.registry = registry
        self.tool_context = tool_context
        self.parent_permission_manager = parent_permission_manager

    def __call__(self, task: WorkerTaskContext) -> WorkerResult:
        payload = task.payload
        if isinstance(payload, DefinitionTask):
            return self.run_definition(task, payload.definition, mode=payload.mode)
        if isinstance(payload, ForkTask):
            return self.run_fork(
                task,
                payload.messages,
                mode=payload.mode,
                model=payload.model,
                allowlist=payload.allowlist,
                denylist=payload.denylist,
            )
        return WorkerResult(error_code="subagent_invalid_request")

    def run_definition(
        self, task: WorkerTaskContext, definition: AgentDefinition,
        *, mode: AgentMode = AgentMode.DO,
    ) -> WorkerResult:
        if task.launch_policy is None:
            return WorkerResult(error_code="subagent_policy_unavailable")
        return self._run_child(
            task,
            mode=mode,
            allow=definition.tools_allow,
            deny=definition.tools_deny,
            model=definition.model,
            permission_mode=definition.permission_mode,
            system_prompt=definition.body,
            max_iterations=definition.max_iterations,
        )

    def run_fork(
        self, task: WorkerTaskContext, snapshot: Sequence[ChatMessage], *,
        mode: AgentMode = AgentMode.DO, model: str | None = None,
        allowlist: Iterable[str] | None = None, denylist: Iterable[str] = (),
    ) -> WorkerResult:
        if task.launch_policy is None:
            return WorkerResult(error_code="subagent_policy_unavailable")
        safe_snapshot = capture_fork_snapshot(snapshot, self.tool_context.sensitive_values)
        # Fork 缺省使用启动策略可见集；显式 allowlist 只能继续收窄。
        allow = task.launch_policy.visible_tools if allowlist is None else frozenset(allowlist)
        return self._run_child(
            task,
            mode=mode,
            allow=allow,
            deny=denylist,
            model=model,
            permission_mode=None,
            initial_messages=safe_snapshot,
        )

    def _run_child(
        self, task: WorkerTaskContext, *, mode: AgentMode,
        allow: Iterable[str], deny: Iterable[str], model: str | None,
        permission_mode: AgentPermissionMode | None,
        system_prompt: str = "", max_iterations: int = 8,
        initial_messages: Sequence[ChatMessage] = (),
    ) -> WorkerResult:
        launch = task.launch_policy
        if launch is None:
            return WorkerResult(error_code="subagent_policy_unavailable")
        selected_model = model or self.default_model
        if not isinstance(selected_model, str) or selected_model not in set(self.provider_factory.available_models):
            return WorkerResult(error_code="subagent_model_unavailable")
        try:
            provider = self.provider_factory.create(
                selected_model,
                timeout_seconds=max(0.1, task.budget.remaining_seconds()),
            )
        except Exception:
            return WorkerResult(error_code="subagent_model_unavailable")

        role_allow = frozenset(allow)
        role_deny = frozenset(deny)
        cache = ChildReadCache()
        child_session = ChatSession(
            messages=list(initial_messages),
            session_id=f"subagent-{task.task_id[:48]}",
        )
        child_context = ToolContext(
            workspace_root=self.tool_context.workspace_root,
            default_timeout_seconds=self.tool_context.default_timeout_seconds,
            command_timeout_seconds=self.tool_context.command_timeout_seconds,
            sensitive_values=self.tool_context.sensitive_values,
        )
        child_permission = _child_permission_manager(
            self.parent_permission_manager,
            _definition_permission_mode(permission_mode, launch.permission_mode),
        )
        context_manager = ContextManager(
            child_session,
            child_context.workspace_root,
            child_context.sensitive_values,
            artifact_session_id=child_session.session_id,
        )
        wrappers = {
            name: _GuardedTool(
                name=name,
                inner=self.registry.get(name),
                runner=self,
                task=task,
                launch=launch,
                allow=role_allow,
                deny=role_deny,
                mode=mode,
                context=child_context,
                permission=child_permission,
                cache=cache,
            )
            for name in self.registry.names()
            if self.registry.get(name) is not None
        }
        startup = child_tool_names(
            self.registry,
            launch=launch,
            latest=launch,
            allow=role_allow,
            deny=role_deny,
            mode=mode,
            execution=TaskExecution.FOREGROUND,
        )
        # registry 首轮按启动快照封闭；每轮 provider 和 executor 还会查 Manager 最新快照。
        child_registry = isolated_registry_view(self.registry, startup, wrappers)
        child_provider = _GuardedProvider(
            provider,
            runner=self,
            task=task,
            launch=launch,
            allow=role_allow,
            deny=role_deny,
            mode=mode,
            permission=child_permission,
            system_prompt=system_prompt,
        )
        try:
            loop = AgentLoop(
                provider=child_provider,
                session=child_session,
                registry=child_registry,
                tool_context=child_context,
                config=AgentLoopConfig(max_iterations=min(max_iterations, 8)),
                permission_manager=child_permission,
                context_manager=context_manager,
                skill_state=ActiveSkillState(),
                skill_catalog=SkillCatalog(skills=()),
                memory_service=None,
                hook_engine=None,
            )
            for event in loop.run(task.task_input, mode=mode, cancel_flag=task.cancel_event):
                if isinstance(event, AgentFinalAnswer):
                    return WorkerResult(summary=redact_text(event.content, child_context.sensitive_values)[:MAX_TASK_SUMMARY_CHARS])
                if isinstance(event, AgentStopped):
                    if event.reason.value == "user_cancelled":
                        return WorkerResult(error_code="subagent_cancelled")
                    if task.budget.stop_code:
                        return WorkerResult(error_code=task.budget.stop_code)
                    if event.reason.value == "max_iterations":
                        return WorkerResult(error_code="subagent_iteration_limit")
                    if event.reason.value == "provider_error":
                        return WorkerResult(error_code="subagent_provider_error")
            if task.budget.stop_code:
                return WorkerResult(error_code=task.budget.stop_code)
            return WorkerResult(error_code="subagent_provider_error")
        except BaseException:
            return WorkerResult(error_code=task.budget.stop_code or "subagent_provider_error")
        finally:
            cache.invalidate()
            context_manager.cleanup()

    def _latest(self, task: WorkerTaskContext) -> ParentPolicySnapshot:
        return self.manager.policy_snapshot(task.scope)

    def _allowed(
        self, task: WorkerTaskContext, launch: ParentPolicySnapshot,
        allow: frozenset[str], deny: frozenset[str], mode: AgentMode,
    ) -> frozenset[str]:
        latest = self._latest(task)
        return child_tool_names(
            self.registry,
            launch=launch,
            latest=latest,
            allow=allow,
            deny=deny,
            mode=mode,
            execution=self.manager.status(task.scope, task.task_id).execution,
        )


class _GuardedProvider:
    def __init__(self, provider, *, runner, task, launch, allow, deny, mode, permission, system_prompt):
        self.provider = provider
        self.runner = runner
        self.task = task
        self.launch = launch
        self.allow = allow
        self.deny = deny
        self.mode = mode
        self.permission = permission
        self.system_prompt = system_prompt

    def stream_chat(self, messages, tools=None, allow_tool_calls=True) -> Iterator[ProviderEvent]:
        latest = self.runner._latest(self.task)
        self.permission.mode = most_restrictive_permission_mode(self.permission.mode, latest.permission_mode)
        allowed = self.runner._allowed(self.task, self.launch, self.allow, self.deny, self.mode)
        filtered_tools = [
            item for item in (tools or ())
            if isinstance(item, dict)
            and isinstance(item.get("function"), dict)
            and item["function"].get("name") in allowed
        ]
        request_messages = list(messages)
        if self.system_prompt:
            request_messages.insert(0, ChatMessage(role="system", content=self.system_prompt))
        request_budget_text = _messages_text(request_messages) + json.dumps(filtered_tools, ensure_ascii=False, default=str)
        if not self.task.budget.begin_round(request_budget_text, cancelled=self.task.cancel_event.is_set()):
            raise ProviderError(self.task.budget.stop_code or "subagent_budget_exceeded")
        try:
            for event in self.provider.stream_chat(request_messages, tools=filtered_tools, allow_tool_calls=allow_tool_calls):
                if self.task.cancel_event.is_set():
                    self.task.budget.check(cancelled=True)
                    raise ProviderError("subagent_cancelled")
                if hasattr(event, "text") and not self.task.budget.record_output(
                    event.text, cancelled=self.task.cancel_event.is_set(),
                ):
                    raise ProviderError(self.task.budget.stop_code or "subagent_budget_exceeded")
                yield event
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("subagent_provider_error", exc) from None


class _GuardedTool:
    def __init__(self, *, name, inner, runner, task, launch, allow, deny, mode, context, permission, cache):
        self.name = name
        self.inner = inner
        self.runner = runner
        self.task = task
        self.launch = launch
        self.allow = allow
        self.deny = deny
        self.mode = mode
        self.context = context
        self.permission = permission
        self.cache = cache
        self.spec = inner.spec
        self.mcp_metadata = getattr(inner, "mcp_metadata", None)

    def run(self, arguments, context):
        if not self.task.budget.check(cancelled=self.task.cancel_event.is_set()):
            raise ToolFailure(self.task.budget.stop_code or "subagent_token_budget_exceeded", "子任务预算已耗尽。")
        allowed = self.runner._allowed(self.task, self.launch, self.allow, self.deny, self.mode)
        if self.name not in allowed:
            raise ToolFailure("subagent_tool_disallowed", "父策略或子任务策略不允许该工具。")
        latest = self.runner._latest(self.task)
        self.permission.mode = most_restrictive_permission_mode(self.permission.mode, latest.permission_mode)
        call = ToolCall(id="subagent-check", name=self.name, arguments=dict(arguments))
        latest_deny_set = PermissionRuleSet(
            source=PermissionLayer.SESSION_RULES,
            rules=latest.permission_deny_rules,
        )
        latest_request = build_permission_request(call, self.context, self.permission.mode, self.inner)
        if match_first_rule(latest_deny_set, latest_request) is not None:
            raise ToolFailure("permission_denied", "父策略拒绝该工具调用。")
        decision = self.permission.check(call, self.context, self.inner)
        if decision.decision is not PermissionDecisionValue.ALLOW:
            raise ToolFailure("permission_denied", "子任务权限检查未允许该工具。")

        cache_path = _safe_read_path(self.context.workspace_root, arguments) if self.name == "read_file" else None
        if cache_path is not None:
            cached = self.cache.get(cache_path)
            if cached is not None:
                return cached
        try:
            # 此 wrapper 自身已由 AgentLoop 的统一 executor 调用；底层 Tool 仅在
            # 最新策略与独立 PermissionManager 均放行后执行，避免第二条执行路径。
            result = self.inner.run(dict(arguments), self.context)
            if cache_path is not None and result.ok:
                self.cache.put(cache_path, result)
            return result
        finally:
            if self.name in {"write_file", "replace_in_file", "run_command"} or self.mcp_metadata is not None or self.name.startswith("mcp__"):
                self.cache.invalidate()


def _child_permission_manager(parent: PermissionManager, mode: PermissionMode) -> PermissionManager:
    def copy_rule_set(rule_set: PermissionRuleSet) -> PermissionRuleSet:
        return PermissionRuleSet(source=rule_set.source, rules=tuple(rule_set.rules))

    return PermissionManager(
        mode=mode,
        # 只复制 session deny；父 session allow 永不外溢至 child。
        session_rules=PermissionRuleSet(
            source=PermissionLayer.SESSION_RULES,
            rules=tuple(
                rule for rule in parent.session_rules.rules
                if rule.action is PermissionDecisionValue.DENY
            ),
        ),
        local_project_rules=copy_rule_set(parent.local_project_rules),
        project_rules=copy_rule_set(parent.project_rules),
        user_global_rules=copy_rule_set(parent.user_global_rules),
        rule_load_errors=tuple(parent.rule_load_errors),
        confirmer=DenyByDefaultConfirmer(),
    )


def _definition_permission_mode(requested: AgentPermissionMode | None, parent: PermissionMode) -> PermissionMode:
    mapping = {
        AgentPermissionMode.STRICT: PermissionMode.STRICT,
        AgentPermissionMode.DEFAULT: PermissionMode.DEFAULT,
        AgentPermissionMode.PERMISSIVE: PermissionMode.PERMISSIVE,
        AgentPermissionMode.TRUSTED: PermissionMode.TRUSTED,
    }
    return most_restrictive_permission_mode(parent, mapping.get(requested, parent))


def _messages_text(messages: Sequence[ChatMessage]) -> str:
    pieces: list[str] = []
    for message in messages:
        pieces.append(message.role)
        pieces.append(message.content or "")
        if message.tool_call_id:
            pieces.append(message.tool_call_id)
        for call in message.tool_calls or ():
            pieces.extend((call.name, call.raw_arguments))
    return "\n".join(pieces)


def _safe_read_path(root: Path, arguments: dict) -> Path | None:
    value = arguments.get("path")
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or candidate.is_symlink() or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
