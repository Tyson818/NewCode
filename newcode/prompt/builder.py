from __future__ import annotations

from collections.abc import Sequence

from newcode.prompt.modules import (
    PromptModule,
    StablePrompt,
    default_optional_modules,
    default_stable_modules,
)
from newcode.prompt.reminder import PromptBuildContext, build_system_reminder
from newcode.session import ChatMessage


class PromptBuilder:
    def __init__(
        self,
        *,
        stable_modules: Sequence[PromptModule] | None = None,
        optional_modules: Sequence[PromptModule] | None = None,
    ) -> None:
        self._stable_modules = list(stable_modules or default_stable_modules())
        self._optional_modules = list(optional_modules or default_optional_modules())

    def build_stable_prompt(self) -> StablePrompt:
        modules = [
            module
            for module in [*self._stable_modules, *self._optional_modules]
            if module.stable and module.content.strip()
        ]
        modules.sort(key=lambda module: module.priority)
        return StablePrompt(
            content="\n\n".join(_format_module(module) for module in modules),
            module_keys=tuple(module.key for module in modules),
        )

    def build_messages(
        self,
        session_messages: Sequence[ChatMessage],
        context: PromptBuildContext,
    ) -> list[ChatMessage]:
        messages = [
            ChatMessage(
                role="system",
                content=self.build_stable_prompt().content,
            )
        ]
        reminder = build_system_reminder(context)
        if reminder:
            messages.append(ChatMessage(role="system", content=reminder))
        messages.extend(session_messages)
        return messages


def _format_module(module: PromptModule) -> str:
    return f"## {module.title}\n{module.content.strip()}"
