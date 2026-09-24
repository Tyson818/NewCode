"""Hook 配置和安全上下文的纯数据模型。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from newcode.context.redaction import redact_text, redact_value


RULE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MAX_MATCH_VALUE_LENGTH = 1024
MAX_MESSAGE_SUMMARY_LENGTH = 512
MAX_ACTION_TEXT_LENGTH = 4000
MAX_ACTION_TIMEOUT_SECONDS = 60.0
SAFE_HOOK_CODES = frozenset({
    "hook_config_invalid", "hook_rule_invalid", "hook_event_invalid",
    "hook_condition_invalid", "hook_action_invalid", "hook_timeout",
    "hook_action_failed", "hook_tool_denied", "hook_http_disabled",
    "hook_http_denied", "hook_subagent_not_available", "hook_shutdown_timeout",
})
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential|authorization|cookie)\b(\s*[:=]\s*)\S+"
)


class HookEvent(str, Enum):
    SYSTEM_START = "system_start"
    SYSTEM_END = "system_end"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    USER_MESSAGE_RECEIVED = "user_message_received"
    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_MODEL_RESPONSE = "after_model_response"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    TURN_CANCELLED = "turn_cancelled"
    TURN_EXCEPTION = "turn_exception"


class HookActionType(str, Enum):
    SHELL = "shell"
    PROMPT_INJECTION = "prompt_injection"
    HTTP_REQUEST = "http_request"
    SUBAGENT = "subagent"


class HookSource(str, Enum):
    USER = "user"
    PROJECT = "project"


class HookScope(str, Enum):
    SYSTEM = "system"
    SESSION = "session"
    TURN = "turn"
    MESSAGE = "message"
    TOOL = "tool"


class HookOutcomeStatus(str, Enum):
    SKIPPED = "skipped"
    SCHEDULED = "scheduled"
    DENIED = "denied"
    FAILED = "failed"


class HookValidationError(ValueError):
    """只向调用方暴露固定代码，不携带配置原文。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HookPredicate:
    field: str
    operator: str
    pattern: str
    negate: bool = False


@dataclass(frozen=True)
class HookCondition:
    operator: str
    predicates: tuple[HookPredicate, ...]


@dataclass(frozen=True)
class HookAction:
    type: HookActionType
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class HookRule:
    id: str
    event: HookEvent
    action: HookAction
    source: HookSource
    condition: HookCondition | None = None
    once: bool = False
    async_requested: bool = False
    deny: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or RULE_ID_PATTERN.fullmatch(self.id) is None:
            raise HookValidationError("hook_rule_invalid")
        if not isinstance(self.event, HookEvent):
            raise HookValidationError("hook_event_invalid")
        if not isinstance(self.action, HookAction) or not isinstance(self.action.type, HookActionType):
            raise HookValidationError("hook_action_invalid")
        if not isinstance(self.source, HookSource):
            raise HookValidationError("hook_rule_invalid")
        if type(self.once) is not bool or type(self.async_requested) is not bool or type(self.deny) is not bool:
            raise HookValidationError("hook_rule_invalid")
        if self.event is HookEvent.BEFORE_TOOL and self.async_requested:
            raise HookValidationError("hook_rule_invalid")
        if self.deny and self.event is not HookEvent.BEFORE_TOOL:
            raise HookValidationError("hook_rule_invalid")
        if self.reason is not None and (
            not self.deny or not isinstance(self.reason, str) or not self.reason.strip()
            or len(self.reason) > 256
        ):
            raise HookValidationError("hook_rule_invalid")


@dataclass(frozen=True)
class HookDiagnostic:
    code: str
    source: HookSource
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if self.code not in SAFE_HOOK_CODES:
            raise HookValidationError("hook_rule_invalid")
        if self.rule_id is not None and RULE_ID_PATTERN.fullmatch(self.rule_id) is None:
            raise HookValidationError("hook_rule_invalid")


