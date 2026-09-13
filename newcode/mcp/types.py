from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


JsonObject = dict[str, Any]
MCPTransport = Literal["stdio", "streamable_http"]


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: MCPTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    _sensitive_values: tuple[str, ...] = field(
        default_factory=tuple,
        repr=False,
        compare=False,
    )

    def redact(self, text: str) -> str:
        redacted = text
        for value in self._sensitive_values:
            if value:
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted


@dataclass(frozen=True)
class MCPToolDescriptor:
    server_name: str
    remote_name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True)
class MCPServerError:
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class MCPToolCallResult:
    content: tuple[Any, ...] = ()
    structured_content: Any | None = None
    is_error: bool = False
    error: MCPServerError | None = None

    @classmethod
    def failure(cls, code: str, message: str) -> MCPToolCallResult:
        return cls(is_error=True, error=MCPServerError(code=code, message=message))


class MCPServerState(Enum):
    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MCPServerStatus:
    server_name: str
    state: MCPServerState
    tool_count: int = 0
    error: MCPServerError | None = None

    @classmethod
    def ready(cls, server_name: str, *, tool_count: int) -> MCPServerStatus:
        return cls(
            server_name=server_name,
            state=MCPServerState.READY,
            tool_count=tool_count,
        )

    @classmethod
    def unavailable(
        cls,
        config: MCPServerConfig,
        *,
        code: str,
        message: str,
    ) -> MCPServerStatus:
        return cls(
            server_name=config.name,
            state=MCPServerState.UNAVAILABLE,
            error=MCPServerError(code=code, message=config.redact(message)),
        )
