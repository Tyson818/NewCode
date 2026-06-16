from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("消息角色只支持 user 或 assistant")
        if not isinstance(self.content, str):
            raise TypeError("消息内容必须是字符串")


@dataclass
class ChatSession:
    messages: list[ChatMessage] = field(default_factory=list)

    def add_user_message(self, content: str) -> None:
        self._add_message("user", content)

    def add_assistant_message(self, content: str) -> None:
        self._add_message("assistant", content)

    def to_provider_messages(self) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self.messages
        ]

    def _add_message(self, role: Role, content: str) -> None:
        if not isinstance(content, str):
            raise TypeError("消息内容必须是字符串")
        if not content.strip():
            return
        self.messages.append(ChatMessage(role=role, content=content))
