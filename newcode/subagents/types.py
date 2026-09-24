"""Definition frontmatter、catalog 与脱敏诊断纯数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
import threading
import time
from typing import Callable

from newcode.permissions.types import PermissionDecisionValue, PermissionMode, PermissionRule


AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_AGENT_DESCRIPTION_LENGTH = 240
MAX_AGENT_BODY_BYTES = 64 * 1024
MAX_TASK_ROUNDS = 8
MAX_TASK_SECONDS = 300.0
MAX_TASK_TOKENS = 16_000
MAX_TASK_SUMMARY_CHARS = 4_000


class AgentSource(str, Enum):
    PROJECT = "project"
    USER = "user"
    BUILTIN = "builtin"
    PLUGIN = "plugin"


class AgentPermissionMode(str, Enum):
    INHERIT = "inherit"
    STRICT = "strict"
    DEFAULT = "default"
    PERMISSIVE = "permissive"
    TRUSTED = "trusted"


class AgentValidationError(ValueError):
    """仅携带稳定错误码，不包含输入文本或路径。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BACKGROUND = "background"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskExecution(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


TERMINAL_TASK_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT}
)


TASK_ERROR_CODES = frozenset(
    {
        "subagent_invalid_request",
        "subagent_not_found",
        "subagent_definition_invalid",
        "subagent_model_unavailable",
        "subagent_permission_denied",
        "subagent_queue_full",
        "subagent_concurrency_limit",
        "subagent_task_store_full",
        "subagent_timeout",
        "subagent_cancelled",
        "subagent_iteration_limit",
        "subagent_token_budget_exceeded",
        "subagent_provider_error",
        "subagent_result_already_collected",
        "subagent_parent_session_closed",
        "subagent_policy_unavailable",
        "subagent_policy_invalid",
        "subagent_policy_expansion_rejected",
        "subagent_policy_publish_thread_invalid",
    }
)


class SubAgentManagerError(RuntimeError):
    """Manager 对外只暴露稳定错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SessionScope:
    session_id: str
    generation: int


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    scope: SessionScope
    state: TaskState
    execution: TaskExecution
    rounds: int
    input_tokens: int
    output_tokens: int
    error_code: str | None = None
    result_available: bool = False
    completion_sequence: int | None = None


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    state: TaskState
    summary: str = ""
    error_code: str | None = None
    completion_sequence: int | None = None


@dataclass(frozen=True)
class WorkerResult:
    """注入式 worker 的受限返回值，不包含 exception 或工具对象。"""

    summary: str = ""
    error_code: str | None = None


@dataclass(frozen=True)
class WorkerTaskContext:
    task_id: str
    scope: SessionScope
    task_input: str = field(repr=False)
    cancel_event: threading.Event = field(repr=False, compare=False)
    budget: "TaskBudget" = field(repr=False, compare=False)
    launch_policy: "ParentPolicySnapshot | None" = field(default=None, repr=False, compare=False)
    payload: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ParentPolicySnapshot:
    """主线程发布的不可变父策略上限；不持有父 AgentLoop/session 引用。"""

    scope: SessionScope
    visible_tools: frozenset[str]
    permission_mode: PermissionMode
    revision: int = 0
    permission_deny_rules: tuple[PermissionRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_tools", frozenset(self.visible_tools))
        rules = tuple(self.permission_deny_rules)
        if any(rule.action is not PermissionDecisionValue.DENY for rule in rules):
            raise ValueError("subagent_policy_deny_rules_only")
        object.__setattr__(self, "permission_deny_rules", rules)


class TaskBudget:
    """有界任务轮次、时长与近似 token 统计；不调用任何模型。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_rounds: int = MAX_TASK_ROUNDS,
        max_seconds: float = MAX_TASK_SECONDS,
        max_tokens: int = MAX_TASK_TOKENS,
    ) -> None:
        self._clock = clock
        self._started_at = clock()
        self._max_rounds = max_rounds
        self._max_seconds = max_seconds
        self._max_tokens = max_tokens
        self._rounds = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._stop_code: str | None = None
        self._lock = threading.Lock()

    @property
    def rounds(self) -> int:
        with self._lock:
            return self._rounds

    @property
    def input_tokens(self) -> int:
        with self._lock:
            return self._input_tokens

    @property
    def output_tokens(self) -> int:
        with self._lock:
            return self._output_tokens

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self._input_tokens + self._output_tokens

    @property
    def stop_code(self) -> str | None:
        with self._lock:
            return self._stop_code

    def remaining_seconds(self) -> float:
        return max(0.0, self._max_seconds - (self._clock() - self._started_at))

    def check(self, cancelled: bool = False) -> bool:
        with self._lock:
            if cancelled:
                self._stop_code = "subagent_cancelled"
            elif self._clock() - self._started_at >= self._max_seconds:
                self._stop_code = "subagent_timeout"
            return self._stop_code is None and self._input_tokens + self._output_tokens < self._max_tokens

    def begin_round(self, request_text: str, *, usage_tokens: int | None = None, cancelled: bool = False) -> bool:
        with self._lock:
            if cancelled:
                self._stop_code = "subagent_cancelled"
                return False
            if self._clock() - self._started_at >= self._max_seconds:
                self._stop_code = "subagent_timeout"
                return False
            if self._rounds >= self._max_rounds:
                self._stop_code = "subagent_iteration_limit"
                return False
            estimate = _safe_token_count(request_text, usage_tokens)
            if self._input_tokens + self._output_tokens + estimate > self._max_tokens:
                self._stop_code = "subagent_token_budget_exceeded"
                return False
            self._rounds += 1
            self._input_tokens += estimate
            return True

    def record_output(self, text: str, *, usage_tokens: int | None = None, cancelled: bool = False) -> bool:
        with self._lock:
            if cancelled:
                self._stop_code = "subagent_cancelled"
                return False
            if self._clock() - self._started_at >= self._max_seconds:
                self._stop_code = "subagent_timeout"
                return False
            estimate = _safe_token_count(text, usage_tokens)
            if self._input_tokens + self._output_tokens + estimate > self._max_tokens:
                self._stop_code = "subagent_token_budget_exceeded"
                return False
            self._output_tokens += estimate
            return True


