"""Hook 规则的纯调度层；不执行工具或网络动作。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from threading import RLock, local
from typing import Callable

from .conditions import condition_matches
from .types import (
    HookContext, HookDiagnostic, HookEvent, HookOutcome, HookOutcomeStatus,
    HookRule, HookScope, HookScopeIdentity, HookSource, HookValidationError,
    SAFE_HOOK_CODES,
)


ActionSink = Callable[[HookRule, HookContext, int], bool]


@dataclass(frozen=True)
class HookEmission:
    outcomes: tuple[HookOutcome, ...] = ()
    diagnostics: tuple[HookDiagnostic, ...] = ()

    @property
    def denied(self) -> bool:
        return any(item.status is HookOutcomeStatus.DENIED for item in self.outcomes)


class HookEngine:
    """按合并后的声明顺序发射事件，并维护仅内存 once 状态。"""

    def __init__(
        self,
        rules: tuple[HookRule, ...],
        *,
        action_sink: ActionSink | None = None,
        on_session_reset: Callable[[int], None] | None = None,
        process_id: str | None = None,
    ) -> None:
        self.rules = tuple(rules)
        self.action_sink = action_sink
        self.on_session_reset = on_session_reset
        self.process_id = process_id or str(os.getpid())
        self._seen: set[tuple[str, ...]] = set()
        self._lock = RLock()
        self._local = local()
        self._generation = 0
        self._diagnostics: list[HookDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[HookDiagnostic, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def reset_session(self) -> None:
        """clear/new/resume 时清除旧 session 的 once；保留 process once。"""

        with self._lock:
            self._seen = {key for key in self._seen if key[2] == HookScope.SYSTEM.value}
            self._generation += 1
            if self.on_session_reset is not None:
                try:
                    self.on_session_reset(self._generation)
                except Exception:
                    # session 主流程不可因 Hook 状态清理失败而中断。
                    self._diagnostics.append(HookDiagnostic("hook_action_failed", HookSource.USER))

    def emit(
        self,
        event: HookEvent,
        context: HookContext,
        *,
        hook_origin: bool = False,
    ) -> HookEmission:
        if hook_origin or getattr(self._local, "inside", False):
            return HookEmission()
        if context.event is not event:
            return HookEmission()
        outcomes: list[HookOutcome] = []
        diagnostics: list[HookDiagnostic] = []
        with self._lock:
            self._local.inside = True
            try:
                for rule in self.rules:
                    if rule.event is not event:
                        continue
                    try:
                        if not condition_matches(rule.condition, context):
                            continue
                        if rule.once:
                            identity = HookScopeIdentity.from_context(context, process_id=self.process_id)
                            key = (rule.id, event.value, identity.scope.value, *identity.parts)
                            if key in self._seen:
                                continue
                            self._seen.add(key)
                        if rule.deny and event is HookEvent.BEFORE_TOOL:
                            outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.DENIED, "hook_tool_denied"))
                            break
                        if self.action_sink is None:
                            outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.SCHEDULED))
                            continue
                        accepted = self.action_sink(rule, context, self._generation)
                        if accepted:
                            outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.SCHEDULED))
                        else:
                            outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.FAILED, "hook_action_failed"))
                            diagnostics.append(HookDiagnostic("hook_action_failed", rule.source, rule.id))
                    except HookValidationError as error:
                        code = error.code if error.code in SAFE_HOOK_CODES else "hook_action_failed"
                        outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.FAILED, code))
                        diagnostics.append(HookDiagnostic(code, rule.source, rule.id))
                    except Exception:
                        outcomes.append(HookOutcome(rule.id, HookOutcomeStatus.FAILED, "hook_action_failed"))
                        diagnostics.append(HookDiagnostic("hook_action_failed", rule.source, rule.id))
            finally:
                self._local.inside = False
        return HookEmission(tuple(outcomes), tuple(diagnostics))
