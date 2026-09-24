"""声明式 Hook 的纯配置、模型与条件匹配接口。"""

from .conditions import condition_matches, parse_condition
from .engine import HookEmission, HookEngine
from .loader import load_hook_rules
from .types import (
    HookAction, HookActionType, HookCondition, HookContext, HookDiagnostic,
    HookEvent, HookLoadResult, HookNetworkPolicy, HookOutcome,
    HookOutcomeStatus, HookPredicate, HookRule, HookScope, HookScopeIdentity,
    HookSource, HookValidationError,
)

__all__ = [
    "HookAction", "HookActionType", "HookCondition", "HookContext",
    "HookDiagnostic", "HookEvent", "HookLoadResult", "HookNetworkPolicy",
    "HookOutcome", "HookOutcomeStatus", "HookPredicate", "HookRule",
    "HookScope", "HookScopeIdentity", "HookSource", "HookValidationError",
    "HookEmission", "HookEngine", "condition_matches", "load_hook_rules", "parse_condition",
]