def _safe_token_count(text: str, usage_tokens: int | None) -> int:
    # 每 3 个 UTF-8 字节约估 1 token，较单纯字符数更保守地覆盖 CJK。
    estimated = (len(text.encode("utf-8")) + 2) // 3
    if usage_tokens is None:
        return estimated
    if isinstance(usage_tokens, bool) or not isinstance(usage_tokens, int) or usage_tokens < 0:
        raise ValueError("usage_tokens_invalid")
    return max(estimated, usage_tokens)


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    source: AgentSource
    tools_allow: tuple[str, ...]
    tools_deny: tuple[str, ...]
    max_iterations: int
    permission_mode: AgentPermissionMode
    model: str | None = None
    body: str = field(default="", repr=False)
    digest: str = field(default="", repr=False)
    root: Path = field(default=Path("."), repr=False)
    entry: Path = field(default=Path("."), repr=False)


@dataclass(frozen=True)
class AgentDiagnostic:
    code: str
    source: AgentSource
    name: str | None = None


@dataclass(frozen=True)
class AgentDirectoryEntry:
    """仅含允许暴露给模型的名称与一句说明。"""

    name: str
    description: str


@dataclass(frozen=True)
class AgentCatalog:
    definitions: tuple[AgentDefinition, ...]
    diagnostics: tuple[AgentDiagnostic, ...] = ()

    def startup_directory(self) -> tuple[AgentDirectoryEntry, ...]:
        return tuple(AgentDirectoryEntry(item.name, item.description) for item in self.definitions)


def parse_agent_frontmatter(value: object) -> tuple[str, str, tuple[str, ...], tuple[str, ...], str | None, int, AgentPermissionMode]:
    """严格校验已解析 YAML；allow 必填，deny 缺省为空。"""

    if not isinstance(value, dict):
        raise AgentValidationError("subagent_definition_invalid")
    allowed = {"name", "description", "tools", "model", "max_iterations", "permission_mode"}
    if set(value) - allowed or not {"name", "description", "tools", "max_iterations", "permission_mode"}.issubset(value):
        raise AgentValidationError("subagent_definition_invalid")

    name = value["name"]
    if not isinstance(name, str) or name != name.casefold() or not AGENT_NAME_PATTERN.fullmatch(name):
        raise AgentValidationError("subagent_definition_invalid")
    description = value["description"]
    if (
        not isinstance(description, str)
        or not description.strip()
        or description != description.strip()
        or "\n" in description
        or "\r" in description
        or len(description) > MAX_AGENT_DESCRIPTION_LENGTH
    ):
        raise AgentValidationError("subagent_definition_invalid")

    tools = value["tools"]
    if not isinstance(tools, dict) or set(tools) - {"allow", "deny"} or "allow" not in tools:
        raise AgentValidationError("subagent_definition_invalid")
    allow = _tool_list(tools["allow"])
    deny = _tool_list(tools.get("deny", []))
    model = value.get("model")
    if model is not None and (
        not isinstance(model, str)
        or not model.strip()
        or model != model.strip()
        or "\n" in model
        or "\r" in model
        or len(model) > 128
    ):
        raise AgentValidationError("subagent_definition_invalid")

    iterations = value["max_iterations"]
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= 8:
        raise AgentValidationError("subagent_definition_invalid")
    try:
        permission_mode = AgentPermissionMode(value["permission_mode"])
    except (TypeError, ValueError) as exc:
        raise AgentValidationError("subagent_definition_invalid") from exc
    return name, description, allow, deny, model, iterations, permission_mode


def _tool_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentValidationError("subagent_definition_invalid")
    if any(not isinstance(name, str) or not TOOL_NAME_PATTERN.fullmatch(name) for name in value):
        raise AgentValidationError("subagent_definition_invalid")
    if len(value) != len(set(value)):
        raise AgentValidationError("subagent_definition_invalid")
    return tuple(value)