@dataclass(frozen=True)
class HookNetworkPolicy:
    enabled: bool = False
    allow_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class HookLoadResult:
    rules: tuple[HookRule, ...]
    network: HookNetworkPolicy
    diagnostics: tuple[HookDiagnostic, ...] = ()


@dataclass(frozen=True)
class HookOutcome:
    rule_id: str
    status: HookOutcomeStatus
    diagnostic_code: str | None = None


@dataclass(frozen=True)
class HookScopeIdentity:
    """供后续 once 引擎使用的非持久化键；此处不维护状态。"""

    scope: HookScope
    parts: tuple[str, ...]

    @classmethod
    def from_context(cls, context: HookContext, *, process_id: str) -> HookScopeIdentity:
        event = context.event
        fields = context.fields
        if event in (HookEvent.SYSTEM_START, HookEvent.SYSTEM_END):
            scope, keys = HookScope.SYSTEM, (process_id,)
        elif event in (HookEvent.SESSION_START, HookEvent.SESSION_END):
            scope, keys = HookScope.SESSION, (fields.get("session.id"),)
        elif event is HookEvent.USER_MESSAGE_RECEIVED:
            scope, keys = HookScope.MESSAGE, (fields.get("session.id"), fields.get("message.id"))
        elif event in (HookEvent.BEFORE_TOOL, HookEvent.AFTER_TOOL):
            scope, keys = HookScope.TOOL, (fields.get("session.id"), fields.get("tool.call_id"))
        else:
            scope, keys = HookScope.TURN, (fields.get("session.id"), fields.get("turn.index"))
        if any(type(value) not in (str, int) or not str(value) for value in keys):
            raise HookValidationError("hook_rule_invalid")
        return cls(scope, tuple(str(value) for value in keys))


ALLOWED_CONTEXT_FIELDS = frozenset({
    "event", "mode", "session.id", "turn.index", "message.id", "message.summary",
    "tool.name", "tool.call_id", "tool.read_only", "tool.normalized_args",
    "tool.result.ok", "tool.error_code", "stop.reason", "exception.kind",
})


@dataclass(frozen=True, init=False)
class HookContext:
    """仅持有白名单字段；用户消息在进入条件匹配前已脱敏截断。"""

    event: HookEvent
    fields: Mapping[str, str | int | bool]

    def __init__(
        self,
        event: HookEvent,
        fields: Mapping[str, Any] | None = None,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(event, HookEvent):
            raise HookValidationError("hook_event_invalid")
        cleaned: dict[str, str | int | bool] = {"event": event.value}
        for key, value in (fields or {}).items():
            if key not in ALLOWED_CONTEXT_FIELDS or key == "event":
                continue
            if key == "message.summary" and event is not HookEvent.USER_MESSAGE_RECEIVED:
                continue
            if key == "tool.normalized_args":
                if not _json_safe(value):
                    continue
                try:
                    value = json.dumps(redact_value(value, sensitive_values), ensure_ascii=False, sort_keys=True, allow_nan=False)
                except (TypeError, ValueError):
                    continue
            elif isinstance(value, str):
                value = redact_text(value, sensitive_values)
                if key == "message.summary":
                    value = SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
            if isinstance(value, str):
                if key == "message.summary":
                    cleaned[key] = value[:MAX_MESSAGE_SUMMARY_LENGTH]
                elif len(value) <= MAX_MATCH_VALUE_LENGTH:
                    cleaned[key] = value
            elif type(value) in (bool, int):
                cleaned[key] = value
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "fields", cleaned)


def _json_safe(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) is str:
        return len(value) <= MAX_MATCH_VALUE_LENGTH
    if type(value) is int:
        return value.bit_length() <= 4096
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return len(value) <= 64 and all(_json_safe(item, depth + 1) for item in value)
    if type(value) is dict:
        return len(value) <= 64 and all(
            type(key) is str and _json_safe(item, depth + 1) for key, item in value.items()
        )
    return False
