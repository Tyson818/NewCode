from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]


class PermissionMode(Enum):
    STRICT = "strict"
    DEFAULT = "default"
    PERMISSIVE = "permissive"
    TRUSTED = "trusted"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PermissionDecisionValue(Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class PermissionLayer(Enum):
    HARD_DENYLIST = "hard_denylist"
    WORKSPACE_SANDBOX = "workspace_sandbox"
    SESSION_RULES = "session_rules"
    LOCAL_PROJECT_RULES = "local_project_rules"
    PROJECT_RULES = "project_rules"
    USER_GLOBAL_RULES = "user_global_rules"
    BUILT_IN_RULES = "built_in_rules"
    PERMISSION_MODE = "permission_mode"
    HITL_CONFIRMATION = "hitl_confirmation"


class ConfirmationScope(Enum):
    ONCE = "once"
    SESSION = "session"


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    original_args: JsonObject
    normalized_args: JsonObject
    workspace_root: Path
    mode: PermissionMode

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))


@dataclass(frozen=True)
class PermissionDecision:
    decision: PermissionDecisionValue
    tool_name: str
    reason: str
    risk_level: RiskLevel
    matched_rule: str | None
    layer: PermissionLayer
    original_args: JsonObject = field(default_factory=dict)
    normalized_args: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class ConfirmationResult:
    allowed: bool
    scope: ConfirmationScope
    reason: str = ""


@dataclass(frozen=True)
class PermissionMatch:
    command: str | None = None
    command_glob: str | None = None
    path: str | None = None
    path_glob: str | None = None
    mcp_server: str | None = None
    mcp_server_glob: str | None = None
    mcp_tool: str | None = None
    mcp_tool_glob: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    id: str
    tool: str
    match: PermissionMatch
    action: PermissionDecisionValue
    reason: str
    risk_level: RiskLevel
    source: PermissionLayer

    def __post_init__(self) -> None:
        if self.action not in {
            PermissionDecisionValue.ALLOW,
            PermissionDecisionValue.DENY,
        }:
            raise ValueError("PermissionRule action 只支持 allow / deny")
