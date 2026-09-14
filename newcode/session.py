from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from newcode.tools.types import ToolCall, ToolResult


Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str | None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError("消息角色只支持 system、user、assistant 或 tool")
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("消息内容必须是字符串或 None")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool 消息必须包含 tool_call_id")


@dataclass
class ChatSession:
    messages: list[ChatMessage] = field(default_factory=list)
    _context_version: int = field(default=0, init=False, repr=False)

    @property
    def context_version(self) -> int:
        return self._context_version

    def replace_messages(self, messages: list[ChatMessage]) -> None:
        self.messages = list(messages)
        self._context_version += 1

    def add_user_message(self, content: str) -> None:
        self._add_text_message("user", content)

    def add_assistant_message(self, content: str) -> None:
        self._add_text_message("assistant", content)

    def add_assistant_tool_call(self, tool_call: ToolCall) -> None:
        self.add_assistant_tool_calls([tool_call])

    def add_assistant_tool_calls(self, tool_calls: list[ToolCall]) -> None:
        if not tool_calls:
            return
        self.messages.append(
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=list(tool_calls),
            )
        )

    def add_tool_result(self, tool_call_id: str, result: ToolResult) -> None:
        if not tool_call_id:
            raise ValueError("tool_call_id 不能为空")
        self.messages.append(
            ChatMessage(
                role="tool",
                content=result.to_json(),
                tool_call_id=tool_call_id,
            )
        )

    def to_provider_messages(self) -> list[dict[str, object]]:
        provider_messages: list[dict[str, object]] = []
        for message in self.messages:
            if message.role == "tool":
                provider_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content or "",
                    }
                )
                continue

            payload: dict[str, object] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": tool_call.raw_arguments,
                        },
                    }
                    for tool_call in message.tool_calls
                ]
            provider_messages.append(payload)
        return provider_messages

    def _add_text_message(self, role: Literal["user", "assistant"], content: str) -> None:
        if not isinstance(content, str):
            raise TypeError("消息内容必须是字符串")
        if not content.strip():
            return
        self.messages.append(ChatMessage(role=role, content=content))
