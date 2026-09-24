from __future__ import annotations

import json
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)
    raw_arguments: str = "{}"


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    details: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool_name: str
    data: Any = None
    error: ToolError | None = None
    metadata: JsonObject = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        tool_name: str,
        data: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            ok=True,
            tool_name=tool_name,
            data=data,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def failure(
        cls,
        tool_name: str,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            ok=False,
            tool_name=tool_name,
            error=ToolError(code=code, message=message, details=dict(details or {})),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> JsonObject:
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class ToolContext:
    workspace_root: Path
    default_timeout_seconds: float = 30.0
    command_timeout_seconds: float = 30.0
    sensitive_values: tuple[str, ...] = ()
    cwd: Path | None = None
    worktree_task_id: str | None = None
    workspace_identity: str = field(init=False)

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).resolve(strict=False)
        cwd = Path(self.cwd).resolve(strict=False) if self.cwd is not None else root
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError("tool_context_cwd_outside_workspace") from exc
        if self.worktree_task_id is not None and (
            not isinstance(self.worktree_task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.worktree_task_id)
        ):
            raise ValueError("tool_context_worktree_identity_invalid")
        identity_input = os.path.normcase(str(root)).encode("utf-8", errors="strict")
        identity = hashlib.sha256(identity_input).hexdigest()[:24]
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "workspace_identity", identity)


class ToolFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec:
        ...

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        ...


def require_string(arguments: JsonObject, name: str, *, allow_empty: bool = False) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ToolFailure(
            "invalid_arguments",
            f"参数 {name} 必须是字符串" if allow_empty else f"参数 {name} 必须是非空字符串",
            {"argument": name},
        )
    return value


def optional_positive_number(
    arguments: JsonObject,
    name: str,
    default: float,
) -> float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ToolFailure(
            "invalid_arguments",
            f"参数 {name} 必须是正数",
            {"argument": name},
        )
    return float(value)
